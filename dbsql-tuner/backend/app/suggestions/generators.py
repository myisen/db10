"""F5 — 12 条 Finding 对应 4 类 Suggestion 的具体生成器。

Mapping table (rule_id → suggestion_type):
  R-01 大表 FTS + 可索引过滤  → INDEX / HINT
  R-02 回表比例过高            → INDEX（覆盖索引）/ HINT
  R-03 基数估算偏差            → HINT（FIRST_ROWS / CARDINALITY）/ STATS
  R-04 Join 不平衡            → HINT（LEADING / ORDERED）
  R-05 Sort 落盘              → HINT（APPEND hint 避免？不 —— PGA 调参建议）
  R-06 笛卡尔积               → (逻辑问题，无自动化建议 —— 返回人工 review)
  R-07 CONNECT BY 无索引       → INDEX
  R-08 谓词列被函数包裹         → REWRITE（改写谓词方向）/ INDEX（函数索引）
  R-09 CLUSTERING_FACTOR 高   → 人工 DBA 建议（重组 / rebuild）
  R-10 SQL 版本爆炸            → 参数建议（cursor_sharing）
  R-11 软解析率低              → 参数建议 / 绑定变量
  R-12 统计陈旧               → STATS（DBMS_STATS.GATHER_TABLE_STATS）

所有生成器都是纯函数（输入 Finding + ctx → Suggestion list），
绝不 raise —— 异常被外层 engine 兜住。
"""
from __future__ import annotations

import re
from typing import List, Optional

from loguru import logger

from ..rules.base import Finding
from ..rules.context import RuleContext
from .base import Risk, Suggestion, SuggestionType


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_all(findings: List[Finding], ctx: RuleContext) -> List[Suggestion]:
    """Run every generator that matches a rule_id and return de-duplicated suggestions."""
    out: List[Suggestion] = []
    seen_keys: set[str] = set()

    for finding in findings:
        gen = GENERATORS.get(finding.rule_id, _no_generator)
        try:
            suggs = gen(finding, ctx)
        except Exception as e:
            logger.warning(f"Suggestion generator for {finding.rule_id} raised: {e}")
            continue
        for s in suggs:
            # De-dup by (type + sql fragment) — 不同 Finding 可能生成完全相同的 CREATE INDEX
            key = f"{s.suggestion_type.value}|{s.runnable_sql}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(s)

    logger.info(f"Suggestion generation complete. {len(out)} suggestion(s) from {len(findings)} finding(s).")
    return out


GENERATORS: dict[str, callable] = {}


def _register(rule_id: str):
    def deco(fn):
        GENERATORS[rule_id] = fn
        return fn
    return deco


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _guess_filter_columns(finding: Finding) -> List[str]:
    """Extract bare column names from access/filter predicates in evidence.

    This is the simplest possible heuristic — it doesn't need accurate semantic
    parsing, just something to feed the index suggestion a plausible column list.
    """
    ev = finding.evidence or {}
    preds = str(ev.get("access_predicates") or ev.get("filter_predicates") or ev.get("predicates") or "")
    # Find tokens like "COL =" or "COL >" — uppercase column names
    cols = re.findall(r"\b([A-Z][A-Z0-9_]{2,})\s*(=|<|>|<=|>=|!=|LIKE|IN|BETWEEN)\b", preds, re.IGNORECASE)
    # Strip SQL keywords
    KEYWORDS = {"SYSDATE", "SYSTIMESTAMP", "NULL", "TRUE", "FALSE", "TRUNC", "TO_DATE", "TO_CHAR",
                "TO_NUMBER", "NVL", "COALESCE", "DATE", "TIMESTAMP", "AND", "OR", "NOT", "IN",
                "LIKE", "BETWEEN", "SELECT", "FROM", "WHERE"}
    return [c.upper() for c in cols if c.upper() not in KEYWORDS]


def _table_from_object_name(name: Optional[str]) -> Optional[tuple[str, str]]:
    """Split 'OWNER.TABLE' or 'TABLE' into (owner_or_None, table)."""
    if not name:
        return None
    parts = name.split(".")
    if len(parts) == 2:
        return parts[0].upper(), parts[1].upper()
    return None, parts[0].upper()


