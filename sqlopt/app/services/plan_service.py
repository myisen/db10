"""执行计划服务：拉取 + 缓存 + 管理。

职责：
  1) explain 新 SQL：归一化 → 查 fingerprint → adapter.explain() → upsert sql_plan
  2) 查 fingerprint 的所有已缓存 plan
  3) 根据历史 snapshot 的 plan_hash 关联已缓存 plan
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..adapters.factory import make_adapter
from ..models.models import SqlFingerprint, SqlPlan, SqlTarget
from .normalizer import normalize


class PlanService:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # 核心：拉一条 SQL 的执行计划（在线 explain）
    # ------------------------------------------------------------------
    def explain_and_cache(
        self,
        target_id: int,
        sql: str,
        source: str = "manual",
    ) -> dict:
        target = self.db.get(SqlTarget, target_id)
        if not target:
            raise ValueError(f"target_id={target_id} not found")

        # 归一化，拿 fingerprint
        norm = normalize(sql)

        # fingerprint 入库（如果是新 SQL）
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

        # 先查是否已缓存过同样的 (fp, plan_hash=None 或已知)
        # （plan_hash 在解释前不知道，先按 fp 粗筛）
        existing = self.db.query(SqlPlan).filter(
            SqlPlan.target_id == target_id,
            SqlPlan.fingerprint == norm.fp,
        ).first()

        adapter = make_adapter(
            target.db_type, target.conn_url, target.username,
            target.password_enc, target.extra_conf or {},
        )
        try:
            raw = adapter.explain(norm.literal_sql or norm.canonical_sql or sql)
        except Exception as e:
            raw = {
                "plan_hash": None,
                "raw_tree": [{"error": str(e)}],
                "formatted_text": f"explain 执行失败: {e}",
                "db_type": target.db_type,
            }

        plan_hash = raw.get("plan_hash")
        if plan_hash is not None:
            existing = self.db.query(SqlPlan).filter(
                SqlPlan.target_id == target_id,
                SqlPlan.fingerprint == norm.fp,
                SqlPlan.plan_hash == plan_hash,
            ).first()

        if existing is None:
            existing = SqlPlan(
                target_id=target_id,
                fingerprint=norm.fp,
                plan_hash=plan_hash,
                raw_tree=raw.get("raw_tree"),
                formatted_text=raw.get("formatted_text", ""),
                db_type=raw.get("db_type", target.db_type),
                source=source,
            )
            self.db.add(existing)
        else:
            # 更新最新内容
            existing.raw_tree = raw.get("raw_tree")
            existing.formatted_text = raw.get("formatted_text", "")
            if plan_hash:
                existing.plan_hash = plan_hash
            existing.source = source

        self.db.commit()
        self.db.refresh(existing)

        return {
            "fingerprint": norm.fp,
            "plan_id": existing.id,
            "plan_hash": existing.plan_hash,
            "db_type": existing.db_type,
            "raw_tree": existing.raw_tree or [],
            "formatted_text": existing.formatted_text or "",
            "cached": False,
        }

    # ------------------------------------------------------------------
    # 查缓存的 plan 列表
    # ------------------------------------------------------------------
    def list_cached(self, fingerprint: str, target_id: int | None = None) -> list[dict]:
        q = self.db.query(SqlPlan).filter(SqlPlan.fingerprint == fingerprint)
        if target_id is not None:
            q = q.filter(SqlPlan.target_id == target_id)
        q = q.order_by(SqlPlan.created_at.desc())

        out = []
        for p in q.all():
            out.append({
                "plan_id": p.id,
                "target_id": p.target_id,
                "fingerprint": p.fingerprint,
                "plan_hash": p.plan_hash,
                "db_type": p.db_type,
                "source": p.source,
                "raw_tree": p.raw_tree or [],
                "formatted_text": p.formatted_text or "",
                "created_at": p.created_at.isoformat() if p.created_at else None,
            })
        return out

    def delete(self, plan_id: int) -> bool:
        p = self.db.get(SqlPlan, plan_id)
        if not p:
            return False
        self.db.delete(p)
        self.db.commit()
        return True
