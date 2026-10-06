"""SQLAlchemy ORM models for the DBSQL-Turner metadata database."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.session import Base


def _now() -> datetime:
    return datetime.utcnow()


class OracleInstance(Base):
    """A target Oracle database that DBSQL-Turner can inspect."""

    __tablename__ = "oracle_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=1521)
    service_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Accounts — passwords stored encrypted (see app.core.security).
    read_user: Mapped[str] = mapped_column(String(100), nullable=False)
    read_password: Mapped[str] = mapped_column(String(512), nullable=False)

    sandbox_user: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    sandbox_password: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # Auto-populated on first successful connection.
    oracle_version: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # health probe
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    last_checked: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now, onupdate=_now
    )

    # Relationships
    top_sql_snapshots: Mapped[list["TopSQLSnapshot"]] = relationship(
        back_populates="instance", cascade="all, delete-orphan"
    )
    optimization_records: Mapped[list["OptimizationRecord"]] = relationship(
        back_populates="instance", cascade="all, delete-orphan"
    )


class TopSQLSnapshot(Base):
    """One-row-per-SQL capture from V$SQL / AWR at a point in time."""

    __tablename__ = "top_sql_snapshots"
    __table_args__ = (
        UniqueConstraint("instance_id", "snapshot_time", "sql_id", name="uq_top_sql"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("oracle_instances.id", ondelete="CASCADE"), index=True
    )
    instance: Mapped[OracleInstance] = relationship(back_populates="top_sql_snapshots")

    snapshot_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    # Identifiers — 11g+ uses SQL_ID; 10g falls back to HASH_VALUE.
    sql_id: Mapped[Optional[str]] = mapped_column(String(13), nullable=True, index=True)
    hash_value: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    child_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    sql_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Metrics (mostly from V$SQL in microseconds / counts).
    executions: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    elapsed_time: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cpu_time: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    buffer_gets: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    disk_reads: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    parse_calls: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    module: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    first_load_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Rank when this snapshot was taken (helps quick pagination of top lists).
    metric_rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class OptimizationRecord(Base):
    """A complete optimisation attempt — from diagnosis to comparison."""

    __tablename__ = "optimization_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("oracle_instances.id", ondelete="CASCADE"), index=True
    )
    instance: Mapped[OracleInstance] = relationship(back_populates="optimization_records")

    sql_id: Mapped[Optional[str]] = mapped_column(String(13), nullable=True, index=True)

    original_sql: Mapped[str] = mapped_column(Text, nullable=False)
    optimized_sql: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Stored as JSON text for cross-db portability (works on SQLite + PG).
    findings_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    suggestions_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    before_metrics_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    after_metrics_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    improvement_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    operator: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
