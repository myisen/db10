"""F8 — Optimisation history / audit API.

The ``optimization_records`` table already exists in models.  This module wraps
it with 5 endpoints covering the lifecycle of a single optimization attempt:

  POST   /api/v1/history                    → create (status=pending or running)
  GET    /api/v1/history                    → list with filters + pagination
  GET    /api/v1/history/{id}               → detail — one record with JSON blobs parsed
  PATCH  /api/v1/history/{id}               → patch fields (suggestions_json/status/after_metrics_json/...)
  DELETE /api/v1/history/{id}               → soft-ish delete — actually removes row

Lifecycle:
  pending → running → accepted / rejected / failed / cancelled

The ``diagnose`` and ``sandbox validate`` endpoints call ``create_record()`` /
``finalize_record()`` helpers to auto-persist, but a UI button "Save to History"
can also POST directly with all the JSON blobs assembled client-side.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_db
from ..models import OracleInstance, OptimizationRecord

router = APIRouter(prefix="/api/v1/history", tags=["history"])


# ---------------------------------------------------------------------------
# Pydantic request schemas
# ---------------------------------------------------------------------------

class CreateHistoryRequest(BaseModel):
    instance_id: int
    sql_id: Optional[str] = None
    original_sql: str
    optimized_sql: Optional[str] = None
    findings: Optional[list] = None
    suggestions: Optional[list] = None
    before_metrics: Optional[dict] = None
    after_metrics: Optional[dict] = None
    improvement_pct: Optional[float] = None
    operator: Optional[str] = None
    status: str = Field(default="pending", pattern="^(pending|running|accepted|rejected|failed|cancelled)$")


class PatchHistoryRequest(BaseModel):
    sql_id: Optional[str] = None
    optimized_sql: Optional[str] = None
    findings: Optional[list] = None
    suggestions: Optional[list] = None
    before_metrics: Optional[dict] = None
    after_metrics: Optional[dict] = None
    improvement_pct: Optional[float] = None
    operator: Optional[str] = None
    status: Optional[str] = Field(default=None, pattern="^(pending|running|accepted|rejected|failed|cancelled)$")


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _to_dict(row: OptimizationRecord) -> dict[str, Any]:
    d = {
        "id": row.id,
        "instance_id": row.instance_id,
        "sql_id": row.sql_id,
        "original_sql": row.original_sql,
        "optimized_sql": row.optimized_sql,
        "findings": _parse_json(row.findings_json),
        "suggestions": _parse_json(row.suggestions_json),
        "before_metrics": _parse_json(row.before_metrics_json),
        "after_metrics": _parse_json(row.after_metrics_json),
        "improvement_pct": row.improvement_pct,
        "operator": row.operator,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }
    # Instance name convenience — lazy-load via relationship (might be detached)
    try:
        d["instance_name"] = row.instance.name if row.instance else None
    except Exception:
        d["instance_name"] = None
    return d


def _parse_json(s: Optional[str]) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _json(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Helpers for auto-persist from /diagnose and /sandbox/validate
# ---------------------------------------------------------------------------

async def create_record(
    db: AsyncSession, *,
    instance_id: int,
    original_sql: str,
    sql_id: Optional[str] = None,
    findings: Optional[list] = None,
    suggestions: Optional[list] = None,
    operator: Optional[str] = None,
    status: str = "pending",
) -> OptimizationRecord:
    row = OptimizationRecord(
        instance_id=instance_id,
        sql_id=sql_id,
        original_sql=original_sql,
        findings_json=_json(findings),
        suggestions_json=_json(suggestions),
        operator=operator or "system",
        status=status,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def finalize_record(
    db: AsyncSession,
    row_id: int, *,
    optimized_sql: Optional[str] = None,
    before_metrics: Optional[dict] = None,
    after_metrics: Optional[dict] = None,
    improvement_pct: Optional[float] = None,
    status: Optional[str] = None,
) -> OptimizationRecord:
    row = await db.get(OptimizationRecord, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="History record not found")
    if optimized_sql is not None:
        row.optimized_sql = optimized_sql
    if before_metrics is not None:
        row.before_metrics_json = _json(before_metrics)
    if after_metrics is not None:
        row.after_metrics_json = _json(after_metrics)
    if improvement_pct is not None:
        row.improvement_pct = improvement_pct
    if status is not None:
        row.status = status
        if status in {"accepted", "rejected", "failed", "cancelled"}:
            row.finished_at = datetime.utcnow()
    await db.commit()
    await db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("")
async def create_history(payload: CreateHistoryRequest, db: AsyncSession = Depends(get_db)):
    row = await create_record(
        db,
        instance_id=payload.instance_id,
        original_sql=payload.original_sql,
        sql_id=payload.sql_id,
        findings=payload.findings,
        suggestions=payload.suggestions,
        operator=payload.operator,
        status=payload.status,
    )
    # Patch the remaining optional fields via finalize_record-style set
    if payload.optimized_sql or payload.before_metrics or payload.after_metrics or payload.improvement_pct:
        await finalize_record(
            db, row.id,
            optimized_sql=payload.optimized_sql,
            before_metrics=payload.before_metrics,
            after_metrics=payload.after_metrics,
            improvement_pct=payload.improvement_pct,
        )
    row = await db.get(OptimizationRecord, row.id)
    return _to_dict(row)


@router.get("")
async def list_history(
    instance_id: Optional[int] = Query(None, description="按实例过滤"),
    sql_id: Optional[str] = Query(None, description="按 sql_id 过滤"),
    status: Optional[str] = Query(None, description="按 status 过滤"),
    operator: Optional[str] = Query(None, description="按操作人过滤"),
    keyword: Optional[str] = Query(None, description="SQL 全文模糊搜索"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(OptimizationRecord)
    if instance_id:
        stmt = stmt.where(OptimizationRecord.instance_id == instance_id)
    if sql_id:
        stmt = stmt.where(OptimizationRecord.sql_id == sql_id)
    if status:
        stmt = stmt.where(OptimizationRecord.status == status)
    if operator:
        stmt = stmt.where(OptimizationRecord.operator == operator)
    if keyword:
        stmt = stmt.where(OptimizationRecord.original_sql.ilike(f"%{keyword}%"))
    stmt = stmt.order_by(OptimizationRecord.created_at.desc()).limit(limit).offset(offset)

    rows = (await db.execute(stmt)).scalars().all()
    return [_to_dict(r) for r in rows]


@router.get("/{record_id}")
async def get_history(record_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(OptimizationRecord, record_id)
    if row is None:
        raise HTTPException(status_code=404, detail="History record not found")
    return _to_dict(row)


@router.patch("/{record_id}")
async def patch_history(payload: PatchHistoryRequest, record_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(OptimizationRecord, record_id)
    if row is None:
        raise HTTPException(status_code=404, detail="History record not found")

    data = payload.model_dump(exclude_unset=True)
    if "findings" in data:
        row.findings_json = _json(data.pop("findings"))
    if "suggestions" in data:
        row.suggestions_json = _json(data.pop("suggestions"))
    if "before_metrics" in data:
        row.before_metrics_json = _json(data.pop("before_metrics"))
    if "after_metrics" in data:
        row.after_metrics_json = _json(data.pop("after_metrics"))

    # Flat remaining fields — careful not to overwrite None accidentally
    for k, v in data.items():
        if v is not None:
            setattr(row, k, v)

    if payload.status in {"accepted", "rejected", "failed", "cancelled"}:
        row.finished_at = datetime.utcnow()

    await db.commit()
    await db.refresh(row)
    return _to_dict(row)


@router.delete("/{record_id}")
async def delete_history(record_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(OptimizationRecord, record_id)
    if row is None:
        raise HTTPException(status_code=404, detail="History record not found")
    await db.delete(row)
    await db.commit()
    return {"deleted": record_id}
