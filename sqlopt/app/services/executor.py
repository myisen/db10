"""ExecutorService：在线执行 SQL。

流程：
  1) 归一化 → 过 PolicyService
  2) 允许 → 调用适配器执行（SELECT 直接执行；写 SQL 强制回滚）
  3) 结果 → 归一化新 SQL 入库 → 写 exec_history
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..adapters.factory import make_adapter
from ..config import get_settings
from ..database import make_session
from ..models.models import SqlExecHistory, SqlFingerprint, SqlTarget
from .normalizer import normalize
from .policy import PolicyService


class ExecutorService:
    def __init__(self, db: Session | None = None):
        self._own_session = db is None
        self.db = db or make_session()

    def close(self) -> None:
        if self._own_session:
            self.db.close()

    def execute(self, target_id: int, sql: str) -> dict:
        settings = get_settings()
        target = self.db.get(SqlTarget, target_id)
        if not target:
            raise ValueError(f"target_id={target_id} not found")

        # 1) 归一化（用关键字推断 stmt_type，还没拿到 command_type）
        norm = normalize(sql)

        # 2) 过策略
        policy = PolicyService(self.db)
        decision = policy.check_exec(norm.stmt_type, sql, target_id)
        if not decision.allow:
            # 仍然记录一次 exec_history（方便查谁被拦了）
            history = SqlExecHistory(
                target_id=target_id,
                fingerprint=norm.fp,
                sql_text=sql,
                stmt_type=norm.stmt_type,
                exec_status="blocked",
                elapsed_ms=0,
                rows_affected=0,
                error_msg=decision.reason,
            )
            self.db.add(history)
            self.db.commit()
            return {
                "status": "blocked",
                "exec_status": "blocked",
                "reason": decision.reason,
                "stmt_type": norm.stmt_type,
                "fingerprint": norm.fp,
                "elapsed_ms": 0.0,
                "rows": [],
                "total_rows": 0,
            }

        # 3) 执行
        adapter = make_adapter(
            target.db_type, target.conn_url, target.username,
            target.password_enc, target.extra_conf or {},
        )
        start = time.time()
        result: dict[str, Any] = {
            "status": "success", "stmt_type": norm.stmt_type,
            "rows": [], "elapsed_ms": 0, "rows_affected": 0,
        }

        try:
            result["rows"] = adapter.execute(sql, settings.exec_timeout_seconds)
            result["rows_affected"] = len(result["rows"])
        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
        result["elapsed_ms"] = round((time.time() - start) * 1000, 2)

        # 4) 自动入库（新 SQL）
        fp = self.db.query(SqlFingerprint).filter(
            SqlFingerprint.fingerprint == norm.fp
        ).first()
        if fp is None:
            fp = SqlFingerprint(
                fingerprint=norm.fp,
                canonical_sql=norm.canonical_sql,
                literal_sql=norm.literal_sql,
                literal_available=norm.literal_available,
                stmt_type=norm.stmt_type,
            )
            self.db.add(fp)

        # 5) exec_history
        exec_status = result["status"]
        if exec_status == "success" and decision.force_rollback:
            exec_status = "rollback"

        history = SqlExecHistory(
            target_id=target_id,
            fingerprint=norm.fp,
            sql_text=sql,
            stmt_type=norm.stmt_type,
            exec_status=exec_status,
            elapsed_ms=result["elapsed_ms"],
            rows_affected=result.get("rows_affected", 0),
            error_msg=result.get("error"),
        )
        self.db.add(history)
        self.db.commit()

        return {
            "status": result["status"],
            "exec_status": exec_status,
            "exec_status_hint": decision.reason if decision.force_rollback else None,
            "stmt_type": norm.stmt_type,
            "elapsed_ms": result["elapsed_ms"],
            "rows": result.get("rows")[: settings.exec_max_rows],
            "total_rows": len(result.get("rows", [])),
            "fingerprint": norm.fp,
        }
