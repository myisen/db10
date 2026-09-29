"""REST API 路由。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..adapters.factory import make_adapter
from ..database import get_db
from ..models.models import (
    SqlExecHistory,
    SqlFingerprint,
    SqlJob,
    SqlPolicy,
    SqlStatSnapshot,
    SqlTarget,
)
from ..schemas import (
    CollectIn,
    CollectOneIn,
    DashboardOut,
    ExecHistoryOut,
    ExecIn,
    ExecOut,
    JobIn,
    JobOut,
    PolicyIn,
    PolicyOut,
    SqlDetailOut,
    SqlQueryIn,
    SqlQueryOut,
    SqlRow,
    TargetIn,
    TargetOut,
    TestConnOut,
)
from ..services.collector import CollectorService
from ..services.crypto import encrypt
from ..services.dashboard import dashboard_summary
from ..services.executor import ExecutorService
from ..services.scheduler import register_job, remove_job, run_job_now


router = APIRouter()


# ---------------------------------------------------------------------------
# Target 管理
# ---------------------------------------------------------------------------
@router.get("/targets", response_model=list[TargetOut])
def list_targets(db: Session = Depends(get_db)):
    return db.query(SqlTarget).filter(SqlTarget.enabled == True).all()


@router.post("/targets", response_model=TargetOut)
def create_target(inp: TargetIn, db: Session = Depends(get_db)):
    existing = db.query(SqlTarget).filter(SqlTarget.name == inp.name).first()
    if existing:
        raise HTTPException(400, "name already exists")
    t = SqlTarget(
        name=inp.name, db_type=inp.db_type, conn_url=inp.conn_url,
        username=inp.username, password_enc=encrypt(inp.password),
        extra_conf=inp.extra_conf or {},
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


@router.delete("/targets/{target_id}")
def delete_target(target_id: int, db: Session = Depends(get_db)):
    t = db.get(SqlTarget, target_id)
    if not t:
        raise HTTPException(404)
    t.enabled = False
    db.commit()
    return {"ok": True}


@router.post("/targets/{target_id}/test", response_model=TestConnOut)
def test_target(target_id: int, db: Session = Depends(get_db)):
    t = db.get(SqlTarget, target_id)
    if not t:
        raise HTTPException(404)
    adapter = make_adapter(
        t.db_type, t.conn_url, t.username, t.password_enc, t.extra_conf or {},
    )
    ok, msg = adapter.test_connection()
    return TestConnOut(ok=ok, message=msg)


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------
@router.post("/collect")
def collect(inp: CollectIn, db: Session = Depends(get_db)):
    cs = CollectorService(db=db)
    try:
        return cs.collect(inp.target_id, inp.since_days)
    finally:
        cs.close()


@router.post("/collect_one")
def collect_one(inp: CollectOneIn, db: Session = Depends(get_db)):
    cs = CollectorService(db=db)
    try:
        return cs.collect_one(inp.target_id, inp.sql_id)
    finally:
        cs.close()


# ---------------------------------------------------------------------------
# 历史 SQL 查询
# ---------------------------------------------------------------------------
@router.post("/sql/history", response_model=SqlQueryOut)
def query_history(inp: SqlQueryIn, db: Session = Depends(get_db)):
    # 用 fingerprint 表 LEFT JOIN snapshot 做聚合
    q = db.query(
        SqlFingerprint.fingerprint,
        SqlFingerprint.canonical_sql,
        SqlFingerprint.literal_sql,
        SqlFingerprint.literal_available,
        SqlFingerprint.stmt_type,
        SqlFingerprint.schema_name,
        func.coalesce(func.sum(SqlStatSnapshot.executions), 0).label("total_exec"),
        func.coalesce(func.avg(SqlStatSnapshot.avg_elapsed_ms), 0).label("avg_ms"),
        func.max(SqlStatSnapshot.snap_time).label("last_exec"),
    ).outerjoin(
        SqlStatSnapshot, SqlFingerprint.fingerprint == SqlStatSnapshot.fingerprint,
    )

    if inp.target_id is not None:
        q = q.filter(SqlStatSnapshot.target_id == inp.target_id)
    if inp.stmt_type:
        q = q.filter(SqlFingerprint.stmt_type == inp.stmt_type)
    if inp.only_with_literal:
        q = q.filter(SqlFingerprint.literal_available == True)
    if inp.min_executions > 0:
        q = q.having(func.coalesce(func.sum(SqlStatSnapshot.executions), 0) >= inp.min_executions)
    if inp.keyword:
        kw = f"%{inp.keyword}%"
        q = q.filter(or_(
            SqlFingerprint.canonical_sql.ilike(kw),
            SqlFingerprint.literal_sql.ilike(kw),
        ))
    if inp.time_from:
        q = q.filter(SqlStatSnapshot.snap_time >= inp.time_from)
    if inp.time_to:
        q = q.filter(SqlStatSnapshot.snap_time <= inp.time_to)

    q = q.group_by(
        SqlFingerprint.fingerprint, SqlFingerprint.canonical_sql,
        SqlFingerprint.literal_sql, SqlFingerprint.literal_available,
        SqlFingerprint.stmt_type, SqlFingerprint.schema_name,
    ).order_by(
        func.coalesce(func.avg(SqlStatSnapshot.avg_elapsed_ms), 0).desc(),
    )

    total = q.count()
    items = q.offset((inp.page - 1) * inp.page_size).limit(inp.page_size).all()

    rows = []
    for r in items:
        rows.append(SqlRow(
            fingerprint=r.fingerprint,
            canonical_sql=r.canonical_sql,
            literal_sql=r.literal_sql,
            literal_available=bool(r.literal_available),
            stmt_type=r.stmt_type,
            schema_name=r.schema_name,
            total_executions=int(r.total_exec or 0),
            avg_elapsed_ms=float(r.avg_ms or 0),
            last_exec_time=r.last_exec,
        ))
    return SqlQueryOut(total=total, page=inp.page, page_size=inp.page_size, items=rows)


@router.get("/sql/{fingerprint}", response_model=SqlDetailOut)
def sql_detail(fingerprint: str, target_id: Optional[int] = Query(None), db: Session = Depends(get_db)):
    fp = db.get(SqlFingerprint, fingerprint)
    if not fp:
        raise HTTPException(404)
    sq = db.query(SqlStatSnapshot).filter(SqlStatSnapshot.fingerprint == fingerprint)
    if target_id is not None:
        sq = sq.filter(SqlStatSnapshot.target_id == target_id)
    snaps = []
    for s in sq.order_by(SqlStatSnapshot.snap_time.desc()).limit(200).all():
        snaps.append({
            "snap_time": s.snap_time.isoformat() if s.snap_time else None,
            "target_id": s.target_id,
            "executions": s.executions,
            "elapsed_ms": s.elapsed_ms,
            "avg_elapsed_ms": s.avg_elapsed_ms,
            "cpu_ms": s.cpu_ms,
            "buffer_gets": s.buffer_gets,
            "rows_processed": s.rows_processed,
            "plan_hash": s.plan_hash,
        })
    return SqlDetailOut(
        fingerprint=fp.fingerprint,
        canonical_sql=fp.canonical_sql,
        literal_sql=fp.literal_sql,
        literal_available=fp.literal_available,
        stmt_type=fp.stmt_type,
        schema_name=fp.schema_name,
        snapshots=snaps,
    )


# ---------------------------------------------------------------------------
# 在线执行
# ---------------------------------------------------------------------------
@router.post("/exec", response_model=ExecOut)
def exec_sql(inp: ExecIn, db: Session = Depends(get_db)):
    es = ExecutorService(db=db)
    try:
        result = es.execute(inp.target_id, inp.sql_text)
    finally:
        es.close()
    if result.get("status") == "blocked":
        # 200 OK，但 status 字段说明被挡；用 200 让前端友好处理
        pass
    return ExecOut(**result)


# ---------------------------------------------------------------------------
# 执行历史
# ---------------------------------------------------------------------------
@router.get("/exec/history", response_model=list[ExecHistoryOut])
def exec_history(target_id: Optional[int] = Query(None),
                 limit: int = Query(100), db: Session = Depends(get_db)):
    q = db.query(SqlExecHistory).order_by(SqlExecHistory.exec_at.desc())
    if target_id is not None:
        q = q.filter(SqlExecHistory.target_id == target_id)
    return q.limit(limit).all()


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------
@router.get("/policies", response_model=list[PolicyOut])
def list_policies(db: Session = Depends(get_db)):
    # 确保默认策略已写入
    from ..services.policy import PolicyService
    PolicyService(db)
    return db.query(SqlPolicy).order_by(SqlPolicy.id).all()


@router.post("/policies", response_model=PolicyOut)
def create_policy(inp: PolicyIn, db: Session = Depends(get_db)):
    p = SqlPolicy(
        name=inp.name, target_id=inp.target_id,
        stmt_type=inp.stmt_type, pattern_regex=inp.pattern_regex,
        allow_exec=inp.allow_exec,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@router.put("/policies/{policy_id}", response_model=PolicyOut)
def update_policy(policy_id: int, inp: PolicyIn, db: Session = Depends(get_db)):
    p = db.get(SqlPolicy, policy_id)
    if not p:
        raise HTTPException(404)
    p.name = inp.name
    p.target_id = inp.target_id
    p.stmt_type = inp.stmt_type
    p.pattern_regex = inp.pattern_regex
    p.allow_exec = inp.allow_exec
    db.commit()
    db.refresh(p)
    return p


@router.delete("/policies/{policy_id}")
def delete_policy(policy_id: int, db: Session = Depends(get_db)):
    p = db.get(SqlPolicy, policy_id)
    if not p:
        raise HTTPException(404)
    db.delete(p)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@router.get("/dashboard", response_model=DashboardOut)
def dashboard(target_id: Optional[int] = Query(None),
              since_hours: int = Query(24), db: Session = Depends(get_db)):
    return dashboard_summary(db, target_id, since_hours)


# ---------------------------------------------------------------------------
# 调度 Job
# ---------------------------------------------------------------------------
@router.get("/jobs", response_model=list[JobOut])
def list_jobs(target_id: Optional[int] = Query(None), db: Session = Depends(get_db)):
    q = db.query(SqlJob).order_by(SqlJob.id)
    if target_id is not None:
        q = q.filter(SqlJob.target_id == target_id)
    return q.all()


@router.post("/jobs", response_model=JobOut)
def create_job(inp: JobIn, db: Session = Depends(get_db)):
    # 验证 target
    t = db.get(SqlTarget, inp.target_id)
    if not t:
        raise HTTPException(400, "target_id not found")
    if inp.trigger_type == "interval":
        if not (inp.interval_hours or inp.interval_minutes or inp.interval_seconds):
            raise HTTPException(400, "interval 至少填一个 (hours/minutes/seconds)")
    elif inp.trigger_type == "cron":
        if not inp.cron_expr:
            raise HTTPException(400, "cron 需填写 cron_expr")
    else:
        raise HTTPException(400, f"trigger_type 只支持 interval/cron，实际 {inp.trigger_type}")

    existing = db.query(SqlJob).filter(SqlJob.name == inp.name).first()
    if existing:
        raise HTTPException(400, "name 已存在")

    job = SqlJob(
        name=inp.name, target_id=inp.target_id, since_days=inp.since_days,
        trigger_type=inp.trigger_type,
        interval_hours=inp.interval_hours,
        interval_minutes=inp.interval_minutes,
        interval_seconds=inp.interval_seconds,
        cron_expr=inp.cron_expr,
        enabled=inp.enabled,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    if job.enabled:
        register_job(job)
        # register_job 内部会另开 Session 回写 next_run_at，需要 refresh 回来
        db.refresh(job)
    return job


@router.put("/jobs/{job_id}", response_model=JobOut)
def update_job(job_id: int, inp: JobIn, db: Session = Depends(get_db)):
    job = db.get(SqlJob, job_id)
    if not job:
        raise HTTPException(404)
    job.name = inp.name
    job.target_id = inp.target_id
    job.since_days = inp.since_days
    job.trigger_type = inp.trigger_type
    job.interval_hours = inp.interval_hours
    job.interval_minutes = inp.interval_minutes
    job.interval_seconds = inp.interval_seconds
    job.cron_expr = inp.cron_expr
    job.enabled = inp.enabled
    db.commit()
    db.refresh(job)
    if job.enabled:
        register_job(job)
        db.refresh(job)
    else:
        remove_job(job.id)
    return job


@router.patch("/jobs/{job_id}/toggle", response_model=JobOut)
def toggle_job(job_id: int, enabled: bool, db: Session = Depends(get_db)):
    job = db.get(SqlJob, job_id)
    if not job:
        raise HTTPException(404)
    job.enabled = enabled
    db.commit()
    db.refresh(job)
    if enabled:
        register_job(job)
        db.refresh(job)
    else:
        remove_job(job.id)
    return job


@router.delete("/jobs/{job_id}")
def delete_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(SqlJob, job_id)
    if not job:
        raise HTTPException(404)
    remove_job(job.id)
    db.delete(job)
    db.commit()
    return {"ok": True}


@router.post("/jobs/{job_id}/run")
def run_job(job_id: int):
    return run_job_now(job_id)
