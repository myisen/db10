"""Rule execution context — bundles all inputs a rule may need.

Designed so a single ``RunContext`` object is passed to every rule's
``match(ctx)`` method; rules only touch the attributes they care about
and can safely ignore missing ones (version-optional data defaults to None).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..oracle.collector_object_profile import ObjectProfileBundle, TableProfile, IndexProfile, ColumnProfile
from ..oracle.collector_top_sql import TopSQLRow
from ..oracle.connection import OracleConnectionManager, OracleVersion
from ..oracle.explain_plan import PlanNode


@dataclass
class RuleContext:
    """Everything a rule needs to evaluate.

    Optional fields (prefixed with ``or_``) may be missing when the caller
    chose not to fetch them — e.g. TopSQL metrics come from V$SQL which
    requires a live Oracle connection.
    """

    # Identifiers
    instance_id: int
    oracle_version: OracleVersion

    # SQL text (the input)
    sql_text: Optional[str] = None
    sql_id: Optional[str] = None

    # Estimated plan — always available if caller ran EXPLAIN PLAN
    estimated_plan: list[PlanNode] = field(default_factory=list)

    # Actual plan (11g+) — optional
    actual_plan: list = field(default_factory=list)

    # Object profile from EXPLAIN deps
    tables: list[TableProfile] = field(default_factory=list)
    indexes: list[IndexProfile] = field(default_factory=list)
    columns: list[ColumnProfile] = field(default_factory=list)

    # TopSQL runtime metrics (from V$SQL/V$SQLAREA) — optional
    top_sql_row: Optional[TopSQLRow] = None

    # Convenience lookups (populated by engine before dispatch)
    table_by_name: dict[str, TableProfile] = field(default_factory=dict)
    index_by_table: dict[str, list[IndexProfile]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        instance_id: int,
        oracle_version: OracleVersion,
        estimated_plan: list[PlanNode],
        tables: list[TableProfile],
        indexes: list[IndexProfile],
        columns: list[ColumnProfile],
        actual_plan: list | None = None,
        top_sql_row: Optional[TopSQLRow] = None,
        sql_text: Optional[str] = None,
        sql_id: Optional[str] = None,
    ) -> "RuleContext":
        ctx = cls(
            instance_id=instance_id,
            oracle_version=oracle_version,
            estimated_plan=estimated_plan,
            tables=tables,
            indexes=indexes,
            columns=columns,
            actual_plan=actual_plan or [],
            top_sql_row=top_sql_row,
            sql_text=sql_text,
            sql_id=sql_id,
        )
        # Build lookups once so each rule doesn't re-scan.
        ctx.table_by_name = {
            f"{t.owner.upper()}.{t.table_name.upper()}": t for t in tables
        }
        for idx in indexes:
            key = f"{idx.owner.upper()}.{idx.table_name.upper()}"
            ctx.index_by_table.setdefault(key, []).append(idx)
        return ctx
