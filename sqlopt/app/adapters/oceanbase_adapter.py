"""OceanBase 适配器（MySQL 协议）。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable

from ..services.crypto import decrypt
from .oracle_adapter import BaseAdapter, RawSQLRecord


class OceanBaseAdapter(BaseAdapter):
    db_type = "oceanbase"

    def __init__(self, conn_url: str, username: str, password_enc: str, extra_conf: dict | None = None):
        # conn_url 形如 "host:port"
        if ":" in conn_url:
            host, port = conn_url.split(":", 1)
            self.host = host
            self.port = int(port)
        else:
            self.host = conn_url
            self.port = 2883
        self.username = username
        self.password = decrypt(password_enc)
        self.extra_conf = extra_conf or {}
        self.tenant_id = self.extra_conf.get("tenant_id")
        self.tenant_name = self.extra_conf.get("tenant_name")

    def _connect(self):
        import pymysql
        return pymysql.connect(
            host=self.host, port=self.port,
            user=self.username, password=self.password,
            # OB 审计视图在 sys 库也能查
            database=None if self.tenant_name else None,
            charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10, read_timeout=300, write_timeout=300,
        )

    def test_connection(self) -> tuple[bool, str]:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                    # 权限
                    cur.execute("SELECT COUNT(*) FROM gv$ob_sql_audit WHERE rownum <= 1")
                    cur.fetchone()
            return True, "OK"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def collect_history(self, since_days: int = 30) -> Iterable[RawSQLRecord]:
        sql = f"""
        SELECT audit_sql_id, sql_text, db_user, exec_start_time, exec_time,
               affected_rows, return_rows, status_code, parameters_json
        FROM gv$ob_sql_audit
        WHERE exec_start_time >= NOW() - INTERVAL {since_days} DAY
        """
        binds = []
        if self.tenant_id:
            sql += " AND tenant_id = %s"
            binds.append(self.tenant_id)

        records: list[RawSQLRecord] = []
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, binds or None)
                    for row in cur.fetchall():
                        params = None
                        pj = row.get("parameters_json")
                        if pj:
                            try:
                                params = json.loads(pj)
                            except Exception:
                                pass
                        records.append(RawSQLRecord(
                            sql_text=row.get("sql_text") or "",
                            sql_id=row.get("audit_sql_id"),
                            command_type=None,   # OB 审计表没 command_type，靠关键字推断
                            binds=self._parse_params(params),
                            executions=1,
                            elapsed_ms=float((row.get("exec_time") or 0) / 1000.0),
                            rows_processed=int(row.get("affected_rows") or 0),
                            snap_time=row.get("exec_start_time") or datetime.utcnow(),
                            db_snap_id=str(row.get("audit_sql_id") or ""),
                            schema_name=row.get("db_user"),
                        ))
        except Exception as e:
            raise RuntimeError(f"OB collect failed: {e}") from e

        return records

    def collect_one(self, sql_id: str) -> RawSQLRecord | None:
        sql = """
        SELECT audit_sql_id, sql_text, db_user, exec_start_time, exec_time,
               affected_rows, return_rows, parameters_json
        FROM gv$ob_sql_audit WHERE audit_sql_id = %s
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (sql_id,))
                    row = cur.fetchone()
                    if not row:
                        return None
                    params = None
                    pj = row.get("parameters_json")
                    if pj:
                        try:
                            params = json.loads(pj)
                        except Exception:
                            pass
                    return RawSQLRecord(
                        sql_text=row.get("sql_text") or "",
                        sql_id=row.get("audit_sql_id"),
                        binds=self._parse_params(params),
                        elapsed_ms=float((row.get("exec_time") or 0) / 1000.0),
                        rows_processed=int(row.get("affected_rows") or 0),
                        snap_time=row.get("exec_start_time"),
                        db_snap_id=str(row.get("audit_sql_id") or ""),
                        schema_name=row.get("db_user"),
                    )
        except Exception:
            return None

    def execute(self, sql: str, timeout_sec: int) -> list[dict]:
        with self._connect() as conn:
            try:
                conn.begin()  # 自动开事务
                with conn.cursor() as cur:
                    cur.execute(sql)
                    if cur.description:
                        cols = [d[0].lower() for d in cur.description]
                        return [dict(zip(cols, r)) for r in cur.fetchmany(size=1000)]
                    return []
            finally:
                conn.rollback()

    def explain(self, sql: str) -> dict:
        """OB EXPLAIN（MySQL 协议），返回原始表 + 自拼文本。"""
        result: dict = {
            "plan_hash": None,
            "raw_tree": [],
            "formatted_text": "",
            "db_type": "oceanbase",
        }

        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    try:
                        cur.execute(f"EXPLAIN {sql}")
                    except Exception as e:
                        result["formatted_text"] = f"EXPLAIN 失败: {e}"
                        return result

                    cols = [d[0].lower() for d in cur.description] if cur.description else []
                    rows = cur.fetchall()
                    result["raw_tree"] = [dict(zip(cols, r)) for r in rows]

                    # 自拼格式化文本
                    lines = []
                    col_widths = {c: max(len(str(r.get(c, "") or "")) for r in result["raw_tree"]) for c in cols}
                    lines.append(" | ".join(c.ljust(col_widths[c]) for c in cols))
                    lines.append("-+-".join("-" * col_widths[c] for c in cols))
                    for r in result["raw_tree"]:
                        lines.append(" | ".join(
                            str(r.get(c, "") or "").ljust(col_widths[c]) for c in cols
                        ))
                    result["formatted_text"] = "\n".join(lines)

                    # plan_hash 从 OB 的 explain output 里拿可能不可靠，留 None
            except Exception as e:
                result["formatted_text"] = f"explain error: {e}"

        return result

    @staticmethod
    def _parse_params(params: Any) -> list[dict[str, Any]] | None:
        """把 OB 的 parameters_json 解析成 normalizer 需要的格式。

        OB 可能的格式：
          [{"name":"x","value":"1","data_type":"INT"}, ...]
          [{"pos":1,"val":"a"}, ...]
        """
        if not params:
            return None
        if isinstance(params, list):
            out = []
            for i, p in enumerate(params):
                if isinstance(p, dict):
                    out.append({
                        "name": p.get("name") or f"p{i}",
                        "value": p.get("value") or p.get("val"),
                        "type": str(p.get("data_type") or p.get("type") or "str").lower(),
                    })
                else:
                    out.append({"name": f"p{i}", "value": p, "type": "str"})
            return out if out else None
        return None
