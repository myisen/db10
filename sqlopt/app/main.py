"""FastAPI 应用入口。

启动：uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
  → API: http://localhost:8000/docs
  → 前端: http://localhost:8000/ui/
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import init_db
from .routers.api import router as api_router


app = FastAPI(title="SQLOpt — SQL 历史性能分析系统", version="0.1.0")

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


# 挂载 frontend 目录为 /ui/static 让浏览器加载同源资源（CDN 资源由浏览器直接访问）
app.mount("/ui", StaticFiles(directory=str(_FRONTEND_DIR)), name="frontend")
