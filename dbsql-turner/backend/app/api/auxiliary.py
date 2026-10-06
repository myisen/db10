"""F9 — Auxiliary diagnostics API.

Three read-only endpoints — all use the **read-only 连接池**（read_user）not sandbox_user,
因为 V$LOCK / V$SESSION_WAIT / V$PARAMETER 这些视图一般 SELECT_CATALOG_ROLE 就能读。

所有 collector 都是 best-effort —— 某个视图 403 时这个端点返回空列表 + warning 文本，
不会让整条链路 500。
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import decrypt_password
from ..db.session import get_db
from ..models import OracleInstance
from ..oracle.collector_auxiliary import (
    collect_key_parameters,
    collect_lock_chain,
    collect_top_waits,
)
from ..oracle.connection import OracleConnectionManager

router = APIRouter(prefix="/api/v1/aux", tags=["auxiliary"])


async def _get_instance(db: AsyncSession, instance_id: int) -> OracleInstance:
    row = await db.get(OracleInstance, instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    return row


def _run_readonly(instance: OracleInstance, fn) -> object:
    """Run ``fn(conn)`` under the instance's read-only connection pool."""
    pwd = decrypt_password(instance.read_password) if instance.read_password else None
    if not pwd:
        raise HTTPException(status_code=500, detail="Read password decrypt failed")
    mgr = OracleConnectionManager.get()
    mgr.ensure_pool(
        instance.id, host=instance.host, port=instance.port,
        service_name=instance.service_name, user=instance.read_user, password=pwd,
    )
    try:
        with mgr.connection(instance.id) as conn:
            return fn(conn)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Oracle access error: {e}")


@router.get("/lock-chain")
async def get_lock_chain(instance_id: int, db: AsyncSession = Depends(get_db)):
    instance = await _get_instance(db, instance_id)
    nodes = await asyncio.to_thread(_run_readonly, instance, collect_lock_chain)
    return {
        "instance_id": instance_id,
        "lock_waiters": [n.to_dict() for n in nodes],
        "hint": "只显示正在等锁的会话（request > 0）。如果返回空表示当前无等待锁。",
    }


@router.get("/top-waits")
async def get_top_waits(instance_id: int, top_n: int = 5, db: AsyncSession = Depends(get_db)):
    instance = await _get_instance(db, instance_id)
    waits = await asyncio.to_thread(
        _run_readonly, instance, lambda conn: collect_top_waits(conn, top_n=top_n)
    )
    return {
        "instance_id": instance_id,
        "top_waits": [w.to_dict() for w in waits],
    }


@router.get("/parameters")
async def get_key_parameters(
    instance_id: int,
    names: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    instance = await _get_instance(db, instance_id)
    want = [n.strip() for n in names.split(",")] if names else None
    params = await asyncio.to_thread(
        _run_readonly, instance,
        lambda conn: collect_key_parameters(conn, names=want),
    )
    return {
        "instance_id": instance_id,
        "parameters": [p.to_dict() for p in params],
        "count": len(params),
    }


# ---------------------------------------------------------------------------
# All-in-one snapshot — 一次返回三样，给前端侧栏用
# ---------------------------------------------------------------------------

@router.get("/snapshot")
async def get_full_snapshot(instance_id: int, db: AsyncSession = Depends(get_db)):
    """锁 + 等待事件 + 关键参数 一次性拉取 —— 侧栏一个 HTTP 请求搞定。"""
    instance = await _get_instance(db, instance_id)

    # 三个 collector 都走同一个 connection manager，各自 best-effort
    def _all(conn):
        locks = collect_lock_chain(conn)
        waits = collect_top_waits(conn, top_n=5)
        params = collect_key_parameters(conn)
        return locks, waits, params

    locks, waits, params = await asyncio.to_thread(_run_readonly, instance, _all)
    return {
        "instance_id": instance_id,
        "lock_waiters": [n.to_dict() for n in locks],
        "top_waits": [w.to_dict() for w in waits],
        "parameters": [p.to_dict() for p in params],
        "snapshot_ms": __import__("time").time() * 1000,
    }
