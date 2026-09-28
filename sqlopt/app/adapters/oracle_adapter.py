"""数据库适配器基类 + Oracle 实现。

关键视图（见权限清单文档）：
  历史 AWR 路径：dba_hist_sqltext / dba_hist_sqlstat / dba_hist_sqlbind / dba_hist_snapshot
  实时 Cursor 路径：gv$sql / gv$sqltext_with_newlines / gv$sql_bind_capture
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from ..services.crypto import decrypt


# --- 统一的数据结构 ---
@dataclass
class RawSQLRecord:
    """采集回来的原始 SQL 记录（一条 SQL 的一次执行 / 一次快照）。"""
    sql_text: str                # 原始 SQL（未归一化）
    sql_id: str | None           # Oracle sql_id / OB 审计 id
    command_type: int | str | None
    # 绑定变量列表（能拿多少拿多少）
    binds: list[dict[str, Any]] | None
    # 性能指标
    executions: int = 0
    elapsed_ms: float = 0.0
    cpu_ms: float = 0.0
    buffer_gets: int = 0
    rows_processed: int = 0
    disk_reads: int = 0
    optimizer_cost: float | None = None
    plan_hash: str | None = None
    snap_time: datetime | None = None
    db_snap_id: str | None = None
    schema_name: str | None = None
    # 原始 JSON（便于后续扩展）
    raw: dict | None = None


# --- 基类 ---
class BaseAdapter(ABC):
    db_type: str = "base"

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """返回 (ok, message)。"""

    @abstractmethod
    def collect_history(self, since_days: int = 30) -> Iterable[RawSQLRecord]:
        """拉历史 SQL 记录。"""

    @abstractmethod
    def collect_one(self, sql_id: str) -> RawSQLRecord | None:
        """手动拉一条 SQL 最新一次执行。"""

    @abstractmethod
    def execute(self, sql: str, timeout_sec: int) -> list[dict]:
        """在线执行（只读）。"""


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------
class OracleAdapter(BaseAdapter):
    db_type = "oracle"

    def __init__(self, conn_url: str, username: str, password_enc: str, extra_conf: dict | None = None):
        self.conn_url = conn_url          # 形如 "host:port/sid" 或 "host:port/service"
        self.username = username
        self.password = decrypt(password_enc)
        self.extra_conf = extra_conf or {}
        self.pdb_service = self.extra_conf.get("pdb_service")  # PDB service name，可选

    # --- 内部 ---
    def _connect(self):
        import oracledb
        # DSN 可以是简单串：host:port/service 或 TNS
        dsn = self.conn_url
        if self.pdb_service:
            # 覆盖 service name
            base = self.conn_url.rsplit("/", 1)[0] if "/" in self.conn_url else self.conn_url
            dsn = f"{base}/{self.pdb_service}"
        return oracledb.connect(user=self.username, password=self.password, dsn=dsn)

    def test_connection(self) -> tuple[bool, str]:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM DUAL")
                    cur.fetchone()
                    # 权限检查
                    cur.execute(
                        "SELECT COUNT(*) FROM dba_hist_snapshot WHERE rownum <= 1"
                    )
                    cur.fetchone()
            return True, "OK"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def collect_history(self, since_days: int = 30) -> Iterable[RawSQLRecord]:
        """拉 AWR 历史。"""
        try:
            import oracledb
        except ImportError as e:
            raise RuntimeError("oracledb not installed") from e

        with self._connect() as conn:
            conn.arraysize = 1000

            # 1) 取时间范围对应的 snap_id
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT MIN(snap_id), MAX(snap_id)
                    FROM dba_hist_snapshot
                    WHERE begin_interval_time >= SYSDATE - :days
                    """,
                    days=since_days,
                )
                row = cur.fetchone()
                if row is None or row[0] is None:
                    return []
                min_snap, max_snap = row

            # 2) 一次性拉取 SQL 文本 + 统计 + 绑定
            # 按 sql_id 聚合（多 piece 拼回）
            sql_map: dict[str, dict] = {}

            with conn.cursor() as cur:
                # 文本（按 piece 聚合）
                cur.execute(
                    """
                    SELECT t.sql_id, t.command_type,
                           t.sql_text_chunk, t.piece,
                           MAX(st.executions_delta) over (partition by t.sql_id) as execs,
                           MAX(st.elapsed_time_delta) over (partition by t.sql_id)/1000 as elapsed_ms,
                           MAX(st.cpu_time_delta) over (partition by t.sql_id)/1000 as cpu_ms,
                           MAX(st.buffer_gets_delta) over (partition by t.sql_id) as buffer_gets,
                           MAX(st.rows_processed_delta) over (partition by t.sql_id) as rows_proc,
                           MAX(st.disk_reads_delta) over (partition by t.sql_id) as disk_reads,
                           MIN(st.optimizer_cost) over (partition by t.sql_id) as opt_cost,
                           MIN(st.plan_hash_value) over (partition by t.sql_id) as plan_hash
                    FROM dba_hist_sqltext t
                    LEFT JOIN dba_hist_sqlstat st
                      ON st.sql_id = t.sql_id AND st.snap_id BETWEEN :min_snap AND :max_snap
                    WHERE EXISTS (
                      SELECT 1 FROM dba_hist_snapshot s
                      WHERE s.snap_id >= :min_snap AND s.snap_id <= :max_snap
                    )
                    """,
                    min_snap=min_snap, max_snap=max_snap,
                )
                for row in cur:
                    (sql_id, cmd_type, chunk, piece, execs, elapsed_ms,
                     cpu_ms, buffer_gets, rows_proc, disk_reads, opt_cost, plan_hash) = row
                    rec = sql_map.setdefault(sql_id, {
                        "sql_id": sql_id,
                        "command_type": cmd_type,
                        "pieces": [],
                        "executions": int(execs or 0),
                        "elapsed_ms": float(elapsed_ms or 0),
                        "cpu_ms": float(cpu_ms or 0),
                        "buffer_gets": int(buffer_gets or 0),
                        "rows_processed": int(rows_proc or 0),
                        "disk_reads": int(disk_reads or 0),
                        "optimizer_cost": float(opt_cost) if opt_cost is not None else None,
                        "plan_hash": str(plan_hash) if plan_hash is not None else None,
                    })
                    rec["pieces"].append((piece, chunk or ""))

            # 3) 拼回 SQL 文本
            records = []
            for sql_id, rec in sql_map.items():
                rec["pieces"].sort(key=lambda x: x[0])
                sql_text = "".join(p for _, p in rec["pieces"])
                avg_elapsed = (
                    rec["elapsed_ms"] / rec["executions"] if rec["executions"] else 0
                )
                records.append(RawSQLRecord(
                    sql_text=sql_text,
                    sql_id=sql_id,
                    command_type=rec["command_type"],
                    binds=None,   # 下一步单独拉
                    executions=rec["executions"],
                    elapsed_ms=rec["elapsed_ms"],
                    avg_elapsed_ms=avg_elapsed,
                    cpu_ms=rec["cpu_ms"],
                    buffer_gets=rec["buffer_gets"],
                    rows_processed=rec["rows_processed"],
                    disk_reads=rec["disk_reads"],
                    optimizer_cost=rec["optimizer_cost"],
                    plan_hash=rec["plan_hash"],
                    snap_time=datetime.utcnow(),
                    db_snap_id=str(max_snap),
                ))

            # 4) 批量拉绑定变量（AWR 里只存前几个 SQL 的 bind，且不一定开启）
            binds_map: dict[str, list[dict]] = {}
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT sql_id, name, position, value_string, value_type
                        FROM dba_hist_sqlbind
                        WHERE snap_id BETWEEN :min_snap AND :max_snap
                        """,
                        min_snap=min_snap, max_snap=max_snap,
                    )
                    for row in cur:
                        sql_id, name, pos, val, vtype = row
                        binds_map.setdefault(sql_id, []).append({
                            "name": name, "position": pos,
                            "value": val, "type": (vtype or "str").lower(),
                        })
            except Exception:
                # 没权限或视图不存在 → 静默降级
                pass

            for r in records:
                r.binds = binds_map.get(r.sql_id)

            return records

    def collect_one(self, sql_id: str) -> RawSQLRecord | None:
        """拉单条 SQL 最新一次执行（走 Cursor 实时视图 + AWR 兜底）。"""
        with self._connect() as conn:
            with conn.cursor() as cur:
                # 优先实时 cursor
                cur.execute(
                    """
                    SELECT s.sql_id, t.sql_text, s.command_type,
                           s.executions, s.elapsed_time/1000, s.cpu_time/1000,
                           s.buffer_gets, s.rows_processed, s.disk_reads,
                           s.optimizer_cost, s.plan_hash_value
                    FROM gv$sql s
                    JOIN gv$sqltext_with_newlines t
                      ON s.sql_id = t.sql_id
                    WHERE s.sql_id = :sql_id
                    ORDER BY t.piece
                    """,
                    sql_id=sql_id,
                )
                rows = cur.fetchall()
                if rows:
                    text = "".join(r[1] for r in rows)
                    first = rows[0]
                    rec = RawSQLRecord(
                        sql_text=text,
                        sql_id=sql_id,
                        command_type=first[2],
                        binds=None,
                        executions=int(first[3] or 0),
                        elapsed_ms=float(first[4] or 0),
                        cpu_ms=float(first[5] or 0),
                        buffer_gets=int(first[6] or 0),
                        rows_processed=int(first[7] or 0),
                        disk_reads=int(first[8] or 0),
                        optimizer_cost=float(first[9]) if first[9] else None,
                        plan_hash=str(first[10]) if first[10] else None,
                        snap_time=datetime.utcnow(),
                    )
                    # 实时绑定变量
                    try:
                        cur.execute(
                            """
                            SELECT sql_id, name, position, value_string, datatype_string
                            FROM gv$sql_bind_capture
                            WHERE sql_id = :sql_id
                            """,
                            sql_id=sql_id,
                        )
                        rec.binds = [
                            {"name": r[1], "position": r[2], "value": r[3], "type": (r[4] or "str").lower()}
                            for r in cur.fetchall()
                        ]
                    except Exception:
                        pass
                    return rec

        # 兜底 AWR
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT sql_text, command_type
                    FROM dba_hist_sqltext WHERE sql_id = :sql_id
                    """,
                    sql_id=sql_id,
                )
                row = cur.fetchone()
                if row:
                    return RawSQLRecord(
                        sql_text=row[0], sql_id=sql_id, command_type=row[1],
                        binds=None, snap_time=datetime.utcnow(),
                    )
        return None

    def execute(self, sql: str, timeout_sec: int) -> list[dict]:
        with self._connect() as conn:
            # 设置只读
            with conn.cursor() as cur:
                try:
                    cur.execute("SET TRANSACTION READ ONLY")
                except Exception:
                    pass
                cur.callproc("dbms_application_info.set_action", ("SQLOpt",))
                try:
                    cur.execute(sql)
                    cols = [d[0].lower() for d in cur.description] if cur.description else []
                    rows = cur.fetchmany(size=1000)
                    return [dict(zip(cols, r)) for r in rows]
                finally:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
