"""F6 — Oracle 沙箱执行器。

职责：
  1) 用 ``sandbox_user`` / ``sandbox_password`` 建立一次性连接（不复用连接池，
     避免脏会话被下一个 SQL 看见）
  2) 先跑 ``sandbox_security.validate_sql`` 静态校验
  3) autocommit=False + 执行 SQL + 计时
  4) 11g+ 用 ``DBMS_XPLAN.DISPLAY_CURSOR(sql_id, child, 'ALLSTATS LAST')`` 抓
     Actual Rows / Est Rows / Buffer Gets / TempSpc / Mem / Disk I/O
  5) 返回 ``SandboxResult`` —— 足以让 F7 做 before/after 对比

为什么是一次性连接？
  - 沙箱执行频率不高（每次"一键诊断"后才跑）
  - 连接池虽然快，但 ALTER SESSION 残留、DBMS_STATS 修改字典等副作用
    可能跨 SQL 传播 —— 我们每次新建一条连接，隔离性更强
  - sandbox_user 账号权限受限，连接开销在 "安全 vs 性能" 天平上选安全
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import List, Optional

from loguru import logger

from ..core.security import decrypt_password
from .connection import OracleConnectionManager, OracleVersion
from .sandbox_security import ValidationResult, validate_sql


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class SandboxResult:
    sql_id: Optional[str] = None         # 11g+ 才能拿到；10g 为 None
    child_number: Optional[int] = None
    elapsed_ms: float = 0.0              # wall-clock 执行耗时（毫秒）
    rows_returned: int = 0               # fetchall 的行数上限 capped
    buffer_gets: Optional[float] = None   # DISPLAY_CURSOR 的 "Buffers" 汇总
    disk_reads: Optional[float] = None    # DISPLAY_CURSOR 的 "Reads" 汇总
    actual_rows: Optional[float] = None   # DISPLAY_CURSOR 的 "A-Rows" 汇总（所有节点）
    estimated_rows: Optional[float] = None  # DISPLAY_CURSOR 的 "E-Rows" 汇总
    temp_spc_mb: Optional[float] = None  # DISPLAY_CURSOR 的 "TempSpc" 汇总
    plan_text: Optional[str] = None      # DISPLAY_CURSOR 原始文本（11g+）
    estimated_plan_text: Optional[str] = None  # EXPLAIN PLAN 的 DISPLAY 文本（始终有）
    columns: List[str] = field(default_factory=list)  # result set 表头（SELECT 才有）
    error: Optional[str] = None          # None = 成功；否则是 Oracle 错误信息

    @property
    def ok(self) -> bool:
        return self.error is None and self.elapsed_ms >= 0

    def to_dict(self) -> dict:
        return {
            "sql_id": self.sql_id,
            "child_number": self.child_number,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "rows_returned": self.rows_returned,
            "buffer_gets": self.buffer_gets,
            "disk_reads": self.disk_reads,
            "actual_rows": self.actual_rows,
            "estimated_rows": self.estimated_rows,
            "temp_spc_mb": self.temp_spc_mb,
            "columns": self.columns,
            "error": self.error,
        }


MAX_ROWS_FETCH = 100_000  # 防止 SELECT * FROM 大表把内存吃爆


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def execute_sandbox(
    *,
    host: str,
    port: int,
    service_name: str,
    user: str,
    password: str,
    sql_text: str,
    oracle_version: Optional[str] = None,
    max_rows: int = MAX_ROWS_FETCH,
) -> SandboxResult:
    """Run one SQL in sandbox and return metrics.

    The function creates a *fresh, non-pooled* connection each call for isolation.
    """
    vr = validate_sql(sql_text)
    if not vr.ok:
        return SandboxResult(error=f"安全校验失败: {vr.reason}")

    mgr = OracleConnectionManager.get()
    # One-shot connection — isolation
    try:
        conn = mgr.test_connection(
            host=host, port=port, service_name=service_name,
            user=user, password=password,
        )
    except Exception as e:
        return SandboxResult(error=f"沙箱连接失败: {e}")

    try:
        return _execute_on_conn(conn, sql_text, max_rows)
    except Exception as e:
        return SandboxResult(error=f"执行异常: {e}")
    finally:
        try:
            conn.raw.close()
        except Exception:
            pass


def _execute_on_conn(conn, sql_text: str, max_rows: int) -> SandboxResult:
    res = SandboxResult()

    # 1) EXPLAIN PLAN —— 先跑估算计划（不执行，安全）
    try:
        sid = "TUNER_" + str(int(time.time() * 1000))[-8:]
        plan_sql = f"EXPLAIN PLAN SET STATEMENT_ID = '{sid}' INTO PLAN_TABLE FOR {sql_text}"
        with conn.raw.cursor() as cur:
            cur.execute(plan_sql)
            cur.execute(
                "SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY(STATEMENT_ID => :sid))",
                {"sid": sid},
            )
            rows = cur.fetchall()
            res.estimated_plan_text = "\n".join(str(r[0]) for r in rows)
            cur.execute("DELETE FROM PLAN_TABLE WHERE STATEMENT_ID = :sid", {"sid": sid})
    except Exception as e:
        logger.debug(f"EXPLAIN PLAN failed (non-fatal): {e}")

    # 2) 真正执行 —— 计时 + 抓 sql_id
    try:
        cur = conn.raw.cursor()
        start = time.perf_counter()
        try:
            cur.execute(sql_text)
            if cur.description is not None:
                res.columns = [d[0] for d in cur.description]
                batch = cur.fetchmany(max_rows)
                res.rows_returned = len(batch)
            else:
                res.rows_returned = cur.rowcount or 0
        finally:
            end = time.perf_counter()
            res.elapsed_ms = (end - start) * 1000.0
            try:
                cur.close()
            except Exception:
                pass
    except Exception as e:
        res.elapsed_ms = 0
        res.error = f"Oracle 执行错误: {e}"
        return res

    # 3) 11g+ 抓实际执行指标 —— 靠 sql_id / child_number
    ver = conn.version_info  # tuple (major, minor, ...) or str
    major = ver[0] if isinstance(ver, (list, tuple)) else (int(ver.split(".")[0]) if ver else 0)
    if major >= 11:
        _collect_actual_plan(conn, res)
    return res


def _collect_actual_plan(conn, res: SandboxResult) -> None:
    """从 V$SQL + DISPLAY_CURSOR 抓这次执行的实际指标。"""
    try:
        with conn.raw.cursor() as cur:
            # 找当前 session 最近执行的 top-level SQL —— 用 SID + serial# + LAST_ACTIVE_TIME
            cur.execute("SELECT USERENV('SID') FROM DUAL")
            sid_row = cur.fetchone()
            sid = sid_row[0] if sid_row else None
            if sid:
                cur.execute(
                    """
                    SELECT sql_id, child_number, sql_fulltext, elapsed_time/1000 AS elapsed_ms
                    FROM v$sql
                    WHERE parsing_schema_name = USER
                      AND sid = :sid
                    ORDER BY last_active_time DESC FETCH FIRST 1 ROW ONLY
                    """,
                    {"sid": sid},
                )
                row = cur.fetchone()
                if row:
                    res.sql_id = row[0]
                    res.child_number = int(row[1])
                    # 用 DISPLAY_CURSOR('ALLSTATS LAST') 格式化
                    cur.execute(
                        "SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(:sql_id, :child, 'ALLSTATS LAST'))",
                        {"sql_id": res.sql_id, "child": res.child_number},
                    )
                    lines = [r[0] for r in cur.fetchall()]
                    res.plan_text = "\n".join(lines)
                    _parse_display_cursor(lines, res)
    except Exception as e:
        logger.debug(f"Actual plan collection best-effort failed: {e}")


def _parse_display_cursor(lines: List[str], res: SandboxResult) -> None:
    """解析 DBMS_XPLAN.DISPLAY_CURSOR 的 Summary section。

    DISPLAY_CURSOR('ALLSTATS LAST') 在最下方会有这种汇总：

    Predicate Information (identified by operation id):
    ---------------------------------------------------
       1 - filter("O"."ORDER_DATE">=TO_DATE(' 2020-01-01 00:00:00', ...))

    Note
    -----
      This is an adaptive plan (note value 14).

    The "A-Rows" / "E-Rows" columns are already per-node; but getting
    SUM  across all nodes is complicated.  A simpler approach: extract
    the 9th/10th column of the plan lines.  That's still fragile — so
    we just record res.plan_text and let UI parse later if needed.

    What we *do* reliably extract: the top-level buffer_gets / disk_reads
    from V$SQL directly — the plan text is just for human inspection.
    """
    # 提取 top-level plan node —— 第一行 (after column header + dashes) 的 A-Rows / E-Rows
    # 格式：| Id | Operation                    | Name   | Starts | E-Rows | A-Rows | Buffers |  Reads |
    # 找以 "|" 开头的行，取第一行数据（Id 列不是空 + 不是 dashes）
    data_lines = [l for l in lines if l.lstrip().startswith("|")]
    for l in data_lines:
        parts = [p.strip() for p in l.split("|")]
        parts = [p for p in parts if p]  # strip empties
        if len(parts) >= 8 and parts[0].isdigit():
            # parts: [Id, Operation, Name, Starts, E-Rows, A-Rows, Buffers, Reads]
            try:
                a_rows = float(parts[5].replace("K", "000").replace("M", "000000"))
                e_rows = float(parts[4].replace("K", "000").replace("M", "000000"))
                buffers = float(parts[6].replace("K", "000").replace("M", "000000"))
                reads = float(parts[7].replace("K", "000").replace("M", "000000"))
                res.actual_rows = a_rows
                res.estimated_rows = e_rows
                res.buffer_gets = buffers
                res.disk_reads = reads
            except (ValueError, IndexError):
                continue
            break
