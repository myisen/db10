"""F2 — Object profile API.

Flow:
  1. Caller POSTs (instance_id + sql)
  2. We run EXPLAIN PLAN INTO PLAN_TABLE → extract deps
  3. Collect table + index + column stats
  4. Return the bundle

Two ways to reach this endpoint from the UI:
  a) Directly paste SQL into the object-profile page
  b) Click a "Get Profile" button on a Top SQL row → carry sql_id / sql_text over
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import decrypt_password
from ..db.session import get_db
from ..models import OracleInstance
from ..oracle.collector_object_profile import collect_object_profile
from ..oracle.connection import OracleConnectionManager
from ..oracle.explain_plan import explain_and_parse
from ..schemas.object_profile import (
    ColumnProfileOut,
    DependencyOut,
    IndexColumnOut,
    IndexProfileOut,
    ObjectProfileOut,
    ObjectProfileRequest,
    TableProfileOut,
)

router = APIRouter(prefix="/api/v1/object-profile", tags=["object-profile"])


async def _get_instance(db: AsyncSession, instance_id: int) -> OracleInstance:
    row = await db.get(OracleInstance, instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    return row


def _ensure_pool(row: OracleInstance) -> OracleConnectionManager:
    pwd = decrypt_password(row.read_password)
    if not pwd:
        raise HTTPException(status_code=500, detail="Cannot decrypt stored password")
    mgr = OracleConnectionManager.get()
    mgr.ensure_pool(
        row.id, host=row.host, port=row.port,
        service_name=row.service_name, user=row.read_user, password=pwd,
    )
    return mgr


@router.post("", response_model=ObjectProfileOut)
async def get_object_profile(
    payload: ObjectProfileRequest,
    db: AsyncSession = Depends(get_db),
):
    row = await _get_instance(db, payload.instance_id)
    mgr = await asyncio.to_thread(_ensure_pool, row)

    # EXPLAIN PLAN → deps
    with mgr.connection(row.id) as conn:
        stmt_id, plan_nodes, deps = await asyncio.to_thread(
            explain_and_parse, conn, payload.sql
        )
        bundle = await asyncio.to_thread(collect_object_profile, conn, deps)

    return ObjectProfileOut(
        instance_id=row.id,
        deps=[DependencyOut(owner=d.owner, name=d.name, object_type=d.object_type) for d in bundle.deps],
        tables=[
            TableProfileOut(
                owner=t.owner, table_name=t.table_name,
                num_rows=t.num_rows, blocks=t.blocks, empty_blocks=t.empty_blocks,
                avg_row_len=t.avg_row_len, pct_free=t.pct_free,
                last_analyzed=t.last_analyzed, partitioned=t.partitioned,
                num_partitions=t.num_partitions, stale=t.stale,
                staleness_reason=t.staleness_reason,
                inserts=t.inserts, updates=t.updates, deletes=t.deletes,
            )
            for t in bundle.tables
        ],
        indexes=[
            IndexProfileOut(
                owner=i.owner, index_name=i.index_name, table_name=i.table_name,
                uniqueness=i.uniqueness, index_type=i.index_type,
                leaf_blocks=i.leaf_blocks, distinct_keys=i.distinct_keys,
                clustering_factor=i.clustering_factor, num_rows=i.num_rows,
                last_analyzed=i.last_analyzed,
                usage_monitoring=i.usage_monitoring, used=i.used,
                column_count=i.column_count,
                columns=[IndexColumnOut(
                    column_name=c.column_name, column_position=c.column_position,
                    descend=c.descend,
                ) for c in i.columns],
            )
            for i in bundle.indexes
        ],
        columns=[
            ColumnProfileOut(
                owner=c.owner, table_name=c.table_name, column_name=c.column_name,
                data_type=c.data_type, data_length=c.data_length, nullable=c.nullable,
                num_nulls=c.num_nulls, num_distinct=c.num_distinct,
                histogram=c.histogram, histogram_buckets=c.histogram_buckets,
            )
            for c in bundle.columns
        ],
    )


# ---------------------------------------------------------------------------
# Quick lookup — tables that *might* be missing indexes
# ---------------------------------------------------------------------------
@router.get("/quick-lookup", response_model=list[TableProfileOut])
async def quick_table_profile(
    instance_id: int,
    owners: str,
    tables: str,
    db: AsyncSession = Depends(get_db),
):
    """Look up a specific set of tables (comma-separated, same length as owners).
    Useful when the caller already knows which tables to inspect — e.g. from
    a Top SQL row that lacks EXPLAIN context.
    """
    row = await _get_instance(db, instance_id)
    mgr = await asyncio.to_thread(_ensure_pool, row)

    owner_list = [o.strip().upper() for o in owners.split(",") if o.strip()]
    table_list = [t.strip().upper() for t in tables.split(",") if t.strip()]
    if len(owner_list) != len(table_list) or not owner_list:
        raise HTTPException(status_code=400, detail="owners and tables must be equal-length comma-separated lists")

    from ..oracle.explain_plan import Dependency
    deps = [Dependency(owner=o, name=t, object_type="TABLE") for o, t in zip(owner_list, table_list)]

    with mgr.connection(row.id) as conn:
        bundle = await asyncio.to_thread(collect_object_profile, conn, deps)

    return [
        TableProfileOut(
            owner=t.owner, table_name=t.table_name,
            num_rows=t.num_rows, blocks=t.blocks, empty_blocks=t.empty_blocks,
            avg_row_len=t.avg_row_len, pct_free=t.pct_free,
            last_analyzed=t.last_analyzed, partitioned=t.partitioned,
            num_partitions=t.num_partitions, stale=t.stale,
            staleness_reason=t.staleness_reason,
        )
        for t in bundle.tables
    ]
