"""Pydantic request/response schemas.

Split by domain so each module is small and easy to extend.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Oracle Instance
# ---------------------------------------------------------------------------
class OracleInstanceBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Display name")
    host: str = Field(..., max_length=255)
    port: int = Field(default=1521, ge=1, le=65535)
    service_name: str = Field(..., max_length=100)
    read_user: str = Field(..., max_length=100)
    read_password: str
    sandbox_user: Optional[str] = None
    sandbox_password: Optional[str] = None


class OracleInstanceCreate(OracleInstanceBase):
    pass


class OracleInstanceUpdate(BaseModel):
    """Partial update — only provided fields are touched."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    host: Optional[str] = None
    port: Optional[int] = None
    service_name: Optional[str] = None
    read_user: Optional[str] = None
    read_password: Optional[str] = None
    sandbox_user: Optional[str] = None
    sandbox_password: Optional[str] = None


class OracleInstanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    host: str
    port: int
    service_name: str
    read_user: str
    sandbox_user: Optional[str]
    oracle_version: Optional[str]
    status: str
    last_checked: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class OracleConnectionTestResult(BaseModel):
    ok: bool
    oracle_version: Optional[str] = None
    message: str
    permissions: list[str] = Field(default_factory=list)
    missing_permissions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Top SQL
# ---------------------------------------------------------------------------
class TopSQLQuery(BaseModel):
    instance_id: int
    metric: str = Field(
        default="elapsed",
        description="Sort metric: elapsed | cpu | logical_reads | physical_reads | executions",
    )
    limit: int = Field(default=20, ge=1, le=200)
    hours: int = Field(default=1, ge=1, le=168, description="Look-back window in hours")


class TopSQLRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sql_id: Optional[str] = None
    hash_value: Optional[int] = None
    child_number: int
    sql_text: Optional[str] = None
    executions: Optional[int] = None
    elapsed_time: Optional[float] = None
    cpu_time: Optional[float] = None
    buffer_gets: Optional[int] = None
    disk_reads: Optional[int] = None
    module: Optional[str] = None
    snapshot_time: datetime
    # Computed by the service layer (not persisted).
    rank: Optional[int] = None
    avg_elapsed_ms: Optional[float] = None
    total_elapsed_ms: Optional[float] = None


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------
class SandboxRunRequest(BaseModel):
    instance_id: int
    sql: str = Field(..., min_length=1)
    bind_vars: Optional[dict] = None
    runs: int = Field(default=3, ge=1, le=10)
    timeout_sec: int = Field(default=300, ge=1, le=3600)


class SandboxRunResult(BaseModel):
    elapsed_ms: float
    cpu_ms: Optional[float] = None
    buffer_gets: Optional[int] = None
    disk_reads: Optional[int] = None
    rows_processed: Optional[int] = None
    plan_nodes: list[dict] = Field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------
class CompareRequest(BaseModel):
    instance_id: int
    original_sql: str = Field(..., min_length=1)
    optimized_sql: str = Field(..., min_length=1)
    bind_vars: Optional[dict] = None
    runs: int = 3
    timeout_sec: int = 300


class MetricDelta(BaseModel):
    before: float
    after: float
    delta_pct: float  # negative = improvement


class CompareResult(BaseModel):
    task_id: int
    before: SandboxRunResult
    after: SandboxRunResult
    deltas: dict[str, MetricDelta]


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------
class Finding(BaseModel):
    rule_id: str
    rule_name: str
    severity: str
    description: str
    object_name: Optional[str] = None
    evidence: Optional[dict] = None


class DiagnosisReport(BaseModel):
    instance_id: int
    sql_id: Optional[str] = None
    findings: list[Finding] = Field(default_factory=list)
    summary: str


# ---------------------------------------------------------------------------
# Generic
# ---------------------------------------------------------------------------
class HealthResponse(BaseModel):
    status: str
    app: str
    version: str
    database: str
