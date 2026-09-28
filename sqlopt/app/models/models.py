"""ORM 模型（SQLite / PostgreSQL 双库兼容）。

经验教训：
  - 不用 PostgreSQL 专属 JSONB/ARRAY，统一用 sqlalchemy.JSON
  - SQLite 上 JSON 实际存为 TEXT，应用层用 json.dumps/loads 手动处理
  - SQLite 主键自增要求类型为 INTEGER，BIGINT 在 SQLite 上不会触发自增
    → 所有 id 列用 Integer，PostgreSQL 上 SQLAlchemy 会自动用 SERIAL/BIGSERIAL
  - 避免使用 SQLAlchemy Declarative 保留字（如 metadata）
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)

from ..database import Base


# ---------------------------------------------------------------------------
# sql_target：被采集实例
# ---------------------------------------------------------------------------
class SqlTarget(Base):
    __tablename__ = "sql_target"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), unique=True, nullable=False)
    db_type = Column(String(16), nullable=False)
    conn_url = Column(String(256), nullable=False)
    username = Column(String(64), nullable=False)
    password_enc = Column(String(256), nullable=False)
    extra_conf = Column(JSON, default=dict)
    enabled = Column(Boolean, default=True)
    last_collect = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# sql_fingerprint：SQL 指纹（canonical + literal）
# ---------------------------------------------------------------------------
class SqlFingerprint(Base):
    __tablename__ = "sql_fingerprint"

    id = Column(Integer, primary_key=True, autoincrement=True)
    fingerprint = Column(String(64), unique=True, nullable=False)
    canonical_sql = Column(Text, nullable=False)
    literal_sql = Column(Text, nullable=True)
    literal_available = Column(Boolean, default=False)
    stmt_type = Column(String(16), nullable=False)
    schema_name = Column(String(64), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------------------------
# sql_stat_snapshot：历史性能快照
# ---------------------------------------------------------------------------
class SqlStatSnapshot(Base):
    __tablename__ = "sql_stat_snapshot"

    id = Column(Integer, primary_key=True, autoincrement=True)
    target_id = Column(Integer, ForeignKey("sql_target.id"), nullable=False)
    fingerprint = Column(
        String(64), ForeignKey("sql_fingerprint.fingerprint"), nullable=False
    )
    snap_time = Column(DateTime, nullable=False)

    executions = Column(BigInteger, default=0)
    elapsed_ms = Column(Float, default=0.0)
    avg_elapsed_ms = Column(Float, default=0.0)
    cpu_ms = Column(Float, default=0.0)
    buffer_gets = Column(BigInteger, default=0)
    rows_processed = Column(BigInteger, default=0)
    disk_reads = Column(BigInteger, default=0)
    optimizer_cost = Column(Float, nullable=True)
    plan_hash = Column(String(32), nullable=True)
    db_snap_id = Column(String(64), nullable=True)

    raw_data = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "target_id", "fingerprint", "snap_time", name="uk_snap_unique"
        ),
        Index("idx_snap_fp_time", "fingerprint"),
        Index("idx_snap_target_time", "target_id", "snap_time"),
    )


# ---------------------------------------------------------------------------
# sql_exec_history：在线执行历史
# ---------------------------------------------------------------------------
class SqlExecHistory(Base):
    __tablename__ = "sql_exec_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    target_id = Column(Integer, ForeignKey("sql_target.id"), nullable=False)
    fingerprint = Column(String(64), nullable=True)
    sql_text = Column(Text, nullable=False)
    stmt_type = Column(String(16), nullable=False)
    exec_status = Column(String(16), nullable=False)
    elapsed_ms = Column(Float, default=0.0)
    rows_affected = Column(BigInteger, default=0)
    error_msg = Column(Text, nullable=True)
    exec_by = Column(String(64), nullable=False, default="anonymous")
    exec_at = Column(DateTime, server_default=func.now())

    __table_args__ = (Index("idx_exec_target_time", "target_id", "exec_at"),)


# ---------------------------------------------------------------------------
# sql_policy：执行策略
# ---------------------------------------------------------------------------
class SqlPolicy(Base):
    __tablename__ = "sql_policy"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), unique=True, nullable=False)
    target_id = Column(Integer, ForeignKey("sql_target.id"), nullable=True)
    stmt_type = Column(String(16), nullable=False)
    pattern_regex = Column(String(512), nullable=True)
    allow_exec = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())
