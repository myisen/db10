"""全局配置，通过 .env 覆盖。"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- 数据库 ---
    database_url: str = "sqlite:///./sqlopt.db"

    # --- 加密 ---
    fernet_key: str = "CHANGE_ME_IN_PROD"

    # --- API ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # --- 执行安全 ---
    exec_timeout_seconds: int = 30
    exec_max_rows: int = 1000
    exec_hard_timeout_seconds: int = 300

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
