"""Health check endpoint — always fast, exercises DB connectivity."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..db.session import get_db
from ..schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(db: AsyncSession = Depends(get_db)) -> HealthResponse:
    settings = get_settings()
    # Lightweight DB ping (SELECT 1).  On failure the exception handler kicks in.
    await db.execute(text("SELECT 1"))
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        database=settings.database_url.split("://")[0],
    )
