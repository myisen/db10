"""DBSQL-Tuner FastAPI application entrypoint."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from .api import (
    auxiliary_router,
    diagnose_router,
    execution_plan_router,
    health_router,
    instances_router,
    object_profile_router,
    history_router,
    sandbox_router,
    top_sql_router,
)
from .core.config import get_settings
from .core.exceptions import register_exception_handlers
from .core.logging import setup_logging
from .db.session import close_db, init_db
from .oracle.connection import OracleConnectionManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hooks."""
    setup_logging()
    settings = get_settings()
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")

    # Initialise metadata DB tables (dev helper — production uses alembic).
    if settings.debug:
        try:
            await init_db()
        except Exception as e:
            logger.warning(f"init_db() failed (non-fatal): {e}")

    yield

    # Cleanup
    OracleConnectionManager.get().close_all()
    await close_db()
    logger.info("Shutdown complete.")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Oracle SQL optimisation platform — M1 MVP.",
        lifespan=lifespan,
    )

    # CORS — allow everything in dev; tighten up before prod.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Global error handling
    register_exception_handlers(app)

    # Routers
    app.include_router(health_router)
    app.include_router(instances_router)
    app.include_router(top_sql_router)
    app.include_router(object_profile_router)
    app.include_router(execution_plan_router)
    app.include_router(diagnose_router)
    app.include_router(sandbox_router)
    app.include_router(history_router)
    app.include_router(auxiliary_router)

    return app


# For uvicorn: ``uvicorn app.main:app --reload``
app = create_app()
