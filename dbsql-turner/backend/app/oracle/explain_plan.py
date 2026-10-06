"""F2-01 + F3-01 — EXPLAIN PLAN execution + dependency extraction + plan tree parsing.

One-shot EXPLAIN PLAN strategy:
1. ``EXPLAIN PLAN SET STATEMENT_ID=... INTO PLAN_TABLE FOR <sql>``
2. ``SELECT ... FROM PLAN_TABLE WHERE STATEMENT_ID=:id ORDER BY ID`` — 依赖抽取 + 计划
3. ``DBMS_XPLAN.DISPLAY('PLAN_TABLE', :id, 'ALL')`` — 人类可读文本（可选）

PLAN_TABLE 是 Oracle 内置临时表（首次 EXPLAIN 自动建），**会话隔离**：
不同会话的 STATEMENT_ID 彼此不可见，所以我们用 ``session_id + uuid`` 做前缀避免冲突。
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Iterable, Optional

from loguru import logger

from .connection import OracleConnection


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------

@dataclass
class Dependency:
    """One object the SQL touches (table / index / view)."""
    owner: Optional[str]
    name: str
    object_type: str  # TABLE | INDEX | VIEW | MATERIALIZED VIEW


@dataclass
class PlanNode:
    """One row from PLAN_TABLE.

    All nullable fields default to None, so callers can construct a PlanNode
    with only ``id`` + ``operation`` + a couple of fields — useful for unit tests.
    """
    id: int = 0
    parent_id: Optional[int] = None
    operation: str = ""             # TABLE ACCESS | INDEX SCAN | HASH JOIN ...
    options: Optional[str] = None  # FULL | RANGE SCAN | UNIQUE ...
    object_owner: Optional[str] = None
    object_name: Optional[str] = None
    object_alias: Optional[str] = None
    optimizer: Optional[str] = None
    rows: Optional[float] = None   # Cardinality estimate (E-Rows)
    bytes: Optional[int] = None
    cost: Optional[float] = None
    time: Optional[int] = None     # in seconds (estimated)
    access_predicates: Optional[str] = None
    filter_predicates: Optional[str] = None


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _make_stmt_id() -> str:
    """Session-local unique STATEMENT_ID."""
    return f"DBSQL_{uuid.uuid4().hex[:12]}"


def explain_plan(conn: OracleConnection, sql: str) -> str:
    """Run EXPLAIN PLAN INTO PLAN_TABLE. Returns the STATEMENT_ID used."""
    # Wrap SQL — DDL / multi-statement / scripts are rejected at this layer.
    sql = sql.strip().rstrip(";")
    stmt_id = _make_stmt_id()

    explain_sql = (
        "EXPLAIN PLAN SET STATEMENT_ID = :stmt_id INTO PLAN_TABLE FOR " + sql
    )
    cur = conn.cursor()
    try:
        cur.execute(explain_sql, {"stmt_id": stmt_id})
    except Exception as e:
        # Wrap with simpler error
        cur.close()
        raise RuntimeError(f"EXPLAIN PLAN failed: {e}") from e
    cur.close()
    logger.debug(f"EXPLAIN PLAN stmt_id={stmt_id}")
    return stmt_id


def fetch_plan_table_rows(conn: OracleConnection, stmt_id: str) -> list[tuple]:
    """Raw PLAN_TABLE rows ordered by ID."""
    query = """
        SELECT
            ID, PARENT_ID, OPERATION, OPTIONS,
            OBJECT_OWNER, OBJECT_NAME, OBJECT_ALIAS,
            OPTIMIZER, CARDINALITY, BYTES, COST, TIME,
            ACCESS_PREDICATES, FILTER_PREDICATES
        FROM PLAN_TABLE
        WHERE STATEMENT_ID = :sid
        ORDER BY ID
    """
    return conn.fetch_all(query, {"sid": stmt_id})


def parse_plan_nodes(rows: list[tuple]) -> list[PlanNode]:
    """Convert raw PLAN_TABLE tuples to PlanNode objects."""
    nodes: list[PlanNode] = []
    for r in rows:
        nodes.append(PlanNode(
            id=r[0], parent_id=r[1],
            operation=(r[2] or "").strip(),
            options=(r[3] or "").strip() or None,
            object_owner=r[4], object_name=r[5], object_alias=r[6],
            optimizer=r[7],
            rows=_float_or_none(r[8]), bytes=_int_or_none(r[9]),
            cost=_float_or_none(r[10]), time=_int_or_none(r[11]),
            access_predicates=r[12], filter_predicates=r[13],
        ))
    return nodes


def extract_dependencies(nodes: Iterable[PlanNode]) -> list[Dependency]:
    """Collect unique table/index/view references from plan nodes.

    Only keep operations that actually access storage:
      TABLE ACCESS BY ...  → TABLE
      INDEX SCAN ...       → INDEX
      MAT_VIEW ACCESS      → MATERIALIZED VIEW
      VIEW                 → VIEW
      MERGE STATEMENT      → TABLE (merge target)
      INSERT/UPDATE/DELETE → TABLE (DML target)

    We deliberately skip INTERNAL operations (HASH JOIN / NL / SORT / etc.).
    """
    DEP_OP_PATTERNS = [
        ("TABLE ACCESS", "TABLE"),
        ("INDEX ", "INDEX"),    # INDEX RANGE SCAN / FULL SCAN / ...
        ("INDEX SKIP", "INDEX"),
        ("MAT_VIEW ACCESS", "MATERIALIZED VIEW"),
        ("VIEW", "VIEW"),
        ("MERGE STATEMENT", "TABLE"),
        ("INSERT STATEMENT", "TABLE"),
        ("UPDATE STATEMENT", "TABLE"),
        ("DELETE STATEMENT", "TABLE"),
    ]

    seen: set[tuple[str | None, str, str]] = set()
    deps: list[Dependency] = []
    for n in nodes:
        if not n.object_name:
            continue
        matched_type = None
        for pat, typ in DEP_OP_PATTERNS:
            if n.operation.startswith(pat):
                matched_type = typ
                break
        if matched_type is None:
            continue
        key = (n.object_owner, n.object_name.upper(), matched_type)
        if key in seen:
            continue
        seen.add(key)
        deps.append(Dependency(
            owner=n.object_owner, name=n.object_name, object_type=matched_type,
        ))
    return deps


# ---------------------------------------------------------------------------
# All-in-one convenience
# ---------------------------------------------------------------------------

def explain_and_parse(conn: OracleConnection, sql: str) -> tuple[str, list[PlanNode], list[Dependency]]:
    """Run EXPLAIN PLAN → parse nodes → extract deps — one call."""
    stmt_id = explain_plan(conn, sql)
    raw = fetch_plan_table_rows(conn, stmt_id)
    nodes = parse_plan_nodes(raw)
    deps = extract_dependencies(nodes)
    return stmt_id, nodes, deps


# ---------------------------------------------------------------------------
# DBMS_XPLAN human-readable fallback
# ---------------------------------------------------------------------------

def dbms_xplan_display(conn: OracleConnection, stmt_id: str) -> str:
    """DBMS_XPLAN.DISPLAY text — useful for debugging / 10g fallback."""
    # DBMS_XPLAN returns a table; we collect all rows and join.
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY('PLAN_TABLE', :sid, 'ALL'))",
            {"sid": stmt_id},
        )
        lines = [r[0] for r in cur.fetchall()]
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"DBMS_XPLAN.DISPLAY failed: {e}")
        return f"<DBMS_XPLAN unavailable: {e}>"
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _float_or_none(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int_or_none(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
