"""Object profile + Execution plan request/response schemas.

Split off from ``schemas/__init__.py`` to keep the main file focused on M1;
these are F2/F3 specific.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------
class ObjectProfileRequest(BaseModel):
    instance_id: int
    sql: str = Field(..., min_length=1, description="原始 SQL 文本（用于 EXPLAIN PLAN）")


class ObjectProfileByIdRequest(BaseModel):
    instance_id: int
    sql_id: Optional[str] = None
    hash_value: Optional[int] = None


class ExecutionPlanRequest(BaseModel):
    instance_id: int
    sql: Optional[str] = Field(default=None, description="SQL 文本 — 若提供则 EXPLAIN PLAN INTO 估算计划")
    sql_id: Optional[str] = Field(default=None, description="SQL_ID — 若提供则从 V$SQL / DISPLAY_CURSOR 拿实际计划（11g+）")
    child_number: int = Field(default=0)


# ---------------------------------------------------------------------------
# Response: Object profile
# ---------------------------------------------------------------------------
class DependencyOut(BaseModel):
    owner: Optional[str] = None
    name: str
    object_type: str  # TABLE | INDEX | VIEW | MATERIALIZED VIEW


class TableProfileOut(BaseModel):
    owner: str
    table_name: str
    num_rows: Optional[float] = None
    blocks: Optional[int] = None
    empty_blocks: Optional[int] = None
    avg_row_len: Optional[int] = None
    pct_free: Optional[int] = None
    last_analyzed: Optional[datetime] = None
    partitioned: Optional[str] = None
    num_partitions: Optional[int] = None
    stale: bool = False
    staleness_reason: Optional[str] = None
    inserts: Optional[int] = None
    updates: Optional[int] = None
    deletes: Optional[int] = None


class IndexColumnOut(BaseModel):
    column_name: str
    column_position: int
    descend: Optional[str] = None


class IndexProfileOut(BaseModel):
    owner: str
    index_name: str
    table_name: str
    uniqueness: Optional[str] = None
    index_type: Optional[str] = None
    leaf_blocks: Optional[int] = None
    distinct_keys: Optional[float] = None
    clustering_factor: Optional[float] = None
    num_rows: Optional[float] = None
    last_analyzed: Optional[datetime] = None
    usage_monitoring: Optional[bool] = None
    used: Optional[bool] = None
    column_count: int = 0
    columns: list[IndexColumnOut] = Field(default_factory=list)


class ColumnProfileOut(BaseModel):
    owner: str
    table_name: str
    column_name: str
    data_type: Optional[str] = None
    data_length: Optional[int] = None
    nullable: Optional[str] = None
    num_nulls: Optional[float] = None
    num_distinct: Optional[float] = None
    histogram: Optional[str] = None
    histogram_buckets: Optional[int] = None


class ObjectProfileOut(BaseModel):
    instance_id: int
    deps: list[DependencyOut] = Field(default_factory=list)
    tables: list[TableProfileOut] = Field(default_factory=list)
    indexes: list[IndexProfileOut] = Field(default_factory=list)
    columns: list[ColumnProfileOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Response: Execution plan
# ---------------------------------------------------------------------------
class PlanNodeOut(BaseModel):
    """Estimated plan (from PLAN_TABLE / EXPLAIN)."""
    id: int
    parent_id: Optional[int] = None
    operation: str
    options: Optional[str] = None
    object_owner: Optional[str] = None
    object_name: Optional[str] = None
    rows: Optional[float] = None
    bytes: Optional[int] = None
    cost: Optional[float] = None
    time: Optional[int] = None
    access_predicates: Optional[str] = None
    filter_predicates: Optional[str] = None
    level: int = 0  # Computed tree depth for indent


class ActualPlanNodeOut(BaseModel):
    """Actual plan (from V$SQL_PLAN_STATISTICS_ALL, 11g+)."""
    id: int
    parent_id: Optional[int] = None
    operation: str
    options: Optional[str] = None
    object_owner: Optional[str] = None
    object_name: Optional[str] = None
    e_rows: Optional[float] = None
    a_rows: Optional[float] = None
    buffers: Optional[int] = None
    reads: Optional[int] = None
    temp_spc: Optional[float] = None
    deviation: Optional[float] = None
    access_predicates: Optional[str] = None
    filter_predicates: Optional[str] = None
    # Highlights
    row_estimation_bad: bool = False  # deviation > 5 or < 0.2


class ExecutionPlanOut(BaseModel):
    instance_id: int
    sql_text: Optional[str] = None
    estimated_plan: list[PlanNodeOut] = Field(default_factory=list)
    actual_plan: list[ActualPlanNodeOut] = Field(default_factory=list)
    display_text: Optional[str] = None
    source: str = "explain"  # explain | sql_cursor | mixed
    sql_id: Optional[str] = None
    child_number: Optional[int] = None
    oracle_version: Optional[str] = None
