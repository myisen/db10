"""F1 — Top SQL collectors.

Two sources:
1. **Realtime** — V$SQL / GV$SQL (11g+) or V$SQLAREA (10g fallback).
2. **Historical (AWR)** — DBA_HIST_SQLSTAT + DBA_HIST_SNAPSHOT + DBA_HIST_SQLTEXT.

All collectors take an ``OracleConnection`` (which already carries the parsed
Oracle version) so they can pick the right view.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

from .connection import OracleConnection


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------

@dataclass
class TopSQLRow:
    """Normalised row returned by every collector — version differences are
    swallowed at this layer."""
    sql_id: Optional[str] = None
    hash_value: Optional[int] = None
    child_number: int = 0
    sql_text: Optional[str] = None
    executions: Optional[int] = None
    elapsed_time_us: Optional[float] = None   # V$SQL uses microseconds
    cpu_time_us: Optional[float] = None
    buffer_gets: Optional[int] = None
    disk_reads: Optional[int] = None
    parse_calls: Optional[int] = None
    module: Optional[str] = None
    first_load_time: Optional[datetime] = None
    source: str = "realtime"  # realtime | awr


# ---------------------------------------------------------------------------
# Realtime collector
# ---------------------------------------------------------------------------

# Columns common across 10g / 11g+ — we always SELECT a superset and read
# only those present (oracledb raises for missing names; instead we build
# the query dynamically based on conn.version).
def _build_realtime_query(version) -> str:
    """Return the V$SQL / V$SQLAREA query appropriate for the Oracle version."""

    # 11g+: SQL_ID is available, plus CHILD_NUMBER, PARSING_SCHEMA_NAME.
    # 10g : no SQL_ID, fall back to HASH_VALUE + ADDRESS.
    if version.has_sql_id:
        return """
            SELECT
                SQL_ID,
                HASH_VALUE,
                CHILD_NUMBER,
                SQL_TEXT,
                EXECUTIONS,
                ELAPSED_TIME,
                CPU_TIME,
                BUFFER_GETS,
                DISK_READS,
                PARSE_CALLS,
                MODULE,
                FIRST_LOAD_TIME
            FROM V$SQL
            WHERE PARSING_SCHEMA_NAME NOT IN ('SYS','SYSTEM','DBSNMP','OUTLN')
              AND EXECUTIONS > 0
        """
    # 10g — V$SQLAREA does not have CHILD_NUMBER or FIRST_LOAD_TIME in the
    # same shape.  We join with V$SQL (10.2 has V$SQL) to get the per-child
    # view.  If V$SQL is missing (10.1) we fall back to V$SQLAREA alone.
    return """
        SELECT
            NULL AS SQL_ID,
            HASH_VALUE,
            0 AS CHILD_NUMBER,
            SQL_TEXT,
            EXECUTIONS,
            ELAPSED_TIME,
            CPU_TIME,
            BUFFER_GETS,
            DISK_READS,
            PARSE_CALLS,
            MODULE,
            NULL AS FIRST_LOAD_TIME
        FROM V$SQLAREA
        WHERE PARSING_SCHEMA_NAME NOT IN ('SYS','SYSTEM','DBSNMP','OUTLN')
          AND EXECUTIONS > 0
    """


def collect_realtime_top_sql(
    conn: OracleConnection,
    metric: str = "elapsed",
    limit: int = 20,
) -> list[TopSQLRow]:
    """Pull top SQL from V$SQL/V$SQLAREA, sort by the requested metric.

    metric must be one of: elapsed | cpu | logical_reads | physical_reads | executions
    Sort metric = ``total`` so we surface SQL that hurts most in aggregate,
    not the slowest single call that nobody executes.
    """
    query = _build_realtime_query(conn.version)
    rows = conn.fetch_all(query)

    # Convert DDL-agnostic tuple -> dict via cursor.description.
    cur = conn.cursor()
    cur.execute(query)
    cols = [c[0].upper() for c in cur.description]
    cur.close()

    parsed: list[TopSQLRow] = []
    for r in rows:
        d = dict(zip(cols, r))
        parsed.append(TopSQLRow(
            sql_id=d.get("SQL_ID"),
            hash_value=d.get("HASH_VALUE"),
            child_number=d.get("CHILD_NUMBER") or 0,
            sql_text=d.get("SQL_TEXT"),
            executions=d.get("EXECUTIONS"),
            elapsed_time_us=_float(d.get("ELAPSED_TIME")),
            cpu_time_us=_float(d.get("CPU_TIME")),
            buffer_gets=d.get("BUFFER_GETS"),
            disk_reads=d.get("DISK_READS"),
            parse_calls=d.get("PARSE_CALLS"),
            module=d.get("MODULE"),
            source="realtime",
        ))

    # Sort.  Total = value (not average), so high-frequency SQL that adds up wins.
    key_map = {
        "elapsed": lambda x: x.elapsed_time_us or 0,
        "cpu": lambda x: x.cpu_time_us or 0,
        "logical_reads": lambda x: x.buffer_gets or 0,
        "physical_reads": lambda x: x.disk_reads or 0,
        "executions": lambda x: x.executions or 0,
    }
    fn = key_map.get(metric, key_map["elapsed"])
    parsed.sort(key=fn, reverse=True)
    return parsed[:limit]


# ---------------------------------------------------------------------------
# AWR historical collector
# ---------------------------------------------------------------------------

def collect_awr_top_sql(
    conn: OracleConnection,
    hours: int = 1,
    metric: str = "elapsed",
    limit: int = 20,
) -> list[TopSQLRow]:
    """Aggregate SQL across AWR snapshots within the look-back window.

    Only works if AWR is licensed (Diagnostic Pack) and snapshots are being
    gathered.  On 10g/11g we rely on DBA_HIST_* views which exist when AWR is
    enabled; if not, we return an empty list (callers already handle that).
    """
    # Find the snap_id range first.
    try:
        since = datetime.utcnow() - timedelta(hours=hours)
        snap_query = """
            SELECT MIN(SNAP_ID), MAX(SNAP_ID)
            FROM DBA_HIST_SNAPSHOT
            WHERE END_INTERVAL_TIME >= CAST(:since AS TIMESTAMP)
        """
        row = conn.fetch_one(snap_query, {"since": since})
    except Exception as e:
        logger.warning(f"AWR snapshot query failed — AWR may be disabled: {e}")
        return []

    if not row or not row[0]:
        logger.info("No AWR snapshots in the requested window.")
        return []

    min_snap, max_snap = row
    logger.info(f"AWR window: snap {min_snap} → {max_snap} ({hours}h)")

    # Aggregate SQL across snapshots.
    # DBA_HIST_SQLSTAT columns match V$SQL roughly, but we need SUM() because
    # snapshots are cumulative counters.
    try:
        if conn.version.has_sql_id:
            agg_query = """
                SELECT
                    s.SQL_ID,
                    s.HASH_VALUE,
                    MAX(SUBSTR(t.SQL_TEXT, 1, 4000)) AS SQL_TEXT,
                    SUM(s.EXECUTIONS_DELTA)          AS EXECUTIONS,
                    SUM(s.ELAPSED_TIME_DELTA)        AS ELAPSED_TIME,
                    SUM(s.CPU_TIME_DELTA)            AS CPU_TIME,
                    SUM(s.BUFFER_GETS_DELTA)         AS BUFFER_GETS,
                    SUM(s.DISK_READS_DELTA)          AS DISK_READS
                FROM DBA_HIST_SQLSTAT s
                LEFT JOIN DBA_HIST_SQLTEXT t
                  ON t.DBID = s.DBID AND t.SQL_ID = s.SQL_ID
                WHERE s.SNAP_ID BETWEEN :min_snap AND :max_snap
                  AND s.EXECUTIONS_DELTA > 0
                GROUP BY s.SQL_ID, s.HASH_VALUE
            """
        else:
            # 10g: no SQL_ID in DBA_HIST_SQLSTAT (uses HASH_VALUE + ADDRESS).
            agg_query = """
                SELECT
                    NULL AS SQL_ID,
                    s.HASH_VALUE,
                    MAX(SUBSTR(t.SQL_TEXT, 1, 4000)) AS SQL_TEXT,
                    SUM(s.EXECUTIONS_DELTA)   AS EXECUTIONS,
                    SUM(s.ELAPSED_TIME_DELTA) AS ELAPSED_TIME,
                    SUM(s.CPU_TIME_DELTA)     AS CPU_TIME,
                    SUM(s.BUFFER_GETS_DELTA)  AS BUFFER_GETS,
                    SUM(s.DISK_READS_DELTA)   AS DISK_READS
                FROM DBA_HIST_SQLSTAT s
                LEFT JOIN DBA_HIST_SQLTEXT t
                  ON t.DBID = s.DBID AND t.HASH_VALUE = s.HASH_VALUE
                WHERE s.SNAP_ID BETWEEN :min_snap AND :max_snap
                  AND s.EXECUTIONS_DELTA > 0
                GROUP BY s.HASH_VALUE
            """
        rows = conn.fetch_all(agg_query, {"min_snap": min_snap, "max_snap": max_snap})
    except Exception as e:
        logger.warning(f"AWR aggregate query failed: {e}")
        return []

    parsed: list[TopSQLRow] = []
    for r in rows:
        parsed.append(TopSQLRow(
            sql_id=r[0] if conn.version.has_sql_id else None,
            hash_value=r[1],
            sql_text=r[2],
            executions=_int(r[3]),
            elapsed_time_us=_float(r[4]),
            cpu_time_us=_float(r[5]),
            buffer_gets=_int(r[6]),
            disk_reads=_int(r[7]),
            source="awr",
        ))

    key_map = {
        "elapsed": lambda x: x.elapsed_time_us or 0,
        "cpu": lambda x: x.cpu_time_us or 0,
        "logical_reads": lambda x: x.buffer_gets or 0,
        "physical_reads": lambda x: x.disk_reads or 0,
        "executions": lambda x: x.executions or 0,
    }
    parsed.sort(key=key_map.get(metric, key_map["elapsed"]), reverse=True)
    return parsed[:limit]


# ---------------------------------------------------------------------------
# Sorting & "total" metric unification (used by API layer)
# ---------------------------------------------------------------------------

def compute_top_score(row: TopSQLRow, metric: str) -> float:
    """Unified "total cost" score used for ranking Top SQL.

    metric maps to a raw total counter; if executions is tiny we still surface
    SQL that actually costs a lot *in total* rather than a one-off slow query.
    """
    if metric == "elapsed":
        return row.elapsed_time_us or 0.0
    if metric == "cpu":
        return row.cpu_time_us or 0.0
    if metric == "logical_reads":
        return float(row.buffer_gets or 0)
    if metric == "physical_reads":
        return float(row.disk_reads or 0)
    if metric == "executions":
        return float(row.executions or 0)
    return row.elapsed_time_us or 0.0


# ---------------------------------------------------------------------------
# Internals
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
