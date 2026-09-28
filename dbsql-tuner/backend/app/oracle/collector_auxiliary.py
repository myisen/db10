"""F9 — Auxiliary Oracle diagnostics.

Three read-only collectors that complement the main SQL/plan/profile pipeline
with the "is this a database environment problem, not a SQL problem?" angle:

  1. collect_lock_chain      — V$LOCK + V$SESSION → blocker → waiter 链
  2. collect_top_waits       — V$SESSION_WAIT GROUP BY EVENT COUNT Top 5
  3. collect_key_parameters  — V$PARAMETER 快照（optimizer_mode / pga / sga / cursor_sharing ...）

All are best-effort — if a V$ view is inaccessible (权限不足 / 版本不同),
the collector returns an empty list + 不抛异常.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from loguru import logger

from .connection import OracleConnection


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------

@dataclass
class LockNode:
    sid: int
    serial: int
    username: Optional[str]
    machine: Optional[str]
    status: Optional[str]        # ACTIVE / INACTIVE / KILLED ...
    lock_type: Optional[str]
    mode_held: Optional[str]
    request_mode: Optional[str]
    request_status: Optional[str]  # GRANTED / WAITING
    wait_event: Optional[str]
    sql_id: Optional[str]
    sql_text_preview: Optional[str]

    def to_dict(self) -> dict:
        return self.__dict__


@dataclass
class TopWaitEvent:
    event: str
    wait_count: int
    total_wait_sec: float
    interpretation: str  # 简短解读（人类可读）

    def to_dict(self) -> dict:
        return self.__dict__


@dataclass
class ParameterRow:
    name: str
    value: str
    default: bool
    is_modified: bool
    description: Optional[str]

    def to_dict(self) -> dict:
        return self.__dict__


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# 每个等待事件对应的典型解读 —— 快速帮助 DBA 定位问题根因
_WAIT_EVENT_README: dict[str, str] = {
    "db file sequential read":   "单块顺序读 —— 可能是小表全表扫描或索引范围扫描多",
    "db file scattered read":    "多块散乱读 —— 典型 TABLE ACCESS FULL，考虑加索引",
    "log file sync":             "日志写等待 —— 可能频繁 COMMIT 或 redo log 慢盘",
    "log file switch":           "日志切换 —— REDO 组太小或 ARCHIVE 慢",
    "buffer busy waits":         "Buffer 忙 —— 热点数据块争用（热点表 / 热点索引）",
    "latch free":                "Latch 争用 —— shared pool / library cache 不够大",
    "library cache pin":         "硬解析多 —— 缺绑定变量 / cursor_sharing 设置问题",
    "enqueue: TX":               "TX 锁争用 —— 业务行锁冲突（可能应用设计问题）",
    "direct path write temp":    "Sort/Hash 落盘 —— PGA_AGGREGATE_TARGET 太小",
    "direct path read temp":     "读临时表 —— PGA 不够导致 spilling",
    "archiver log":              "归档等待 —— 归档进程落后或磁盘满",
    "control file sequential read": "控制文件读 —— 控制文件太大 / 磁盘慢",
    "SQL*Net message from client":  "等待客户端 —— 应用端慢，非 Oracle 问题",
    "rdbms ipc reply":           "后台进程同步等待 —— 可能有 SMON / PMON 异常",
    "PL/SQL lock timer":         "DBMS_LOCK.SLEEP —— 业务代码里显式 sleep",
}


def _interpret(event: str) -> str:
    for k, v in _WAIT_EVENT_README.items():
        if event.lower().startswith(k.lower()):
            return v
    return "（未收录 —— 请在 My Oracle Support 或 V$EVENT_NAME 查含义）"


# ---------------------------------------------------------------------------
# 1. Lock chain
# ---------------------------------------------------------------------------

LOCK_CHAIN_SQL = """
SELECT
  s.sid, s.serial#, NVL(s.username, '-') AS username,
  SUBSTR(NVL(s.machine, '-'), 1, 20) AS machine,
  s.status,
  l.type AS lock_type,
  DECODE(l.lmode, 0, 'None', 1, 'Null', 2, 'SubShare', 3, 'Share',
                  4, 'SubExcl', 5, 'ShareExcl', 6, 'Exclusive', TO_CHAR(l.lmode)) AS mode_held,
  DECODE(l.request, 0, 'None', 1, 'Null', 2, 'SubShare', 3, 'Share',
                  4, 'SubExcl', 5, 'ShareExcl', 6, 'Exclusive', TO_CHAR(l.request)) AS request_mode,
  DECODE(l.block, 0, 'Not Blocking', 1, 'Blocking', 2, 'Global Block', TO_CHAR(l.block)) AS request_status,
  sw.event AS wait_event,
  s.sql_id,
  SUBSTR(qa.sql_text, 1, 120) AS sql_text_preview
