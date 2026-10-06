"""Global exception handlers and custom exception types."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from loguru import logger


class TunerError(Exception):
    """Base for all DBSQL-Turner errors surfaced to API consumers."""

    def __init__(self, message: str, status_code: int = 400, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


class OracleConnectionError(TunerError):
    """Raised when an Oracle connection fails or times out."""

    def __init__(self, message: str, detail: str | None = None):
        super().__init__(message, status_code=503, detail=detail)


class OraclePermissionError(TunerError):
    """Raised when the Oracle account lacks required privileges."""

    def __init__(self, message: str, detail: str | None = None):
        super().__init__(message, status_code=403, detail=detail)


class SandboxSecurityError(TunerError):
    """Raised when sandbox SQL fails the whitelist check."""

    def __init__(self, message: str):
        super().__init__(message, status_code=400)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers to the FastAPI app so errors become clean JSON."""

    @app.exception_handler(TunerError)
    async def _handle_tuner(_, exc: TunerError) -> JSONResponse:
        logger.warning(f"{exc.__class__.__name__}: {exc.message} ({exc.detail})")
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.message, "detail": exc.detail},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_, exc: Exception) -> JSONResponse:
        logger.exception(f"Unhandled exception: {exc}")
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "detail": str(exc)},
        )
