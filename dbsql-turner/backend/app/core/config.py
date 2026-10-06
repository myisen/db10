"""Application configuration loaded from environment or .env file."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for DBSQL-Turner backend."""

    # --- App ---
    app_name: str = "DBSQL-Turner"
    app_version: str = "0.1.0"
    debug: bool = True
    host: str = "0.0.0.0"
    port: int = 8000

    # --- Metadata database (PostgreSQL in prod, SQLite in dev) ---
    # Use sqlite:///./dbsql_turner.db for development, postgresql+asyncpg://... for prod
    database_url: str = "sqlite+aiosqlite:///./dbsql_turner.db"

    # --- Oracle driver ---
    # Path to Oracle Instant Client library dir (Thick mode).
    # Leave empty and oracledb will search standard OS paths.
    oracle_client_lib_dir: str = ""
    # Default pool sizing
    oracle_pool_min: int = 2
    oracle_pool_max: int = 20
    oracle_pool_increment: int = 1

    # --- Security ---
    # Encryption key for storing Oracle passwords at rest (Fernet, 32 bytes url-safe base64).
    # Generate one with: from cryptography.fernet import Fernet; print(Fernet.generate_key())
    secret_key: str = "please-change-me-in-production"

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton settings accessor (thread-safe)."""
    return Settings()
