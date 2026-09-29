"""Dashboard 聚合查询。"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from ..models.models import SqlJob, SqlStatSnapshot, SqlTarget


def top_slow_sql(
    db: Session,
    target_id: int | None = None,
    since_hours: int = 24,
    top_n: int = 10,
) -> list[dict]:
    """Top N 慢 SQL（按 avg_elapsed_ms 排序，从高到低）。"""
    q = db.query(
        SqlStatSnapshot.fingerprint,
        SqlStatSnapshot.target_id,
        func.coalesce(func.sum(SqlStatSnapshot.executions), 0).label("total_exec"),
        func.coalesce(func.avg(SqlStatSnapshot.avg_elapsed_ms), 0).label("avg_ms"),
        func.coalesce(func.max(SqlStatSnapshot.elapsed_ms), 0).label("max_elapsed_ms"),
        func.max(SqlStatSnapshot.snap_time).label("last_seen"),
    ).filter(
        SqlStatSnapshot.snap_time >= datetime.utcnow() - timedelta(hours=since_hours)
    )
    if target_id is not None:
        q = q.filter(SqlStatSnapshot.target_id == target_id)

    q = q.group_by(
        SqlStatSnapshot.fingerprint, SqlStatSnapshot.target_id
    ).order_by(
        desc("avg_ms"), desc("total_exec")
    ).limit(top_n)

    results = []
    for row in q.all():
        results.append({
            "fingerprint": row.fingerprint,
            "target_id": row.target_id,
            "total_exec": int(row.total_exec or 0),
            "avg_ms": float(row.avg_ms or 0),
            "max_elapsed_ms": float(row.max_elapsed_ms or 0),
            "last_seen": row.last_seen.isoformat() if row.last_seen else None,
        })
    return results


def stmt_type_distribution(
    db: Session,
    target_id: int | None = None,
    since_hours: int = 24,
) -> list[dict]:
    """按 stmt_type 分组的 SQL 数量。"""
    from ..models.models import SqlFingerprint

    q = db.query(
        SqlFingerprint.stmt_type,
        func.count(func.distinct(SqlStatSnapshot.fingerprint)).label("fp_cnt"),
        func.coalesce(func.sum(SqlStatSnapshot.executions), 0).label("exec_cnt"),
    ).outerjoin(
        SqlStatSnapshot, SqlStatSnapshot.fingerprint == SqlFingerprint.fingerprint
    ).filter(
        SqlStatSnapshot.snap_time >= datetime.utcnow() - timedelta(hours=since_hours)
    )
    if target_id is not None:
        q = q.filter(SqlStatSnapshot.target_id == target_id)

    q = q.group_by(SqlFingerprint.stmt_type)

    return [
        {"stmt_type": r.stmt_type or "UNKNOWN",
         "fingerprint_cnt": int(r.fp_cnt or 0),
         "execution_cnt": int(r.exec_cnt or 0)}
        for r in q.all()
    ]


def target_health(db: Session) -> list[dict]:
    """每个实例的采集健康度：是否有采集过、最近一次什么时候。"""
    results = []
    for t in db.query(SqlTarget).filter(SqlTarget.enabled == True).all():
        last_snap = db.query(
            func.max(SqlStatSnapshot.snap_time)
        ).filter(SqlStatSnapshot.target_id == t.id).scalar()
        last_collect = t.last_collect
        ref_time = last_snap or last_collect
        status = "never"
        if ref_time:
            diff = (datetime.utcnow() - ref_time).total_seconds()
            if diff < 3600:  # 1h
                status = "ok"
            elif diff < 86400:  # 24h
                status = "warn"
            else:
                status = "stale"

        # 调度任务状态
        jobs = db.query(SqlJob).filter(SqlJob.target_id == t.id, SqlJob.enabled == True).all()
        latest_job_run = None
        latest_job_status = None
        for j in jobs:
            if j.last_run_at and (latest_job_run is None or j.last_run_at > latest_job_run):
                latest_job_run = j.last_run_at
                latest_job_status = j.last_run_status

        results.append({
            "target_id": t.id,
            "name": t.name,
            "db_type": t.db_type,
            "last_collect": t.last_collect.isoformat() if t.last_collect else None,
            "last_snap_time": last_snap.isoformat() if last_snap else None,
            "status": status,
            "job_count": len(jobs),
            "latest_job_run": latest_job_run.isoformat() if latest_job_run else None,
            "latest_job_status": latest_job_status,
        })
    return results


def dashboard_summary(
    db: Session,
    target_id: int | None = None,
    since_hours: int = 24,
) -> dict:
    """仪表盘整体汇总（一次查完所有东西）。"""
    return {
        "target_health": target_health(db),
        "top_slow_sql": top_slow_sql(db, target_id, since_hours, top_n=10),
        "stmt_type_distribution": stmt_type_distribution(db, target_id, since_hours),
        "since_hours": since_hours,
    }
