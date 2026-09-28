"""F3-02 — Actual execution plan fetching via DBMS_XPLAN.DISPLAY_CURSOR (11g+)
and V$SQL_PLAN_STATISTICS_ALL.

Fallback chain (version-aware):
  1) 11g+: DISPLAY_CURSOR(sql_id, child_number, 'ALLSTATS LAST') — actual rows vs estimated
  2) 10.2+: DISPLAY_CURSOR without 'ALLSTATS' (requires STATISTICS_LEVEL=ALL to have A-Rows)
  3) 10.1+: DISPLAY('PLAN_TABLE', ...) — estimated only
  4) Always available (all versions): EXPLAIN PLAN INTO PLAN_TABLE — estimated only

The API layer calls whichever of these makes sense based on conn.version.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from .connection import OracleConnection


@dataclass
class ActualPlanNode:
    id: int
    parent_id: Optional[int]
    operation: str
    options: Optional[str] = None
    object_name: Optional[str] = None
    object_owner: Optional[str] = None
    e_rows: Optional[float] = None      # estimated cardinality (rows)
    a_rows: Optional[float] = None      # actual rows executed
    buffers: Optional[int] = None
    reads: Optional[int] = None
    temp_spc: Optional[float] = None    # MB approximately
    access_predicates: Optional[str] = None
    filter_predicates: Optional[str] = None
    # Derived
    deviation: Optional[float] = None   # a_rows / max(e_rows, 1) when both present


# ---------------------------------------------------------------------------
# V$SQL_PLAN + V$SQL_PLAN_STATISTICS_ALL path (11g+)
# ---------------------------------------------------------------------------

_SQL_PLAN_QUERY = """
    SELECT
        p.ID, p.PARENT_ID, p.OPERATION, p.OPTIONS,
        p.OBJECT_NAME, p.OBJECT_OWNER,
        p.CARDINALITY AS E_ROWS,
        s.ROWS       AS A_ROWS,
        s.BUFFER_GETS AS BUFFERS,
        s.CR_BUFFER_GETS AS READS,
        s.TEMP_SPACE / 1024 / 1024 AS TEMP_MB,
        p.ACCESS_PREDICATES, p.FILTER_PREDICATES
    FROM V$SQL_PLAN p
    LEFT JOIN V$SQL_PLAN_STATISTICS_ALL s
      ON s.ADDRESS = p.ADDRESS
     AND s.HASH_VALUE = p.HASH_VALUE
     AND s.CHILD_NUMBER = p.CHILD_NUMBER
     AND s.OPERATION_ID = p.ID
    WHERE p.SQL_ID = :sql_id
      AND p.CHILD_NUMBER = :child_number
    ORDER BY p.ID
"""


def fetch_actual_plan_from_sql(
    conn: OracleConnection,
    sql_id: str,
    child_number: int = 0,
) -> list[ActualPlanNode]:
    """Try the 11g+ path.  Returns empty list if not supported / missing data."""
    if not conn.version.has_sql_id:
        return []
    try:
        rows = conn.fetch_all(
            _SQL_PLAN_QUERY, {"sql_id": sql_id, "child_number": child_number}
        )
    except Exception as e:
        logger.debug(f"V$SQL_PLAN_STATISTICS_ALL failed (expected on 10g): {e}")
        return []

    nodes: list[ActualPlanNode] = []
    for r in rows:
        e = _float(r[6])
        a = _float(r[7])
        dev = (a / max(e, 1)) if (a is not None and e is not None) else None
        nodes.append(ActualPlanNode(
            id=r[0], parent_id=r[1],
            operation=(r[2] or "").strip(), options=(r[3] or "").strip() or None,
            object_name=r[4], object_owner=r[5],
            e_rows=e, a_rows=a,
            buffers=_int(r[8]), reads=_int(r[9]), temp_spc=_float(r[10]),
            access_predicates=r[11], filter_predicates=r[12],
            deviation=dev,
        ))
    return nodes


# ---------------------------------------------------------------------------
# DBMS_XPLAN.DISPLAY_CURSOR text — as a second opinion (debug / 10g fallback)
# ---------------------------------------------------------------------------

def fetch_display_cursor_text(
    conn: OracleConnection,
    sql_id: Optional[str] = None,
    child_number: Optional[int] = None,
    plan_table_name: Optional[str] = None,
    statement_id: Optional[str] = None,
) -> str:
    """Return DBMS_XPLAN text.

    Path resolution:
    - If sql_id supplied → DISPLAY_CURSOR (preferred, 11g+)
    - Else if statement_id + plan_table_name → DISPLAY(plan_table_name, statement_id) (all versions)
    """
    cur = conn.cursor()
    try:
        if sql_id and conn.version.has_sql_id:
            fmt = "'ALLSTATS LAST'" if conn.version.is_11g_plus else "'ALL'"
            cur.execute(
                f"SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(:sql_id, :cn, {fmt}))",
                {"sql_id": sql_id, "cn": child_number},
            )
        elif plan_table_name and statement_id:
            cur.execute(
                "SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY(:pt, :sid, 'ALL'))",
                {"pt": plan_table_name, "sid": statement_id},
            )
        else:
            return ""
        return "\n".join(r[0] for r in cur.fetchall())
    except Exception as e:
        logger.debug(f"DBMS_XPLAN text fetch failed: {e}")
        return ""
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
