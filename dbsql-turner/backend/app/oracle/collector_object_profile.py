"""F2 — Object profile collector.

Consumes deps (from EXPLAIN PLAN) and collects three layers of stats:
  - Table stats      → DBA_TABLES + DBA_TAB_PARTITIONS
  - Index stats      → DBA_INDEXES + DBA_IND_COLUMNS
  - Column stats     → DBA_TAB_COLUMNS + DBA_TAB_HISTOGRAMS
  - Index usage      → V$OBJECT_USAGE (10g/11g, requires MONITORING USAGE) / DBA_INDEXES.USE (12c+)
  - Stats staleness  → DBA_TAB_MODIFICATIONS (11g+) or LAST_ANALYZED age fallback

All queries are version-aware; missing columns degrade gracefully (null fields)
rather than crashing, so a 10g environment still produces a useful profile.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

from .connection import OracleConnection
from .explain_plan import Dependency


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------

@dataclass
class TableProfile:
    owner: str
    table_name: str
    num_rows: Optional[float] = None
    blocks: Optional[int] = None
    empty_blocks: Optional[int] = None
    avg_row_len: Optional[int] = None
    pct_free: Optional[int] = None
    last_analyzed: Optional[datetime] = None
    partitioned: Optional[str] = None   # YES | NO
    num_partitions: Optional[int] = None
    object_type: str = "TABLE"
    # Staleness
    stale: bool = False
    staleness_reason: Optional[str] = None
    inserts: Optional[int] = None
    updates: Optional[int] = None
    deletes: Optional[int] = None


@dataclass
class IndexColumn:
    column_name: str
    column_position: int
    descend: Optional[str] = None  # ASC | DESC


@dataclass
class IndexProfile:
    owner: str
    index_name: str
    table_name: str
    uniqueness: Optional[str] = None      # UNIQUE | NONUNIQUE
    index_type: Optional[str] = None      # NORMAL | BITMAP | FUNCTION-BASED NORMAL ...
    leaf_blocks: Optional[int] = None
    distinct_keys: Optional[float] = None
    clustering_factor: Optional[float] = None
    num_rows: Optional[float] = None
    last_analyzed: Optional[datetime] = None
    usage_monitoring: Optional[bool] = None  # true → MONITORING USAGE on
    used: Optional[bool] = None               # V$OBJECT_USAGE.USED
    column_count: int = 0
    columns: list[IndexColumn] = field(default_factory=list)


@dataclass
class ColumnProfile:
    owner: str
    table_name: str
    column_name: str
    data_type: Optional[str] = None
    data_length: Optional[int] = None
    nullable: Optional[str] = None        # Y | N
    num_nulls: Optional[float] = None
    num_distinct: Optional[float] = None
    low_value: Optional[str] = None
    high_value: Optional[str] = None
    histogram: Optional[str] = None       # HEIGHT BALANCED | FREQUENCY | NONE
    histogram_buckets: Optional[int] = None


@dataclass
class ObjectProfileBundle:
    deps: list[Dependency]
    tables: list[TableProfile]
    indexes: list[IndexProfile]
    columns: list[ColumnProfile]


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def _collect_tables(conn: OracleConnection, deps: list[Dependency]) -> list[TableProfile]:
    table_deps = [d for d in deps if d.object_type in ("TABLE", "MATERIALIZED VIEW")]
    if not table_deps:
        return []

    # Build (owner, name) IN list — DBA_TABLES uses UPPER case; we rely on PLAN_TABLE
    # already returning UPPER-cased names.  Some views lack DBA_TABLES rows — filter
    # silently rather than fail.
    owner_names = [(d.owner.upper() if d.owner else None, d.name.upper()) for d in table_deps]
    owner_names = [(o, n) for o, n in owner_names if o]  # need owner for DBA_TABLES

    # DBA_TABLES rows
    if owner_names:
        q_parts = []
        binds: dict = {}
        for i, (o, n) in enumerate(owner_names):
            q_parts.append(f"(:o{i}, :n{i})")
            binds[f"o{i}"] = o
            binds[f"n{i}"] = n
        in_clause = ",".join(q_parts)

        query = f"""
            SELECT OWNER, TABLE_NAME, NUM_ROWS, BLOCKS, EMPTY_BLOCKS,
                   AVG_ROW_LEN, PCT_FREE, LAST_ANALYZED, PARTITIONED
            FROM DBA_TABLES
            WHERE (OWNER, TABLE_NAME) IN ({in_clause})
        """
        rows = conn.fetch_all(query, binds)
    else:
        rows = []

    # Partitions count
    partitions: dict[tuple[str, str], int] = {}
    if owner_names:
        try:
            p_query = f"""
                SELECT TABLE_OWNER, TABLE_NAME, COUNT(*)
                FROM DBA_TAB_PARTITIONS
                WHERE (TABLE_OWNER, TABLE_NAME) IN ({in_clause})
                GROUP BY TABLE_OWNER, TABLE_NAME
            """
            for r in conn.fetch_all(p_query, binds):
                partitions[(r[0], r[1])] = int(r[2])
        except Exception as e:
            logger.debug(f"DBA_TAB_PARTITIONS query skipped (may be unprivileged): {e}")

    # Staleness — DBA_TAB_MODIFICATIONS (11g+) vs LAST_ANALYZED age (all versions).
    modifications: dict[tuple[str, str], tuple[Optional[int], Optional[int], Optional[int]]] = {}
    if conn.version.is_11g_plus and owner_names:
        try:
            m_query = f"""
                SELECT TABLE_OWNER, TABLE_NAME, INSERTS, UPDATES, DELETES
                FROM DBA_TAB_MODIFICATIONS
                WHERE (TABLE_OWNER, TABLE_NAME) IN ({in_clause})
                  AND (INSERTS + UPDATES + DELETES) > 0
            """
            for r in conn.fetch_all(m_query, binds):
                modifications[(r[0], r[1])] = (r[2], r[3], r[4])
        except Exception as e:
            logger.debug(f"DBA_TAB_MODIFICATIONS skipped: {e}")

    tables: list[TableProfile] = []
    for r in rows:
        owner, tname = r[0], r[1]
        p = TableProfile(
            owner=owner, table_name=tname,
            num_rows=r[2], blocks=r[3], empty_blocks=r[4],
            avg_row_len=r[5], pct_free=r[6], last_analyzed=r[7],
            partitioned=r[8],
            num_partitions=partitions.get((owner, tname)),
        )
        # Staleness
        mod = modifications.get((owner, tname))
        p.stale, p.staleness_reason = _judge_staleness(p, mod)
        if mod:
            p.inserts, p.updates, p.deletes = mod
        tables.append(p)
    return tables


def _judge_staleness(table: TableProfile, mods: Optional[tuple]) -> tuple[bool, Optional[str]]:
    """Returns (is_stale, reason)."""
    # 1) Modifications ratio — 11g+ path
    if mods and table.num_rows and table.num_rows > 0:
        ins, upd, dele = mods
        total_mods = (ins or 0) + (upd or 0) + (dele or 0)
        ratio = total_mods / max(table.num_rows, 1)
        if ratio > 0.1:  # 10% change
            return True, f"自上次收集以来 DML 超过 {ratio*100:.1f}%（ins={ins}, upd={upd}, del={dele}）"

    # 2) LAST_ANALYZED age — fallback for all versions
    if table.last_analyzed is None:
        return True, "从未收集过统计信息（LAST_ANALYZED IS NULL）"

    try:
        age_days = (datetime.utcnow() - table.last_analyzed.replace(tzinfo=None)).days
    except Exception:
        age_days = 0
    if age_days > 30:
        return True, f"统计信息已 {age_days} 天未更新（> 30 天阈值）"

    return False, None


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

def _collect_indexes(conn: OracleConnection, deps: list[Dependency]) -> list[IndexProfile]:
    table_deps = [d for d in deps if d.object_type in ("TABLE", "MATERIALIZED VIEW")]
    if not table_deps:
        return []

    binds: dict = {}
    q_parts = []
    for i, d in enumerate(table_deps):
        owner = (d.owner or "").upper()
        if not owner:
            continue
        q_parts.append(f"(:to{i}, :tn{i})")
        binds[f"to{i}"] = owner
        binds[f"tn{i}"] = d.name.upper()
    if not q_parts:
        return []

    in_clause = ",".join(q_parts)

    # DBA_INDEXES — always available.
    idx_query = f"""
        SELECT OWNER, INDEX_NAME, TABLE_NAME, UNIQUENESS, INDEX_TYPE,
               LEAF_BLOCKS, DISTINCT_KEYS, CLUSTERING_FACTOR,
               NUM_ROWS, LAST_ANALYZED
        FROM DBA_INDEXES
        WHERE (TABLE_OWNER, TABLE_NAME) IN ({in_clause})
    """
    rows = conn.fetch_all(idx_query, binds)

    # Index columns — DBA_IND_COLUMNS
    idx_col_query = f"""
        SELECT INDEX_OWNER, INDEX_NAME, COLUMN_NAME, COLUMN_POSITION, DESCEND
        FROM DBA_IND_COLUMNS
        WHERE (TABLE_OWNER, TABLE_NAME) IN ({in_clause})
        ORDER BY INDEX_OWNER, INDEX_NAME, COLUMN_POSITION
    """
    idx_cols_rows = conn.fetch_all(idx_col_query, binds)

    col_map: dict[tuple[str, str], list[IndexColumn]] = {}
    for r in idx_cols_rows:
        key = (r[0], r[1])
        col_map.setdefault(key, []).append(IndexColumn(
            column_name=r[2], column_position=r[3],
            descend=r[4] if r[4] else None,
        ))

    # Usage stats — version branch
    usage: dict[tuple[str, str], tuple[Optional[bool], Optional[bool]]] = {}
    if conn.version.is_12c_plus:
        try:
            q2 = f"""
                SELECT OWNER, INDEX_NAME,
                       USE,              -- 12c+: Y/N
                       MONITORING        -- 12c+: Y/N
                FROM DBA_INDEXES
                WHERE (TABLE_OWNER, TABLE_NAME) IN ({in_clause})
            """
            for r in conn.fetch_all(q2, binds):
                usage[(r[0], r[1])] = (
                    _yes_no_to_bool(r[3]), _yes_no_to_bool(r[2]),  # MONITORING, USE
                )
        except Exception as e:
            logger.debug(f"12c+ DBA_INDEXES.USE skipped: {e}")
    else:
        # 10g / 11g — V$OBJECT_USAGE requires monitoring enabled (ALTER INDEX ... MONITORING USAGE).
        # We try but gracefully degrade on privilege / absent monitoring.
        try:
            q2 = """
                SELECT INDEX_OWNER, INDEX_NAME, MONITORING, USED
                FROM V$OBJECT_USAGE
                WHERE INDEX_OWNER || '.' || INDEX_NAME IN (
            """ + " UNION ALL ".join(
                ["SELECT :uo{}||'.'||:un{} FROM DUAL".format(i, i) for i in range(len(q_parts))]
            ) + ")"
            usage_binds: dict = {}
            for i in range(len(q_parts)):
                usage_binds[f"uo{i}"] = binds[f"to{i}"]
                usage_binds[f"un{i}"] = binds[f"tn{i}"]
            for r in conn.fetch_all(q2, usage_binds):
                usage[(r[0], r[1])] = (_yes_no_to_bool(r[2]), _yes_no_to_bool(r[3]))
        except Exception as e:
            logger.debug(f"V$OBJECT_USAGE skipped (monitoring not active or no privilege): {e}")

    indexes: list[IndexProfile] = []
    for r in rows:
        owner, iname, tname = r[0], r[1], r[2]
        cols = col_map.get((owner, iname), [])
        mon, used = usage.get((owner, iname), (None, None))
        indexes.append(IndexProfile(
            owner=owner, index_name=iname, table_name=tname,
            uniqueness=r[3], index_type=r[4],
            leaf_blocks=r[5], distinct_keys=r[6],
            clustering_factor=r[7], num_rows=r[8], last_analyzed=r[9],
            usage_monitoring=mon, used=used,
            column_count=len(cols), columns=cols,
        ))
    return indexes


def _yes_no_to_bool(v: Optional[str]) -> Optional[bool]:
    if v is None:
        return None
    s = str(v).strip().upper()
    if s == "Y":
        return True
    if s == "N":
        return False
    return None


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------

def _collect_columns(conn: OracleConnection, deps: list[Dependency]) -> list[ColumnProfile]:
    table_deps = [d for d in deps if d.object_type in ("TABLE", "MATERIALIZED VIEW")]
    if not table_deps:
        return []

    binds: dict = {}
    q_parts = []
    for i, d in enumerate(table_deps):
        owner = (d.owner or "").upper()
        if not owner:
            continue
        q_parts.append(f"(:co{i}, :cn{i})")
        binds[f"co{i}"] = owner
        binds[f"cn{i}"] = d.name.upper()
    if not q_parts:
        return []

    in_clause = ",".join(q_parts)

    # Column base stats
    col_query = f"""
        SELECT OWNER, TABLE_NAME, COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE,
               NUM_NULLS, NUM_DISTINCT, LOW_VALUE, HIGH_VALUE
        FROM DBA_TAB_COLUMNS
        WHERE (OWNER, TABLE_NAME) IN ({in_clause})
    """
    rows = conn.fetch_all(col_query, binds)

    # Histogram per column (DBA_TAB_HISTOGRAMS has one row per bucket; bucket count = COUNT(*))
    hist_query = f"""
        SELECT OWNER, TABLE_NAME, COLUMN_NAME, HISTOGRAM, COUNT(*) AS BUCKET_COUNT
        FROM DBA_TAB_HISTOGRAMS
        WHERE (OWNER, TABLE_NAME) IN ({in_clause})
        GROUP BY OWNER, TABLE_NAME, COLUMN_NAME, HISTOGRAM
    """
    hist_map: dict[tuple[str, str, str], tuple[Optional[str], Optional[int]]] = {}
    try:
        for r in conn.fetch_all(hist_query, binds):
            hist_map[(r[0], r[1], r[2])] = (r[3], int(r[4]))
    except Exception as e:
        logger.debug(f"DBA_TAB_HISTOGRAMS skipped: {e}")

    columns: list[ColumnProfile] = []
    for r in rows:
        h = hist_map.get((r[0], r[1], r[2]))
        columns.append(ColumnProfile(
            owner=r[0], table_name=r[1], column_name=r[2],
            data_type=r[3], data_length=r[4], nullable=r[5],
            num_nulls=r[6], num_distinct=r[7],
            low_value=r[8], high_value=r[9],
            histogram=h[0] if h else None,
            histogram_buckets=h[1] if h else None,
        ))
    return columns


# ---------------------------------------------------------------------------
# Entry point — one call
# ---------------------------------------------------------------------------

def collect_object_profile(
    conn: OracleConnection,
    deps: list[Dependency],
) -> ObjectProfileBundle:
    """Collect full object profile bundle for all deps."""
    tables = _collect_tables(conn, deps)
    indexes = _collect_indexes(conn, deps)
    columns = _collect_columns(conn, deps)
    logger.info(
        f"Object profile collected: {len(tables)} tables, "
        f"{len(indexes)} indexes, {len(columns)} columns"
    )
    return ObjectProfileBundle(deps=deps, tables=tables, indexes=indexes, columns=columns)
