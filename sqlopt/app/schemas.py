"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# --- Target ---
class TargetIn(BaseModel):
    name: str
    db_type: str   # oracle / oceanbase
    conn_url: str  # Oracle: host:port/service ; OB: host:port
    username: str
    password: str
    extra_conf: dict | None = None


class TargetOut(BaseModel):
    id: int
    name: str
    db_type: str
    conn_url: str
    username: str
    extra_conf: dict | None
    enabled: bool
    last_collect: datetime | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- SQL 查询 ---
class SqlQueryIn(BaseModel):
    target_id: int | None = None
    stmt_type: str | None = None
    keyword: str | None = None          # 搜 canonical/literal
    only_with_literal: bool = False
    min_executions: int = 0
    time_from: datetime | None = None
    time_to: datetime | None = None
    page: int = 1
    page_size: int = 50


class SqlRow(BaseModel):
    fingerprint: str
    canonical_sql: str
    literal_sql: str | None
    literal_available: bool
    stmt_type: str
    schema_name: str | None
    total_executions: int
    avg_elapsed_ms: float
    last_exec_time: datetime | None


class SqlQueryOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[SqlRow]


class SqlDetailOut(BaseModel):
    fingerprint: str
    canonical_sql: str
    literal_sql: str | None
    literal_available: bool
    stmt_type: str
    schema_name: str | None
    snapshots: list[dict[str, Any]]


# --- 执行 ---
class ExecIn(BaseModel):
    target_id: int
    sql_text: str


class ExecOut(BaseModel):
    status: str                   # success / failed / blocked / rollback
    exec_status: str | None = None
    exec_status_hint: str | None = None
    reason: str | None = None     # blocked 时的拦截原因
    stmt_type: str
    elapsed_ms: float = 0.0
    rows: list[dict[str, Any]] = []
    total_rows: int = 0
    fingerprint: str | None = None
    error: str | None = None


# --- 采集 ---
class CollectIn(BaseModel):
    target_id: int
    since_days: int = 30


class CollectOneIn(BaseModel):
    target_id: int
    sql_id: str


class TestConnOut(BaseModel):
    ok: bool
    message: str


# --- 策略 ---
class PolicyIn(BaseModel):
    name: str
    target_id: int | None = None
    stmt_type: str
    pattern_regex: str | None = None
    allow_exec: bool = False


class PolicyOut(PolicyIn):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- 执行历史 ---
class ExecHistoryOut(BaseModel):
    id: int
    target_id: int
    fingerprint: str | None
    sql_text: str
    stmt_type: str
    exec_status: str
    elapsed_ms: float
    rows_affected: int
    error_msg: str | None
    exec_by: str
    exec_at: datetime

    model_config = ConfigDict(from_attributes=True)