FROM v$lock l
JOIN v$session s ON s.sid = l.sid
LEFT JOIN v$session_wait sw ON sw.sid = s.sid AND sw.seq# = (
  SELECT MAX(seq#) FROM v$session_wait WHERE sid = s.sid
)
LEFT JOIN v$sqlarea qa ON qa.address = s.sql_address AND qa.hash_value = s.sql_hash_value
WHERE l.request > 0  -- 正在等锁的才是真正的 waiter
ORDER BY l.request ASC
FETCH FIRST 100 ROWS ONLY
"""


def collect_lock_chain(conn: OracleConnection) -> List[LockNode]:
    nodes: List[LockNode] = []
    try:
        with conn.raw.cursor() as cur:
            cur.execute(LOCK_CHAIN_SQL)
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                nodes.append(LockNode(
                    sid=int(d["SID"]),
                    serial=int(d["SERIAL#"]),
                    username=d.get("USERNAME"),
                    machine=d.get("MACHINE"),
                    status=d.get("STATUS"),
                    lock_type=d.get("LOCK_TYPE"),
                    mode_held=d.get("MODE_HELD"),
                    request_mode=d.get("REQUEST_MODE"),
                    request_status=d.get("REQUEST_STATUS"),
                    wait_event=d.get("WAIT_EVENT"),
                    sql_id=d.get("SQL_ID"),
                    sql_text_preview=d.get("SQL_TEXT_PREVIEW"),
                ))
    except Exception as e:
        logger.debug(f"collect_lock_chain best-effort failed (权限/版本): {e}")
    return nodes


# ---------------------------------------------------------------------------
# 2. Top wait events
# ---------------------------------------------------------------------------

TOP_WAITS_SQL = """
SELECT EVENT, COUNT(*) AS WAITERS, SUM(WAIT_TIME_MICRO)/1_000_000 AS TOTAL_WAIT_SEC
FROM V$SESSION_WAIT
WHERE STATE = 'WAITING'
GROUP BY EVENT
ORDER BY WAITERS DESC
FETCH FIRST 10 ROWS ONLY
"""


def collect_top_waits(conn: OracleConnection, top_n: int = 5) -> List[TopWaitEvent]:
    rows: List[TopWaitEvent] = []
    try:
        with conn.raw.cursor() as cur:
            cur.execute(TOP_WAITS_SQL)
            for event, cnt, sec in cur.fetchall():
                rows.append(TopWaitEvent(
                    event=event,
                    wait_count=int(cnt),
                    total_wait_sec=float(sec or 0),
                    interpretation=_interpret(event),
                ))
    except Exception as e:
        logger.debug(f"collect_top_waits best-effort failed: {e}")
    return rows[:top_n]


# ---------------------------------------------------------------------------
# 3. Key parameters
# ---------------------------------------------------------------------------

KEY_PARAMETER_NAMES = [
    # 优化器
    "optimizer_mode",
    "optimizer_index_cost_adj",
    "optimizer_index_caching",
    "statistics_level",
    # 解析
    "cursor_sharing",
    "session_cached_cursors",
    # 内存
    "sga_target",
    "sga_max_size",
    "pga_aggregate_target",
    "db_cache_size",
    "shared_pool_size",
    "java_pool_size",
    "large_pool_size",
    "streams_pool_size",
    # 并发/锁
    "dml_locks",
    "transactions",
    # 其他
    "db_block_size",
    "db_file_multiblock_read_count",
]


def collect_key_parameters(conn: OracleConnection,
                           names: Optional[List[str]] = None) -> List[ParameterRow]:
    want = set(names or KEY_PARAMETER_NAMES)
    rows: List[ParameterRow] = []
    try:
        with conn.raw.cursor() as cur:
            # First attempt — pass one param name at a time to be safe
            for name in sorted(want):
                cur.execute(
                    """
                    SELECT NAME, VALUE, ISDEFAULT, ISMODIFIED, DESCRIPTION
                    FROM V$PARAMETER
                    WHERE NAME = :name
                    """,
                    {"name": name},
                )
                for name_, value, isdefault, ismodified, desc in cur.fetchall():
                    rows.append(ParameterRow(
                        name=name_,
                        value=str(value) if value is not None else "",
                        default=(isdefault == "TRUE"),
                        is_modified=(ismodified == "MODIFIED"),
                        description=desc,
                    ))
            if rows:
                return rows
            # If the per-name approach returned nothing (version/perm issue), fall back to bulk approach below.
            for name, value, isdefault, ismodified, desc in cur.fetchall():
                rows.append(ParameterRow(
                    name=name,
                    value=str(value) if value is not None else "",
                    default=(isdefault == "TRUE"),
                    is_modified=(ismodified == "MODIFIED"),
                    description=desc,
                ))
    except Exception as e:
        logger.debug(f"collect_key_parameters best-effort failed: {e}")
        # 降级：用 IN (:n1, :n2, ...) 形式重试
        try:
            with conn.raw.cursor() as cur:
                want_sorted = sorted(want)
                placeholders = ", ".join(f":p{i}" for i in range(len(want_sorted)))
                bind_map = {f"p{i}": n for i, n in enumerate(want_sorted)}
                cur.execute(
                    """
                    SELECT NAME, VALUE, ISDEFAULT, ISMODIFIED, DESCRIPTION
                    FROM V$PARAMETER
                    WHERE NAME IN (""" + placeholders + """
                    ORDER BY NAME
                    """,
                    bind_map,
                )
                for name, value, isdefault, ismodified, desc in cur.fetchall():
                    rows.append(ParameterRow(
                        name=name,
                        value=str(value) if value is not None else "",
                        default=(isdefault == "TRUE"),
                        is_modified=(ismodified == "MODIFIED"),
                        description=desc,
                    ))
        except Exception as e2:
            logger.debug(f"V$PARAMETER second attempt also failed: {e2}")
    return rows
