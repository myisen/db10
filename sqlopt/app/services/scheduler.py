"""APScheduler 调度服务。

设计要点：
  - 用 BackgroundScheduler（FastAPI 进程内）
  - 启动时从 sql_job 表加载 enabled=True 的任务
  - 执行时创建独立 Session，避免多线程共享 Session 出事
  - 更新 last_run_* 字段（由 job runner 内部提交）
  - 异常全部捕获，不影响调度器本身
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from ..database import get_engine, make_session
from ..models.models import SqlJob
from .collector import CollectorService


_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="UTC")
    return _scheduler


def start_scheduler() -> None:
    sched = get_scheduler()
    if not sched.running:
        sched.start()
        reload_jobs()


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# 内部：把 SqlJob ORM → APScheduler Trigger
# ---------------------------------------------------------------------------
def _build_trigger(job: SqlJob):
    if job.trigger_type == "cron" and job.cron_expr:
        parts = job.cron_expr.split()
        # APScheduler CronTrigger 支持 5 字段（分 时 日 月 周）和 6 字段（秒 分 时 日 月 周）
        if len(parts) == 5:
            return CronTrigger(
                minute=parts[0], hour=parts[1], day=parts[2],
                month=parts[3], day_of_week=parts[4], timezone="UTC",
            )
        elif len(parts) == 6:
            return CronTrigger(
                second=parts[0], minute=parts[1], hour=parts[2],
                day=parts[3], month=parts[4], day_of_week=parts[5], timezone="UTC",
            )
        raise ValueError(f"cron_expr 需 5 或 6 字段（空格分隔），实际 {len(parts)}")

    # 默认 interval
    hours = job.interval_hours or 0
    minutes = job.interval_minutes or 0
    seconds = job.interval_seconds or 0
    if hours == minutes == seconds == 0:
        # 默认 24h
        hours = 24
    return IntervalTrigger(hours=hours, minutes=minutes, seconds=seconds, timezone="UTC")


def _job_runner(job_id: int) -> None:
    """被 APScheduler 调用的实际函数。注意：此函数在独立线程中运行。"""
    db = make_session()
    try:
        job = db.get(SqlJob, job_id)
        if job is None or not job.enabled:
            return

        start = datetime.utcnow()
        collector = CollectorService(db=db)
        try:
            result = collector.collect(job.target_id, job.since_days)
            job.last_run_status = "success"
            job.last_run_message = f"inserted_or_updated={result.get('inserted_or_updated', 0)}"
        except Exception as e:
            job.last_run_status = "failed"
            job.last_run_message = str(e)
        finally:
            collector.close()

        job.last_run_at = start
        db.commit()
    except Exception:
        # 调度任务自己的异常，吞掉避免炸掉 scheduler
        pass
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------
def reload_jobs() -> None:
    """从 DB 重载所有 enabled 的任务。"""
    sched = get_scheduler()

    # 先清掉 SQLOpt 自己加的 job（用 id 前缀区分）
    for j in list(sched.get_jobs()):
        if j.id.startswith("sqlopt:job:"):
            sched.remove_job(j.id)

    db = make_session()
    try:
        for job in db.query(SqlJob).filter(SqlJob.enabled == True).all():
            _register_job(job)
    finally:
        db.close()


def _register_job(job: SqlJob) -> None:
    sched = get_scheduler()
    try:
        trigger = _build_trigger(job)
    except Exception as e:
        db = make_session()
        try:
            j = db.get(SqlJob, job.id)
            if j:
                j.last_run_status = "failed"
                j.last_run_message = f"trigger build error: {e}"
                db.commit()
        finally:
            db.close()
        return

    sched.add_job(
        _job_runner,
        trigger=trigger,
        id=f"sqlopt:job:{job.id}",
        name=job.name,
        replace_existing=True,
        kwargs={"job_id": job.id},
    )
    # 立即把 next_run_at 回写 DB（让前端能看到）
    next_t = sched.get_job(f"sqlopt:job:{job.id}").next_run_time
    if next_t is not None:
        db = make_session()
        try:
            j = db.get(SqlJob, job.id)
            if j:
                # next_t 可能是带 tz 的，统一存 naive UTC
                if next_t.tzinfo is not None:
                    next_t = next_t.replace(tzinfo=None)
                j.next_run_at = next_t
                db.commit()
        finally:
            db.close()


def register_job(job: SqlJob) -> None:
    """新增或更新一个任务后，调这个让调度器立即生效。"""
    _register_job(job)


def remove_job(job_id: int) -> None:
    sched = get_scheduler()
    try:
        sched.remove_job(f"sqlopt:job:{job_id}")
    except Exception:
        pass


def run_job_now(job_id: int) -> dict[str, Any]:
    """手动立即执行（同步阻塞直到完成，用于手动触发 API）。"""
    db = make_session()
    job = db.get(SqlJob, job_id)
    if not job:
        db.close()
        return {"found": False}

    start = datetime.utcnow()
    collector = CollectorService(db=db)
    try:
        result = collector.collect(job.target_id, job.since_days)
        job.last_run_status = "success"
        job.last_run_message = f"inserted_or_updated={result.get('inserted_or_updated', 0)} (手动触发)"
        db.commit()
        return {"found": True, "status": "success", "result": result}
    except Exception as e:
        job.last_run_status = "failed"
        job.last_run_message = str(e)
        db.commit()
        return {"found": True, "status": "failed", "error": str(e)}
    finally:
        collector.close()
        db.close()