def _top_n_columns(ctx: RuleContext, table_name: str, n: int = 3) -> List[str]:
    """Return up to n columns of the table, preferring indexed ones."""
    cols_by_table: dict[str, list[str]] = {}
    for idx in ctx.indexes:
        key = f"{idx.owner.upper()}.{idx.table_name.upper()}"
        for c in idx.columns:
            cols_by_table.setdefault(key, []).append(c.column_name.upper())
    # De-dup
    return list(dict.fromkeys(cols_by_table.get(table_name, [])))[:n]


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

def _no_generator(finding: Finding, ctx: RuleContext) -> List[Suggestion]:
    # Default — return empty list; rule has no runnable suggestion.
    return []


@_register("R-01")
def _gen_r01_fts(finding: Finding, ctx: RuleContext) -> List[Suggestion]:
    """大表 FTS + 可索引过滤列 → 复合索引 + INDEX hint."""
    out: List[Suggestion] = []
    tbl = _table_from_object_name(finding.object_name)
    if not tbl:
        return out
    owner, table = tbl
    filter_cols = _guess_filter_columns(finding)
    if not filter_cols:
        # Fall back to existing indexed columns as a guess
        filter_cols = _top_n_columns(ctx, f"{owner or ''}.{table}".strip("."), n=3)
    if not filter_cols:
        return out

    col_list = ", ".join(filter_cols)
    idx_name = f"IDX_{table[:20]}_{''.join(c[:3] for c in filter_cols)[:10]}".upper()
    ddl = f"CREATE INDEX {owner + '.' if owner else ''}{idx_name} ON {owner + '.' if owner else ''}{table} ({col_list});"

    out.append(Suggestion(
        suggestion_type=SuggestionType.INDEX,
        rule_id=finding.rule_id, rule_name=finding.rule_name,
        risk=Risk.HIGH,
        title=f"为 {table} 创建过滤索引",
        description=f"该表出现 TABLE ACCESS FULL，过滤列 {col_list} 可能缺失索引",
        runnable_sql=ddl,
        params={"table": table, "columns": filter_cols, "index_name": idx_name},
        estimated_benefit="过滤率 > 5% 时通常显著降低 I/O",
        side_effects="索引占用磁盘 + INSERT/UPDATE 变慢；上线前请评估并发窗口",
    ))
    out.append(Suggestion(
        suggestion_type=SuggestionType.HINT,
        rule_id=finding.rule_id, rule_name=finding.rule_name,
        risk=Risk.MEDIUM,
        title="强制走索引 Hint（临时应急）",
        description="使用 /*+ INDEX */ hint 让优化器先走索引验证收益",
        runnable_sql=f"/*+ INDEX({table} {idx_name}) */",
        params={"table": table, "index_name": idx_name},
        estimated_benefit="验证索引有效性的最快方式",
        side_effects="Hint 绑定执行计划，后续统计变更可能不再最优",
    ))
    return out


@_register("R-02")
def _gen_r02_rowid_ratio(finding: Finding, ctx: RuleContext) -> List[Suggestion]:
    """回表比例过高 → 覆盖索引."""
    out: List[Suggestion] = []
    tbl = _table_from_object_name(finding.object_name)
    if not tbl:
        return out
    owner, table = tbl
    idx_cols = _top_n_columns(ctx, f"{owner or ''}.{table}".strip("."), n=4)
    if not idx_cols:
        return out
    # 将 SELECT 的列也加入 INCLUDE（简化：这里只做 INCLUDE 模板）
    col_list = ", ".join(idx_cols)
    idx_name = f"IDX_{table[:20]}_COV".upper()
    ddl = f"CREATE INDEX {owner + '.' if owner else ''}{idx_name} ON {owner + '.' if owner else ''}{table} ({col_list}) INCLUDE ({col_list});"
    out.append(Suggestion(
        suggestion_type=SuggestionType.INDEX,
        rule_id=finding.rule_id, rule_name=finding.rule_name,
        risk=Risk.HIGH,
        title=f"为 {table} 创建覆盖索引",
        description=f"TABLE ACCESS BY INDEX ROWID 比例偏高，建议 INCLUDE 覆盖更多列减少回表",
        runnable_sql=ddl,
        params={"table": table, "columns": idx_cols},
        estimated_benefit="消除回表（Buffer Gets 下降 50%+）",
        side_effects="索引体积增大，需确保 INCLUDE 列数量可控",
    ))
    return out


