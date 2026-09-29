"""FastAPI 应用入口。

启动：uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
  → API: http://localhost:8000/docs
  → 前端: http://localhost:8000/
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import init_db
from .routers.api import router as api_router
from .services.scheduler import shutdown_scheduler, start_scheduler


app = FastAPI(title="SQLOpt — SQL 历史性能分析系统", version="0.2.0")

# MVP：全开放 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    # SQLite 首次启动自动建表（生产应走 Alembic 迁移）
    init_db()
    # 启动 APScheduler 并从 DB 加载任务
    start_scheduler()


@app.on_event("shutdown")
def _shutdown() -> None:
    shutdown_scheduler()


# --- API ---
app.include_router(api_router, prefix="/api", tags=["api"])


# --- 前端（CDN 单页，无需 npm 构建） ---
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/")
def root() -> FileResponse:
    return FileResponse(_FRONTEND_DIR / "index.html")


@app.get("/ui")
def ui_redirect() -> FileResponse:
    return FileResponse(_FRONTEND_DIR / "index.html")


app.mount("/ui", StaticFiles(directory=str(_FRONTEND_DIR)), name="frontend")
