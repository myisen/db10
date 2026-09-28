"""F3 — Execution plan API.

Sources, by priority:
  1) sql_id + child_number → V$SQL_PLAN_STATISTICS_ALL (actual, 11g+)
  2) sql_text              → EXPLAIN PLAN INTO PLAN_TABLE (estimated, all versions)
  3) Also return DBMS_XPLAN.DISPLAY / DISPLAY_CURSOR text as a debug-friendly sidecar.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import decrypt_password
from ..db.session import get_db
from ..models import OracleInstance
from ..oracle.collector_execution_plan import (
    ActualPlanNode,
    fetch_actual_plan_from_sql,
    fetch_display_cursor_text,
)
from ..oracle.connection import OracleConnectionManager
from ..oracle.explain_plan import (
    PlanNode,
    dbms_xplan_display,
    explain_and_parse,
)
from ..schemas.object_profile import (
    ActualPlanNodeOut,
    ExecutionPlanOut,
    ExecutionPlanRequest,
    PlanNodeOut,
)

router = APIRouter(prefix="/api/v1/execution-plan", tags=["execution-plan"])


async def _get_row(db: AsyncSession, instance_id: int) -> OracleInstance:
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


# ---------------------------------------------------------------------------
# Helpers: tree indent depth
# ---------------------------------------------------------------------------

def _compute_levels(nodes: list[PlanNode]) -> dict[int, int]:
    """Return plan_id → tree-depth so frontend can indent."""
    depth: dict[int, int] = {}

    def walk(nid: int, level: int) -> None:
        depth[nid] = level
        kids = [n for n in nodes if n.parent_id == nid]
        for k in kids:
            walk(k.id, level + 1)

    # Multiple roots possible (rare, but guard).
    roots = [n for n in nodes if n.parent_id is None]
    for r in roots:
        walk(r.id, 0)
    return depth


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@router.post("", response_model=ExecutionPlanOut)
async def get_execution_plan(
    payload: ExecutionPlanRequest,
    db: AsyncSession = Depends(get_db),
):
    row = await _get_row(db, payload.instance_id)
    mgr = await asyncio.to_thread(_ensure_pool, row)

    estimated: list[PlanNode] = []
    actual: list[ActualPlanNode] = []
    display_text = ""
    source = ""

    with mgr.connection(row.id) as conn:
        # 1) Actual plan from V$SQL (if sql_id present)
        if payload.sql_id and conn.version.has_sql_id:
            actual = await asyncio.to_thread(
                fetch_actual_plan_from_sql, conn, payload.sql_id, payload.child_number
            )
            display_text = await asyncio.to_thread(
                fetch_display_cursor_text, conn,
                sql_id=payload.sql_id, child_number=payload.child_number,
            )
            source = "sql_cursor"

        # 2) Estimated from EXPLAIN PLAN (if sql present)
        if payload.sql:
            stmt_id, estimated, _ = await asyncio.to_thread(
                explain_and_parse, conn, payload.sql
            )
            # DBMS_XPLAN text as debug sidecar
            if not display_text:
                display_text = await asyncio.to_thread(
                    fetch_display_cursor_text, conn,
                    plan_table_name="PLAN_TABLE", statement_id=stmt_id,
                )
            elif not actual:
                actual = []  # already empty
            if source:
                source = "mixed" if actual else "explain_via_cursor"
            else:
                source = "explain"

        # 3) If neither sql_id nor sql → empty response (caller error)
        if not payload.sql_id and not payload.sql:
            raise HTTPException(status_code=400, detail="Must provide sql_id or sql")

    # Build estimated PlanNodeOut
    levels = _compute_levels(estimated)
    estimated_out = [
        PlanNodeOut(
            id=n.id, parent_id=n.parent_id,
            operation=n.operation, options=n.options,
            object_owner=n.object_owner, object_name=n.object_name,
            rows=n.rows, bytes=n.bytes, cost=n.cost, time=n.time,
            access_predicates=n.access_predicates,
            filter_predicates=n.filter_predicates,
            level=levels.get(n.id, 0),
        )
        for n in estimated
    ]

    # Build actual PlanNodeOut — mark bad row estimations
    actual_out = []
    for n in actual:
        bad = False
        if n.deviation is not None:
            if n.e_rows and n.e_rows >= 1 and (n.deviation > 5 or n.deviation < 0.2):
                bad = True
        actual_out.append(ActualPlanNodeOut(
            id=n.id, parent_id=n.parent_id,
            operation=n.operation, options=n.options,
            object_owner=n.object_owner, object_name=n.object_name,
            e_rows=n.e_rows, a_rows=n.a_rows,
            buffers=n.buffers, reads=n.reads, temp_spc=n.temp_spc,
            deviation=n.deviation,
            access_predicates=n.access_predicates,
            filter_predicates=n.filter_predicates,
            row_estimation_bad=bad,
        ))

    ver_str = mgr.get_version(row.id)
    return ExecutionPlanOut(
        instance_id=row.id,
        sql_text=payload.sql,
        estimated_plan=estimated_out,
        actual_plan=actual_out,
        display_text=display_text or None,
        source=source or "explain",
        sql_id=payload.sql_id,
        child_number=payload.child_number,
        oracle_version=f"{ver_str.major}.{ver_str.minor}" if ver_str and ver_str.major else None,
    )