@_register("R-03")
def _gen_r03_cardinality(finding: Finding, ctx: RuleContext) -> List[Suggestion]:
    """基数偏差 → 统计刷新 + 临时 hint."""
    out: List[Suggestion] = []
    tbl = _table_from_object_name(finding.object_name) or _guess_main_table(ctx)
    if tbl:
        owner, table = tbl
        out.append(Suggestion(
            suggestion_type=SuggestionType.STATS,
            rule_id=finding.rule_id, rule_name=finding.rule_name,
            risk=Risk.LOW,
            title=f"刷新 {table} 统计信息",
            description="基数偏差通常是统计陈旧导致",
            runnable_sql=f"EXEC DBMS_STATS.GATHER_TABLE_STATS(OWNNAME=>'{owner or ''}', TABNAME=>'{table}', CASCADE=>TRUE, ESTIMATE_PERCENT=>DBMS_STATS.AUTO_SAMPLE_SIZE);",
            params={"table": table},
            estimated_benefit="重新收集后优化器可能选到正确的 Join 顺序",
            side_effects="收集期间会扫描全表，建议在维护窗口执行",
        ))
    # Hint 兜底
    out.append(Suggestion(
        suggestion_type=SuggestionType.HINT,
        rule_id=finding.rule_id, rule_name=finding.rule_name,
        risk=Risk.MEDIUM,
        title="临时 CARDINALITY hint 固定行数估计",
        description=f"基数偏差 {finding.evidence.get('deviation', '?')}×，可用 hint 强制修正",
        runnable_sql="/*+ CARDINALITY(<table> <rows>) */  -- 请填入实际表和期望行数",
        params={},
        estimated_benefit="应急修正执行计划",
        side_effects="治标不治本，统计刷新后应移除",
    ))
    return out


@_register("R-04")
def _gen_r04_join_imbalance(finding: Finding, ctx: RuleContext) -> List[Suggestion]:
    out: List[Suggestion] = [Suggestion(
        suggestion_type=SuggestionType.HINT,
        rule_id=finding.rule_id, rule_name=finding.rule_name,
        risk=Risk.MEDIUM,
        title="LEADING hint 强制小表驱动",
        description="Join 两侧不均衡，小表应该在左（驱动）",
        runnable_sql="/*+ LEADING(<small_table> <big_table>) USE_NL(<big_table>) */  -- 请替换为实际表名",
        params={},
        estimated_benefit="避免大表被当作驱动表",
        side_effects="NL hint 在小表大场景比 HASH 更差；务必确认行数",
    )]
    return out


@_register("R-05")
def _gen_r05_sort_disk(finding: Finding, ctx: RuleContext) -> List[Suggestion]:
    out: List[Suggestion] = [Suggestion(
        suggestion_type=SuggestionType.REWRITE,
        rule_id=finding.rule_id, rule_name=finding.rule_name,
        risk=Risk.CRITICAL,
        title="考虑 PGA_AGGREGATE_TARGET / SORT_AREA_SIZE 调优",
        description=f"排序使用磁盘 {finding.evidence.get('temp_spc_mb', '?')} MB，可从两个方向解决",
        runnable_sql="""-- 方向 1: 会话级（立即生效，仅影响当前会话）
ALTER SESSION SET WORKAREA_SIZE_POLICY=MANUAL;
ALTER SESSION SET SORT_AREA_SIZE=524288;  -- 512KB，按实际调整

-- 方向 2: 系统级调大 PGA_AGGREGATE_TARGET（需 DBA + 评估 memory_target）
-- ALTER SYSTEM SET PGA_AGGREGATE_TARGET=2G SCOPE=BOTH;""",
        params={},
        estimated_benefit="减少磁盘 I/O，排序速度翻倍",
        side_effects="PGA 增大 = 内存占用增大；别超 SGA + PGA 总和上限",
    )]
    return out


