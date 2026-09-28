"""F4 — Diagnosis API.

One endpoint runs the full chain:
  1. Load OracleInstance
  2. ensure_pool + connect
  3. EXPLAIN PLAN → PlanNode + Dependency
  4. Collect object profile (F2)
  5. Collect execution plan (F3 — actual from V$SQL if sql_id provided)
  6. Optionally fetch TopSQLRow (V$SQL metrics)
  7. Build RuleContext → run_all_rules → return DiagnosisReport

This is the "一站式" entry point that the UI calls when user pastes SQL and clicks "一键诊断".
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import decrypt_password
from ..db.session import get_db
from ..models import OracleInstance
from ..oracle.collector_object_profile import collect_object_profile
from ..oracle.collector_top_sql import collect_realtime_top_sql
from ..oracle.connection import OracleConnectionManager
from ..oracle.explain_plan import explain_and_parse
from ..rules import RuleContext, run_all_rules, summarize
from ..rules.base import Severity
from ..schemas import DiagnosisReport, Finding
from ..suggestions import generate_all

router = APIRouter(prefix="/api/v1/diagnose", tags=["diagnose"])


async def _load_instance(db: AsyncSession, instance_id: int) -> OracleInstance:
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
# All-in-one diagnose
# ---------------------------------------------------------------------------

@router.post("", response_model=DiagnosisReport)
async def diagnose(
    payload: dict,  # Loose — {instance_id, sql?, sql_id?}
    db: AsyncSession = Depends(get_db),
):
    instance_id: int = payload.get("instance_id")
    sql_text: str | None = payload.get("sql")
    sql_id: str | None = payload.get("sql_id")

    if not instance_id:
        raise HTTPException(status_code=400, detail="instance_id required")
    if not sql_text and not sql_id:
        raise HTTPException(status_code=400, detail="Provide sql or sql_id")

    row = await _load_instance(db, instance_id)
    mgr = await asyncio.to_thread(_ensure_pool, row)

    ctx = None
    findings = []

    try:
        with mgr.connection(row.id) as conn:
            ver = mgr.get_version(row.id)

            estimated_plan = []
            actual_plan = []
            deps = []

            # F2+F3 — EXPLAIN PLAN
            if sql_text:
                stmt_id, estimated_plan, deps = await asyncio.to_thread(
                    explain_and_parse, conn, sql_text
                )

            # F3 — actual plan (if sql_id provided and 11g+)
            if sql_id and ver and ver.has_sql_id:
                from ..oracle.collector_execution_plan import fetch_actual_plan_from_sql
                actual_plan = await asyncio.to_thread(
                    fetch_actual_plan_from_sql, conn, sql_id, payload.get("child_number", 0)
                )

            # F2 — object profile
            tables = []
            indexes = []
            columns = []
            if deps:
                bundle = await asyncio.to_thread(collect_object_profile, conn, deps)
                tables = bundle.tables
                indexes = bundle.indexes
                columns = bundle.columns

            # TopSQL row — try V$SQL by sql_id (optional)
            top_sql_row = None
            if sql_id and ver and ver.has_sql_id:
                try:
                    topsql = await asyncio.to_thread(
                        collect_realtime_top_sql, conn, "elapsed", 200
                    )
                    match = next((r for r in topsql if r.sql_id == sql_id), None)
                    if match:
                        top_sql_row = match
                except Exception:
                    pass

            # Build RuleContext & run engine
            ctx = RuleContext.build(
                instance_id=row.id,
                oracle_version=ver,
                estimated_plan=estimated_plan,
                actual_plan=actual_plan,
                tables=tables, indexes=indexes, columns=columns,
                top_sql_row=top_sql_row,
                sql_text=sql_text, sql_id=sql_id,
            )
            findings = run_all_rules(ctx)
            suggestions = generate_all(findings, ctx)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Diagnosis failed: {e}") from e

    return {
        "instance_id": instance_id,
        "sql_id": sql_id,
        "findings": [
            Finding(
                rule_id=f.rule_id, rule_name=f.rule_name,
                severity=f.severity.label,
                description=f.description,
                object_name=f.object_name,
                evidence=f.evidence,
            ).model_dump()
            for f in findings
        ],
        "suggestions": [s.to_dict() for s in suggestions],
        "summary": summarize(findings),
    }


# ---------------------------------------------------------------------------
# Quick probe — registered rules & their severity
# ---------------------------------------------------------------------------
@router.get("/rules")
async def list_rules():
    from ..rules.base import get_all_rules
    return [
        {
            "rule_id": r.rule_id,
            "rule_name": r.rule_name,
            "severity": r.severity.label,
            "severity_code": r.severity.value,
            "description": r.description,
        }
        for r in get_all_rules()
    ]
