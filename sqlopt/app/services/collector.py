"""采集服务：把适配器拉回来的 RawSQLRecord 归一化 + 入库。"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy.orm import Session

from ..adapters.factory import make_adapter
from ..database import get_engine, make_session
from ..models.models import (
    SqlFingerprint,
    SqlStatSnapshot,
    SqlTarget,
)
from ..services.normalizer import normalize


class CollectorService:
    """采集 + 归一化 + 入库。"""

    def __init__(self, db: Session | None = None):
        self._own_session = db is None
        self.db = db or make_session()

    def close(self) -> None:
        if self._own_session:
            self.db.close()

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------
    def collect(self, target_id: int, since_days: int = 30) -> dict:
        """全量采集。返回统计。"""
        target = self.db.get(SqlTarget, target_id)
        if not target:
            raise ValueError(f"target_id={target_id} not found")

        adapter = make_adapter(
            target.db_type, target.conn_url, target.username,
            target.password_enc, target.extra_conf or {},
        )

        inserted = 0
        for raw in adapter.collect_history(since_days):
            if self._upsert_one(target, raw):
                inserted += 1

        target.last_collect = datetime.utcnow()
        self.db.commit()
        return {"inserted_or_updated": inserted, "source": target.db_type}

    def collect_one(self, target_id: int, sql_id: str) -> dict:
        """单条 SQL 最新执行拉取。"""
        target = self.db.get(SqlTarget, target_id)
        if not target:
            raise ValueError(f"target_id={target_id} not found")

        adapter = make_adapter(
            target.db_type, target.conn_url, target.username,
            target.password_enc, target.extra_conf or {},
        )
        raw = adapter.collect_one(sql_id)
        if not raw:
            return {"found": False}
        ok = self._upsert_one(target, raw)
        self.db.commit()
        return {"found": True, "inserted_or_updated": ok}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _upsert_one(self, target: SqlTarget, raw) -> bool:
        """归一化 + upsert 到 fingerprint / snapshot。"""
        norm = normalize(
            raw.sql_text,
            binds=raw.binds,
            command_type=raw.command_type,
        )
        if not norm.canonical_sql:
            return False

        # fingerprint：存在则更新 literal_sql / stmt_type
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
                schema_name=raw.schema_name,
            )
            self.db.add(fp)
        else:
            # 有新的 literal 就更新
            if norm.literal_available and not fp.literal_available:
                fp.literal_sql = norm.literal_sql
                fp.literal_available = True
            fp.updated_at = datetime.utcnow()

        # snapshot：按 (target_id, fingerprint, snap_time) 唯一
        snap_time = raw.snap_time or datetime.utcnow()
        existing = self.db.query(SqlStatSnapshot).filter(
            SqlStatSnapshot.target_id == target.id,
            SqlStatSnapshot.fingerprint == norm.fp,
            SqlStatSnapshot.snap_time == snap_time,
        ).first()
        if existing:
            # 更新（取新值，或累加 executions）
            existing.executions = max(existing.executions or 0, raw.executions or 0)
            existing.elapsed_ms = max(existing.elapsed_ms or 0, raw.elapsed_ms or 0)
            existing.cpu_ms = max(existing.cpu_ms or 0, raw.cpu_ms or 0)
            existing.buffer_gets = max(existing.buffer_gets or 0, raw.buffer_gets or 0)
            existing.rows_processed = max(existing.rows_processed or 0, raw.rows_processed or 0)
            existing.disk_reads = max(existing.disk_reads or 0, raw.disk_reads or 0)
            if raw.optimizer_cost is not None:
                existing.optimizer_cost = raw.optimizer_cost
            if raw.plan_hash is not None:
                existing.plan_hash = raw.plan_hash
            return True

        avg_ms = (
            raw.elapsed_ms / raw.executions if raw.executions else 0.0
        )
        snap = SqlStatSnapshot(
            target_id=target.id,
            fingerprint=norm.fp,
            snap_time=snap_time,
            executions=raw.executions or 0,
            elapsed_ms=raw.elapsed_ms or 0,
            avg_elapsed_ms=avg_ms,
            cpu_ms=raw.cpu_ms or 0,
            buffer_gets=raw.buffer_gets or 0,
            rows_processed=raw.rows_processed or 0,
            disk_reads=raw.disk_reads or 0,
            optimizer_cost=raw.optimizer_cost,
            plan_hash=raw.plan_hash,
            db_snap_id=raw.db_snap_id,
        )
        self.db.add(snap)
        return True