@_register("R-06")
def _gen_r06_cartesian(finding: Finding, ctx: RuleContext) -> List[Suggestion]:
    """笛卡尔积是业务逻辑问题，只给人工 review 建议."""
    return [Suggestion(
        suggestion_type=SuggestionType.REWRITE,
        rule_id=finding.rule_id, rule_name=finding.rule_name,
        risk=Risk.CRITICAL,
        title="🔴 缺少 Join 条件 — 请立即人工审查 SQL",
        description="MERGE JOIN CARTESIAN 通常意味着 WHERE 或 ON 子句漏了 Join 条件",
        runnable_sql="""-- 请检查以下点：
-- 1) FROM 子句多张表时，WHERE 是否有关联条件
-- 2) ANSI JOIN 的 ON 子句是否为空或被注释
-- 3) 是否本应写 INNER JOIN 却漏了 ON""",
        params={},
        estimated_benefit="无 — 修正后 SQL 才是可运行的",
        side_effects="这不是优化问题而是正确性问题，必须修复",
    )]


@_register("R-07")
def _gen_r07_connect_by_no_index(finding: Finding, ctx: RuleContext) -> List[Suggestion]:
    out: List[Suggestion] = []
    tbl = _table_from_object_name(finding.object_name)
    if not tbl:
        return out
    owner, table = tbl
    idx_name = f"IDX_{table[:20]}_HIER".upper()
    # 简化：parent_id / child_id 作为递归索引 — 实际应由业务列替换
    out.append(Suggestion(
        suggestion_type=SuggestionType.INDEX,
        rule_id=finding.rule_id, rule_name=finding.rule_name,
        risk=Risk.HIGH,
        title=f"为 {table} 建立递归索引",
        description="CONNECT BY 递归查询应在父节点/子节点列上有索引",
        runnable_sql=f"CREATE INDEX {owner + '.' if owner else ''}{idx_name} ON {owner + '.' if owner else ''}{table}(parent_id, {table[:20]}_id);  -- 请替换为实际父/子列名",
        params={"table": table},
        estimated_benefit="消除递归全表扫描，层次查询 O(N) → O(log N)",
        side_effects="索引需与 CONNECT BY PRIOR 条件对齐",
    ))
    return out


@_register("R-08")
def _gen_r08_function_wrap(finding: Finding, ctx: RuleContext) -> List[Suggestion]:
    out: List[Suggestion] = [Suggestion(
        suggestion_type=SuggestionType.REWRITE,
        rule_id=finding.rule_id, rule_name=finding.rule_name,
        risk=Risk.MEDIUM,
        title="改写谓词方向让列裸奔",
        description=f"发现函数包裹列 —— {finding.evidence.get('predicates', '')[:60]}",
        runnable_sql="""-- 坏: WHERE TO_CHAR(ORDER_DATE) = '2024-01-01'
-- 好: WHERE ORDER_DATE >= DATE '2024-01-01' AND ORDER_DATE < DATE '2024-01-02'
--
-- 坏: WHERE TRUNC(created_at) = SYSDATE
-- 好: WHERE created_at >= TRUNC(SYSDATE) AND created_at < TRUNC(SYSDATE) + 1""",
        params={},
        estimated_benefit="让现有索引可以被使用",
        side_effects="改写需确认业务口径（TRUNC 边界条件等价）",
    )]
    out.append(Suggestion(
        suggestion_type=SuggestionType.INDEX,
        rule_id=finding.rule_id, rule_name=finding.rule_name,
        risk=Risk.HIGH,
        title="函数索引（当改写不适用时）",
        description="在被包裹的列上建立函数索引",
        runnable_sql="CREATE INDEX IDX_FUNC_xxx ON <OWNER>.<TABLE>(TRUNC(<COLUMN>));  -- 替换成实际函数和列",
        params={},
        estimated_benefit="无需改写 SQL 即可走索引",
        side_effects="函数索引是 DDL 变更；注意 DML 性能影响",
    ))
    return out


@_register("R-09")
def _gen_r09_high_cf(finding: Finding, ctx: RuleContext) -> List[Suggestion]:
    out: List[Suggestion] = [Suggestion(
        suggestion_type=SuggestionType.STATS,
        rule_id=finding.rule_id, rule_name=finding.rule_name,
        risk=Risk.MEDIUM,
        title="重建索引 / 按排序列重排",
        description=f"CLUSTERING_FACTOR 比值偏高（{finding.evidence.get('ratio', '?')}），表数据顺序与索引列不匹配",
        runnable_sql=f"""-- 方向 1: 重建索引
ALTER INDEX {finding.object_name} REBUILD ONLINE;

-- 方向 2: 按查询 ORDER BY 列重建表（需要停机）
CREATE TABLE {finding.object_name}_NEW AS
  SELECT * FROM {finding.object_name} ORDER BY <index_columns>;""",
        params={"index": finding.object_name},
        estimated_benefit="CLUSTERING_FACTOR 接近理想值 = 回表代价显著下降",
        side_effects="ALTER INDEX REBUILD 需要在线 DDL 权限；按排序列重排表是大手术",
    )]
    return out


