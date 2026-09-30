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
import time

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

    @abstractmethod
    def explain(self, sql: str) -> dict:
        """拉执行计划。

        返回结构（双端统一）：
          {
            "plan_hash": str | None,
            "raw_tree": [ {...}, ... ],          # 原始计划行（字段由 DB 决定）
            "formatted_text": str,               # 人类可读的格式化计划文本
          }
        """


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

    # ------------------------------------------------------------------
    # 诊断：版本 + 架构 + 完整权限矩阵（给 DBA 看）
    # ------------------------------------------------------------------
    def diagnose(self) -> dict:
        """一次性探测 Oracle 连接、版本、架构、必需权限。

        返回 dict：
          {
            "connect": {"ok": bool, "error": str},
            "version": "...",
            "cdb": bool,           # 是否多租户
            "pdb_name": str | None,
            "current_schema": str,
            "has_awr_bind_capture": bool,
            "checks": [ {"name", "ok", "detail", "grant_sql"}, ... ]
          }
        """
        out: dict = {
            "connect": {"ok": False, "error": ""},
            "version": None,
            "cdb": False,
            "pdb_name": None,
            "current_schema": None,
            "has_awr_bind_capture": False,
            "checks": [],
        }
        # 必需清单：(检查名, 测试 SQL, 缺失时 DBA 应该 GRANT 什么)
        CHECKS = [
            # --- 基础 ---
            ("CREATE SESSION", None, "已经能连上说明有 CREATE SESSION"),
            ("SELECT_CATALOG_ROLE",
             "SELECT COUNT(*) FROM all_tables WHERE rownum<=1",
             "GRANT SELECT_CATALOG_ROLE TO {user} CONTAINER=ALL"),
            # --- AWR 历史 ---
            ("AWR: DBA_HIST_SNAPSHOT",
             "SELECT COUNT(*) FROM dba_hist_snapshot WHERE rownum<=1",
             "GRANT SELECT ON SYS.DBA_HIST_SNAPSHOT TO {user} CONTAINER=ALL"),
            ("AWR: DBA_HIST_SQLTEXT",
             "SELECT COUNT(*) FROM dba_hist_sqltext WHERE rownum<=1",
             "GRANT SELECT ON SYS.DBA_HIST_SQLTEXT TO {user} CONTAINER=ALL"),
            ("AWR: DBA_HIST_SQLSTAT",
             "SELECT COUNT(*) FROM dba_hist_sqlstat WHERE rownum<=1",
             "GRANT SELECT ON SYS.DBA_HIST_SQLSTAT TO {user} CONTAINER=ALL"),
            ("AWR: DBA_HIST_SQLBIND",
             "SELECT COUNT(*) FROM dba_hist_sqlbind WHERE rownum<=1",
             "GRANT SELECT ON SYS.DBA_HIST_SQLBIND TO {user} CONTAINER=ALL"),
            ("AWR: DBA_HIST_SQL_BIND_METADATA",
             "SELECT COUNT(*) FROM dba_hist_sql_bind_metadata WHERE rownum<=1",
             "GRANT SELECT ON SYS.DBA_HIST_SQL_BIND_METADATA TO {user} CONTAINER=ALL"),
            # --- 实时 Cursor ---
            ("Live: GV_$SQL",
             "SELECT COUNT(*) FROM gv$sql WHERE rownum<=1",
             "GRANT SELECT ON SYS.GV_$SQL TO {user} CONTAINER=ALL"),
            ("Live: GV_$SQLTEXT_WITH_NEWLINES",
             "SELECT COUNT(*) FROM gv$sqltext_with_newlines WHERE rownum<=1",
             "GRANT SELECT ON SYS.GV_$SQLTEXT_WITH_NEWLINES TO {user} CONTAINER=ALL"),
            ("Live: GV_$SQL_BIND_CAPTURE",
             "SELECT COUNT(*) FROM gv$sql_bind_capture WHERE rownum<=1",
             "GRANT SELECT ON SYS.GV_$SQL_BIND_CAPTURE TO {user} CONTAINER=ALL"),
            ("Live: GV_$PDBS",
             "SELECT COUNT(*) FROM gv$pdbs WHERE rownum<=1",
             "GRANT SELECT ON SYS.GV_$PDBS TO {user} CONTAINER=ALL"),
            # --- 在线执行 ---
            ("Exec: SELECT ANY TABLE",
             "SELECT ANY_TABLE FROM DUAL",  # 占位，见下方
             "GRANT SELECT ANY TABLE TO {user} CONTAINER=ALL"),
            ("Exec: EXECUTE DBMS_SQL",
             "BEGIN EXECUTE IMMEDIATE 'SELECT 1 FROM DUAL'; END;",
             "GRANT EXECUTE ON DBMS_SQL TO {user} CONTAINER=ALL"),
            # --- ★ 执行计划 ---
            ("★ Plan: EXECUTE DBMS_XPLAN",
             "SELECT DBMS_XPLAN.FORMAT_PLAN(SYSDATE,'BASIC') FROM DUAL",  # 近似测试
             "GRANT EXECUTE ON DBMS_XPLAN TO {user} CONTAINER=ALL"),
            ("★ Plan: SELECT PLAN_TABLE$",
             "SELECT COUNT(*) FROM sys.plan_table$ WHERE rownum<=1",
             "GRANT SELECT ON SYS.PLAN_TABLE$ TO {user} CONTAINER=ALL"),
            ("★ Plan: GV_$SQL_PLAN",
             "SELECT COUNT(*) FROM gv$sql_plan WHERE rownum<=1",
             "GRANT SELECT ON SYS.GV_$SQL_PLAN TO {user} CONTAINER=ALL"),
            ("★ Plan: CREATE TABLE (PLAN_TABLE auto)",
             "CREATE TABLE sqlopt_plan_tbl_check (id NUMBER)",
             "GRANT CREATE TABLE TO {user} CONTAINER=ALL"),
        ]

        try:
            with self._connect() as conn:
                out["connect"]["ok"] = True
                with conn.cursor() as cur:
                    # 版本
                    try:
                        cur.execute("SELECT BANNER FROM GV_$VERSION WHERE rownum<=1")
                        out["version"] = cur.fetchone()[0]
                    except Exception:
                        try:
                            cur.execute("SELECT PRODUCT || ' ' || VERSION FROM PRODUCT_COMPONENT_VERSION WHERE rownum<=1")
                            out["version"] = cur.fetchone()[0]
                        except Exception as e:
                            out["version"] = f"(unknown: {e})"

                    # CDB 判断
                    try:
                        cur.execute("SELECT CDB FROM V$DATABASE")
                        out["cdb"] = cur.fetchone()[0] == "YES"
                    except Exception:
                        out["cdb"] = False

                    # 当前 schema
                    try:
                        cur.execute("SELECT SYS_CONTEXT('USERENV','SESSION_USER') FROM DUAL")
                        out["current_schema"] = cur.fetchone()[0]
                    except Exception:
                        out["current_schema"] = self.username

                    # PDB 名
                    try:
                        cur.execute("SELECT SYS_CONTEXT('USERENV','CON_NAME') FROM DUAL")
                        pdb = cur.fetchone()[0]
                        out["pdb_name"] = pdb if pdb != "CDB$ROOT" else None
                    except Exception:
                        pass

                    # awr_bind_capture
                    try:
                        cur.execute("SELECT VALUE FROM GV_$PARAMETER WHERE NAME='awr_bind_capture' AND rownum<=1")
                        v = cur.fetchone()
                        out["has_awr_bind_capture"] = (v is not None and v[0] == "ALL")
                    except Exception:
                        out["has_awr_bind_capture"] = False

                    # 逐项权限探测
                    for name, sql, grant_sql in CHECKS:
                        grant_sql = grant_sql.replace("{user}", self.username)
                        if sql is None:
                            out["checks"].append({"name": name, "ok": True, "detail": "连接成功即代表已授权", "grant_sql": grant_sql})
                            continue
                        if name == "Exec: SELECT ANY TABLE":
                            # 特殊：用 all_tables 存在性替代（SELECT ANY TABLE 不直接测）
                            sql = "SELECT COUNT(*) FROM all_tables WHERE rownum<=1"
                        try:
                            cur.execute(sql)
                            cur.fetchall()
                            out["checks"].append({"name": name, "ok": True, "detail": "OK", "grant_sql": grant_sql})
                        except Exception as e:
                            out["checks"].append({
                                "name": name, "ok": False,
                                "detail": f"{type(e).__name__}: {str(e)[:160]}",
                                "grant_sql": grant_sql,
                            })
                            # 清理可能被 CREATE TABLE 留下的垃圾表
                            if name == "★ Plan: CREATE TABLE (PLAN_TABLE auto)":
                                try:
                                    cur.execute("DROP TABLE sqlopt_plan_tbl_check")
                                except Exception:
                                    pass
        except Exception as e:
            out["connect"]["ok"] = False
            out["connect"]["error"] = f"{type(e).__name__}: {e}"

        return out

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

    def explain(self, sql: str) -> dict:
        """EXPLAIN PLAN + DBMS_XPLAN.DISPLAY。"""
        result: dict = {
            "plan_hash": None,
            "raw_tree": [],
            "formatted_text": "",
            "db_type": "oracle",
        }
        stmt_id = f"sqlopt_{int(time.time() * 1000) % 10000000}"

        with self._connect() as conn:
            conn.rollback()  # EXPLAIN PLAN 不能在只读事务里
            with conn.cursor() as cur:
                # 1) EXPLAIN PLAN
                try:
                    cur.execute(f"EXPLAIN PLAN SET STATEMENT_ID = '{stmt_id}' FOR {sql}")
                except Exception as e:
                    result["formatted_text"] = f"EXPLAIN PLAN 失败: {e}"
                    return result

                # 2) 原始树
                try:
                    cur.execute(
                        """
                        SELECT operation, options, object_name, object_type,
                               cost, cardinality, bytes, cpu_cost, io_cost,
                               access_predicates, filter_predicates, projection
                        FROM plan_table
                        WHERE statement_id = :sid
                        ORDER BY id
                        """,
                        sid=stmt_id,
                    )
                    cols = [d[0].lower() for d in cur.description]
                    result["raw_tree"] = [dict(zip(cols, r)) for r in cur.fetchall()]
                except Exception as e:
                    result["raw_tree"] = [{"error": str(e)}]

                # 3) DBMS_XPLAN.DISPLAY 拉格式化文本
                try:
                    cur.execute(
                        "SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY(STATEMENT_ID => :sid))",
                        sid=stmt_id,
                    )
                    lines = [str(r[0]) if r else "" for r in cur.fetchall()]
                    result["formatted_text"] = "\n".join(lines)
                except Exception as e:
                    # 降级：自拼 raw_tree 缩进
                    indent = 0
                    out_lines = []
                    for r in result["raw_tree"]:
                        op = f"{r.get('operation', '')}{' '+r.get('options','') if r.get('options') else ''}"
                        obj = r.get("object_name", "") or ""
                        cost = r.get("cost") or ""
                        out_lines.append(f"  {op:<25} {obj:<30} cost={cost}")
                    result["formatted_text"] = "\n".join(out_lines) or f"DBMS_XPLAN 不可用: {e}"

                # 4) 清理 PLAN_TABLE（PLAN_TABLE 是 session 级，不清也行，保险起见）
                try:
                    cur.execute("DELETE FROM plan_table WHERE statement_id = :sid", sid=stmt_id)
                except Exception:
                    pass

                conn.commit()  # EXPLAIN PLAN 产生的 DML 需要提交

                # 5) 从刚刚的 cursor 执行计划里拿 plan_hash（gv$sql_plan）
                try:
                    cur.execute(
                        "SELECT plan_hash_value FROM gv$sql_plan WHERE statement_id = :sid",
                        sid=stmt_id,
                    )
                    row = cur.fetchone()
                    if row:
                        result["plan_hash"] = str(row[0])
                except Exception:
                    pass

        return result
