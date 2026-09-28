"""数据库引擎 & 会话管理。

关键经验（来自 1027181 / 1879354）：
  - SQLite 不支持 JSONB/ARRAY，模型层统一用 sqlalchemy.JSON（底层按方言转 TEXT/JSONB）
  - SQLite 连接要加 check_same_thread=False（FastAPI 跨线程用）
  - 引擎用工厂模式，避免 import 时副作用
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


# --- Base 类 ---
class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


# --- 引擎工厂（惰性单例） ---
_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine

    url = get_settings().database_url
    kwargs: dict = {}

    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        # SQLite 开外键（ORM 声明了 FK）
        @event.listens_for(Engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.close()

    _engine = create_engine(url, echo=False, future=True, **kwargs)
    return _engine


_session_factory = sessionmaker(
    autoflush=False, autocommit=False, expire_on_commit=False
)


def make_session() -> Session:
    """创建一个新的 Session（绑定惰性引擎）。"""
    return _session_factory(bind=get_engine())


def get_db():
    """FastAPI 依赖，提供请求级 Session。"""
    db = make_session()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """启动时建表（开发/测试用；生产走 Alembic）。"""
    Base.metadata.create_all(get_engine())
