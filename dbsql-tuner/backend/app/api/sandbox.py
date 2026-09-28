"""F6 — 沙箱执行 + F7 — 量化对比 API.

端点:
  POST /api/v1/sandbox/exec     → 单次 SQL 在沙箱账号执行
  POST /api/v1/sandbox/validate → before / after 两次 + 自动算 delta

所有端点都走 :
  1. instance 元数据（host / port / service / sandbox_user / sandbox_password）
  2. ``sandbox_executor.execute_sandbox`` —— 内含 SQL 安全校验 + 指标抓取

前置条件：oracle_instances 必须配置 sandbox_user / sandbox_password，
          否则返回 400 "实例未配置沙箱账号"。
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import decrypt_password
from ..db.session import get_db
from ..models import OracleInstance
from ..oracle.sandbox_executor import execute_sandbox, SandboxResult
from ..oracle.sandbox_security import validate_sql

router = APIRouter(prefix="/api/v1/sandbox", tags=["sandbox"])


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class ExecRequest(BaseModel):
    instance_id: int
    sql: str = Field(min_length=1, description="要在沙箱执行的 SQL（只能 SELECT / EXPLAIN PLAN / ALTER SESSION）")
    max_rows: int = Field(default=100_000, ge=1, le=1_000_000, description="最大 fetch 行数上限")


class ValidateRequest(BaseModel):
    instance_id: int
    before_sql: str = Field(..., description="原始 SQL")
    after_sql: str = Field(..., description="优化后 SQL（可以是加 Hint / 改写后的版本）")
    max_rows: int = 100_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _load_instance(db: AsyncSession, instance_id: int) -> OracleInstance:
    row = await db.get(OracleInstance, instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    if not row.sandbox_user or not row.sandbox_password:
        raise HTTPException(status_code=400, detail="实例未配置沙箱账号（sandbox_user / sandbox_password）")
    return row


def _run_sandbox_sync(row: OracleInstance, sql: str, max_rows: int) -> SandboxResult:
    pwd = decrypt_password(row.sandbox_password) if row.sandbox_password else None
    if not pwd:
        return SandboxResult(error="沙箱密码解密失败")
    return execute_sandbox(
        host=row.host, port=row.port, service_name=row.service_name,
        user=row.sandbox_user, password=pwd, sql_text=sql,
        oracle_version=row.oracle_version, max_rows=max_rows,
    )


def _delta_dict(before: dict, after: dict) -> dict:
    """Compare two metric dicts — returns before/after/delta for numeric keys.

    delta 的正负语义：elapsed_ms 下降是好事（负 delta → 改进）；
                   rows_returned 应该相等（正 delta 是错误，负 delta 也是错误）。
    """
    metrics = {k for k in (before.keys() | after.keys()) if isinstance(before.get(k), (int, float)) or isinstance(after.get(k), (int, float))}
    out = {}
    for k in metrics:
        b = before.get(k)
        a = after.get(k)
        if b is None or a is None:
            out[k] = {"before": b, "after": a, "delta": None, "improved": None}
            continue
        delta = a - b
        pct = (delta / b * 100) if b != 0 else None
        # 判断改进方向：elapsed_ms / buffer_gets / disk_reads 都是越小越好
        lower_is_better = k in {"elapsed_ms", "buffer_gets", "disk_reads", "temp_spc_mb"}
        if lower_is_better and pct is not None:
            improved = pct < -1  # 至少改进 1% 才算
        elif k in {"actual_rows", "estimated_rows"} and pct is not None:
            # A-Rows 应该稳定（before/after 不应该差太多）
            improved = abs(pct) < 10
        else:
            improved = None
        out[k] = {
            "before": b, "after": a,
            "delta": round(delta, 4) if isinstance(delta, float) else delta,
            "pct_change": round(pct, 2) if pct is not None else None,
            "improved": improved,
        }
    return out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/exec")
async def sandbox_exec(payload: ExecRequest, db: AsyncSession = Depends(get_db)):
    """Run one SQL in sandbox and return metrics.

    静态安全校验 —— 任何 DDL / DML / ALTER SYSTEM / DBMS_SQL 都会被拦截并返回
    ``detail: "安全校验失败: xxx"``，不会尝试连 Oracle。
    """
    vr = validate_sql(payload.sql)
    if not vr.ok:
        raise HTTPException(status_code=400, detail=f"SQL 被禁止: {vr.reason}")

    row = await _load_instance(db, payload.instance_id)
    result = await asyncio.to_thread(_run_sandbox_sync, row, payload.sql, payload.max_rows)

    resp = result.to_dict()
    resp["estimated_plan_text"] = result.estimated_plan_text
    resp["plan_text"] = result.plan_text
    if result.error:
        # 安全校验/连接/执行错误都返回 500（因为用户无法通过参数修正，是环境/权限问题）
        raise HTTPException(status_code=500, detail=result.error)
    return resp


@router.post("/validate")
async def sandbox_validate(payload: ValidateRequest, db: AsyncSession = Depends(get_db)):
    """Run before and after SQL, return metrics + delta + improved flags.

    先做静态安全校验，任何一条 SQL 被禁止都直接返回 400，两条都不过不尝试执行。
    """
    vr_b = validate_sql(payload.before_sql)
    if not vr_b.ok:
        raise HTTPException(status_code=400, detail=f"before_sql 被禁止: {vr_b.reason}")
    vr_a = validate_sql(payload.after_sql)
    if not vr_a.ok:
        raise HTTPException(status_code=400, detail=f"after_sql 被禁止: {vr_a.reason}")

    row = await _load_instance(db, payload.instance_id)

    before, after = await asyncio.gather(
        asyncio.to_thread(_run_sandbox_sync, row, payload.before_sql, payload.max_rows),
        asyncio.to_thread(_run_sandbox_sync, row, payload.after_sql, payload.max_rows),
    )

    # 如果其中一条失败，整条验证无效 —— 把 error 抛给前端
    if before.error:
        raise HTTPException(status_code=500, detail=f"before_sql 执行失败: {before.error}")
    if after.error:
        raise HTTPException(status_code=500, detail=f"after_sql 执行失败: {after.error}")

    before_d = before.to_dict()
    after_d = after.to_dict()
    delta = _delta_dict(before_d, after_d)

    # 综合改进判断
    improved_flags = [v["improved"] for v in delta.values() if v["improved"] is not None]
    overall = None
    if improved_flags:
        overall = sum(improved_flags) / len(improved_flags) > 0.5  # 过半数关键指标改进

    return {
        "before": before_d,
        "after": after_d,
        "before_estimated_plan": before.estimated_plan_text,
        "after_estimated_plan": after.estimated_plan_text,
        "delta": delta,
        "overall_improved": overall,
        "overall_summary": (
            f"✅ 优化后整体更快（{len([k for k, v in delta.items() if v.get('improved')])} 项指标改进）"
            if overall else
            ("⚠️ 优化后没有显著改进" if overall is False else "— 改进不明确")
        ),
    }