@_register("R-10")
def _gen_r10_version_explosion(finding: Finding, ctx: RuleContext) -> List[Suggestion]:
    out: List[Suggestion] = [Suggestion(
        suggestion_type=SuggestionType.REWRITE,
        rule_id=finding.rule_id, rule_name=finding.rule_name,
        risk=Risk.MEDIUM,
        title="检查应用是否使用绑定变量",
        description=f"CHILD_NUMBER={finding.evidence.get('child_number', '?')} 说明有大量不同子版本",
        runnable_sql="""-- DBA 应急：系统级强制绑定（12c+ 推荐 FORCE，旧版谨慎用 SIMILAR）
ALTER SYSTEM SET CURSOR_SHARING=FORCE SCOPE=BOTH;

-- 应用侧：替换所有拼字符串 SQL 为 :bind 变量
-- 坏: f"SELECT * FROM t WHERE id={user_id}"
-- 好: SELECT * FROM t WHERE id = :user_id""",
        params={},
        estimated_benefit="Library Cache 命中率提升，软解析率上升",
        side_effects="CURSOR_SHARING=FORCE 对复杂谓词可能产生次优执行计划",
    )]
    return out


@_register("R-11")
def _gen_r11_high_parse(finding: Finding, ctx: RuleContext) -> List[Suggestion]:
    out: List[Suggestion] = [Suggestion(
        suggestion_type=SuggestionType.REWRITE,
        rule_id=finding.rule_id, rule_name=finding.rule_name,
        risk=Risk.LOW,
        title="应用侧：使用绑定变量 + 连接池（应用层修正）",
        description="PARSE_CALLS / EXECUTIONS 偏高 —— 每条 SQL 都在硬解析",
        runnable_sql="""-- JDBC: 使用 PreparedStatement + 连接池 (HikariCP / Druid)
-- JDBC 连接串加: ?cachePrepStmts=true&prepStmtCacheSize=250

-- Python (oracledb): 使用 bind variables
cursor.execute("SELECT * FROM t WHERE id = :id", {"id": user_id})
# 而非: cursor.execute(f"SELECT * FROM t WHERE id = {user_id}")""",
        params={},
        estimated_benefit="PARSE_CALLS 下降 90%+，Library Cache 减压",
        side_effects="应用代码需要 review 和修改",
    )]
    return out


@_register("R-12")
def _gen_r12_stale_stats(finding: Finding, ctx: RuleContext) -> List[Suggestion]:
    out: List[Suggestion] = []
    tbl = _table_from_object_name(finding.object_name)
    if tbl:
        owner, table = tbl
        out.append(Suggestion(
            suggestion_type=SuggestionType.STATS,
            rule_id=finding.rule_id, rule_name=finding.rule_name,
            risk=Risk.LOW,
            title=f"收集 {table} 统计信息",
            description=finding.description,
            runnable_sql=(
                f"EXEC DBMS_STATS.GATHER_TABLE_STATS(\n"
                f"  OWNNAME=>'{owner or ''}', TABNAME=>'{table}', CASCADE=>TRUE,\n"
                f"  ESTIMATE_PERCENT=>DBMS_STATS.AUTO_SAMPLE_SIZE,\n"
                f"  METHOD_OPT=>'FOR ALL COLUMNS SIZE AUTO',\n"
                f"  DEGREE=>DBMS_STATS.AUTO_DEGREE);"
            ),
            params={"table": table},
            estimated_benefit="让优化器重新选对执行计划 —— 这是最常见的慢 SQL 根因修复",
            side_effects="大表收集期间有轻微扫描开销，建议非高峰运行",
        ))
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _guess_main_table(ctx: RuleContext) -> Optional[tuple[str, str]]:
    """Pick the biggest table from ctx (no evidence → use biggest num_rows)."""
    if not ctx.tables:
        return None
    tbl = max(ctx.tables, key=lambda t: t.num_rows or 0)
    return tbl.owner, tbl.table_name
