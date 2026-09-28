"""F1 — Top SQL API.

Three endpoints:
- ``GET /api/v1/top-sql/realtime`` — V$SQL / V$SQLAREA pull, return immediately.
- ``GET /api/v1/top-sql/awr``      — AWR historical pull over a look-back window.
- ``POST /api/v1/top-sql/snapshot`` — Run realtime + optional AWR, persist results
  to ``top_sql_snapshots`` so F2/F3 can consume them later.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import decrypt_password
from ..db.session import get_db
from ..models import OracleInstance, TopSQLSnapshot
from ..oracle.collector_top_sql import (
    collect_awr_top_sql,
    collect_realtime_top_sql,
)
from ..oracle.connection import OracleConnectionManager

router = APIRouter(prefix="/api/v1/top-sql", tags=["top-sql"])


METRIC_CHOICES = ["elapsed", "cpu", "logical_reads", "physical_reads", "executions"]


def _get_oracle_manager_instance(db: AsyncSession, instance_id: int) -> tuple[OracleInstance, OracleConnectionManager]:
    row = db.get(OracleInstance, instance_id) if False else None
    # Above was a placeholder — real call via sync is tricky in async; use execute instead.
    return row  # pragma: no cover - unreachable, replaced below


async def _load_instance(db: AsyncSession, instance_id: int) -> OracleInstance:
    row = await db.get(OracleInstance, instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    return row


def _ensure_pool_for(db: AsyncSession, row: OracleInstance) -> OracleConnectionManager:
    pwd = decrypt_password(row.read_password)
    if not pwd:
        raise HTTPException(status_code=500, detail="Cannot decrypt stored Oracle password")
    mgr = OracleConnectionManager.get()
    # ensure_pool is blocking — run in thread at call sites.
    mgr.ensure_pool(
        row.id,
        host=row.host,
        port=row.port,
        service_name=row.service_name,
        user=row.read_user,
        password=pwd,
    )
    return mgr


# ---------------------------------------------------------------------------
# Realtime
# ---------------------------------------------------------------------------

@router.get("/realtime")
async def top_sql_realtime(
    instance_id: int = Query(...),
    metric: str = Query(default="elapsed", pattern="^(elapsed|cpu|logical_reads|physical_reads|executions)$"),
    limit: int = Query(default=20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    row = await _load_instance(db, instance_id)
    mgr = await asyncio.to_thread(_ensure_pool_for, db, row)
    # Collect.
    with mgr.connection(instance_id) as conn:
        results = await asyncio.to_thread(
            collect_realtime_top_sql, conn, metric, limit
        )
    return [
        {
            "sql_id": r.sql_id,
            "hash_value": r.hash_value,
            "child_number": r.child_number,
            "sql_text": r.sql_text,
            "executions": r.executions,
            "elapsed_time_us": r.elapsed_time_us,
            "cpu_time_us": r.cpu_time_us,
            "buffer_gets": r.buffer_gets,
            "disk_reads": r.disk_reads,
            "module": r.module,
            "source": r.source,
            "avg_elapsed_ms": (
                round((r.elapsed_time_us or 0) / (r.executions or 1) / 1000, 2)
                if r.elapsed_time_us is not None and r.executions
                else None
            ),
        }
        for r in results
    ]


# ---------------------------------------------------------------------------
# AWR historical
# ---------------------------------------------------------------------------

@router.get("/awr")
async def top_sql_awr(
    instance_id: int = Query(...),
    metric: str = Query(default="elapsed", pattern="^(elapsed|cpu|logical_reads|physical_reads|executions)$"),
    limit: int = Query(default=20, ge=1, le=200),
    hours: int = Query(default=1, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
):
    row = await _load_instance(db, instance_id)
    mgr = await asyncio.to_thread(_ensure_pool_for, db, row)
    with mgr.connection(instance_id) as conn:
        results = await asyncio.to_thread(
            collect_awr_top_sql, conn, hours, metric, limit
        )
    return [
        {
            "sql_id": r.sql_id,
            "hash_value": r.hash_value,
            "sql_text": r.sql_text,
            "executions": r.executions,
            "elapsed_time_us": r.elapsed_time_us,
            "cpu_time_us": r.cpu_time_us,
            "buffer_gets": r.buffer_gets,
            "disk_reads": r.disk_reads,
            "source": r.source,
        }
        for r in results
    ]


# ---------------------------------------------------------------------------
# Snapshot & persist
# ---------------------------------------------------------------------------

@router.post("/snapshot")
async def snapshot_top_sql(
    instance_id: int = Query(...),
    metric: str = Query(default="elapsed", pattern="^(elapsed|cpu|logical_reads|physical_reads|executions)$"),
    limit: int = Query(default=50, ge=1, le=500),
    include_awr: bool = Query(default=False),
    awr_hours: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
):
    """Capture Top SQL right now and persist it to ``top_sql_snapshots``.

    Later collectors (F2 object profile, F3 execution plan) can pick up these
    rows by snapshot_id rather than re-querying V$SQL directly.
    """
    row = await _load_instance(db, instance_id)
    mgr = await asyncio.to_thread(_ensure_pool_for, db, row)

    captured: list = []
    with mgr.connection(instance_id) as conn:
        realtime = await asyncio.to_thread(
            collect_realtime_top_sql, conn, metric, limit
        )
        captured.extend(realtime)
        if include_awr:
            captured.extend(
                await asyncio.to_thread(
                    collect_awr_top_sql, conn, awr_hours, metric, limit
                )
            )

    now = datetime.utcnow()
    for rank, r in enumerate(captured, start=1):
        snap = TopSQLSnapshot(
            instance_id=instance_id,
            snapshot_time=now,
            sql_id=r.sql_id,
            hash_value=r.hash_value,
            child_number=r.child_number,
            sql_text=r.sql_text,
            executions=r.executions,
            elapsed_time=r.elapsed_time_us,
            cpu_time=r.cpu_time_us,
            buffer_gets=r.buffer_gets,
            disk_reads=r.disk_reads,
            module=r.module,
            first_load_time=r.first_load_time,
            metric_rank=rank,
        )
        db.add(snap)
    await db.commit()
    logger.info(
        f"Top SQL snapshot instance={instance_id} rows={len(captured)} metric={metric}"
    )

    return {
        "snapshot_time": now.isoformat(),
        "rows_captured": len(captured),
        "metric": metric,
    }


# ---------------------------------------------------------------------------
# List persisted snapshots for an instance
# ---------------------------------------------------------------------------

@router.get("/snapshots")
async def list_snapshots(
    instance_id: int = Query(...),
    limit: int = Query(default=20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    await _load_instance(db, instance_id)
    result = await db.execute(
        select(TopSQLSnapshot)
        .where(TopSQLSnapshot.instance_id == instance_id)
        .order_by(TopSQLSnapshot.snapshot_time.desc(), TopSQLSnapshot.metric_rank.asc())
        .limit(limit)
    )
    rows = result.scalars().all()
    return [
        {
            "id": s.id,
            "snapshot_time": s.snapshot_time.isoformat(),
            "sql_id": s.sql_id,
            "hash_value": s.hash_value,
            "sql_text": s.sql_text,
            "executions": s.executions,
            "elapsed_time": s.elapsed_time,
            "buffer_gets": s.buffer_gets,
            "disk_reads": s.disk_reads,
            "metric_rank": s.metric_rank,
        }
        for s in rows
    ]
