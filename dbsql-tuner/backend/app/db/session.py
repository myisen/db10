"""SQLAlchemy 2.0 async engine + session factory.

Supports both PostgreSQL (asyncpg) and SQLite (aiosqlite) via the same
``database_url`` setting. SQLite is used for local development and tests.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from ..core.config import get_settings


class Base(DeclarativeBase):
    """All ORM models inherit from this."""


# Lazy initialisation — engines are created on first use so that settings
# (loaded from env/.env) are resolved correctly.
_engine = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def _connect_args(url: str) -> dict:
    """Return driver-specific connect kwargs."""
    if url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.debug,
            future=True,
            connect_args=_connect_args(settings.database_url),
            # SQLite doesn't support pool_pre_ping well — skip it there.
            pool_pre_ping=not settings.database_url.startswith("sqlite"),
        )
        logger.info(f"Metadata DB engine created: {settings.database_url}")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a scoped session and closes it after the request."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create all tables (dev helper, use alembic in production)."""
    engine = get_engine()
    async with engine.begin() as conn:
        # Import models so Base.metadata is populated before create_all.
        from .. import models  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Metadata DB initialised (dev mode).")


async def close_db() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        logger.info("Metadata DB engine disposed.")
