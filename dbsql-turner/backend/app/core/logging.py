"""Loguru configuration (console + rotating file)."""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(log_dir: str | Path = "logs") -> None:
    """Initialise loguru sinks. Safe to call more than once (idempotent)."""
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
               "<level>{message}</level>",
        colorize=True,
    )
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_path / "dbsql_turner_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="00:00",
        retention="30 days",
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )
