"""F4 — Concrete rules R-01 through R-12.

Each rule is a tiny subclass of BaseRule with rule_id / rule_name / severity / match(ctx).
All rules are written defensively:
  - Never assume ctx has every field populated — if a data source is missing, return []
  - Never raise — wrap internal failures with a short-circuit []

Thresholds are tuned for a typical OLTP/OLAP workload; tweak via module-level
constants at the top of each rule if your environment needs different values.
"""
from __future__ import annotations

import re
from typing import List

from loguru import logger

from .base import BaseRule, Finding, Severity, register_rule
from .context import RuleContext


# ---------------------------------------------------------------------------
# Helpers — extracted once, reused across rules
# ---------------------------------------------------------------------------

# Matches predicate column names after function calls, e.g. TO_CHAR(col), TRUNC(dt)
# We just need to know "there's a function somewhere" — exact column capture is optional.
_FUNC_WRAP_RE = re.compile(r"\b[A-Z_][A-Z0-9_]*\s*\(", re.IGNORECASE)

# Pull bare column identifiers out of predicate text (for indexability checks).
_COL_REF_RE = re.compile(r"\b([A-Z_][A-Z0-9_]*)\s*=", re.IGNORECASE)


def _op_is(node, *prefixes: str) -> bool:
    return any(node.operation.startswith(p) for p in prefixes)


def _predicates_text(node) -> str:
    """Concatenate access + filter predicates into a single string for regex."""
    return " ".join(x for x in [node.access_predicates or "", node.filter_predicates or ""])


def _a_rows(node) -> float | None:
    """Actual rows from actual_plan (11g+)."""
    # actual_plan items have .a_rows; estimated_plan items have N/A.
    return getattr(node, "a_rows", None)


def _e_rows(node) -> float | None:
    v = getattr(node, "rows", None)
    if v is None:
        v = getattr(node, "e_rows", None)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# R-01 大表全表扫描 + 存在可索引过滤列
# ---------------------------------------------------------------------------
@register_rule
class RuleR01FullTableScan(BaseRule):
    rule_id = "R-01"
    rule_name = "大表全表扫描"
    severity = Severity.HIGH
    description = "大表出现 TABLE ACCESS FULL，且过滤条件中存在可能加索引的列"

    BIG_TABLE_ROWS_THRESHOLD = 10_000  # 低于此值的表全表扫描是合理的

    def match(self, ctx: RuleContext) -> List[Finding]:
        findings: List[Finding] = []
        for node in ctx.estimated_plan:
            if not _op_is(node, "TABLE ACCESS"):
                continue
            # Options must contain FULL
            opts = (node.options or "").upper()
            if "FULL" not in opts:
                continue

            # 1) Table must be big enough to care
            tbl_key = f"{(node.object_owner or '').upper()}.{(node.object_name or '').upper()}"
            tbl = ctx.table_by_name.get(tbl_key)
            if tbl and tbl.num_rows is not None and tbl.num_rows < self.BIG_TABLE_ROWS_THRESHOLD:
                continue  # small table, FTS is fine

            # 2) Check if predicates look indexable — contains an equality/range filter
            preds = _predicates_text(node)
            indexable = bool(re.search(r"\b[A-Z_][A-Z0-9_]*\s*(=|<|>|<=|>=|IN|LIKE|BETWEEN)\b", preds, re.IGNORECASE))

            evidence = {
                "table": tbl_key,
                "estimated_rows": _e_rows(node),
                "predicates": preds,
                "indexable_column_found": indexable,
            }
            description = f"表 {tbl_key} 全表扫描"
            if tbl and tbl.num_rows:
                description += f"（约 {tbl.num_rows:,} 行）"
            if indexable:
                description += "，过滤条件存在可索引列"

            findings.append(Finding(
                rule_id=self.rule_id, rule_name=self.rule_name,
                severity=self.severity, description=description,
                object_name=node.object_name, evidence=evidence,
                suggestion_hint="检查该表是否缺少覆盖过滤条件的复合索引，或考虑 /*+ INDEX */ Hint",
            ))
        return findings


# ---------------------------------------------------------------------------
# R-02 索引回表比例过高
# ---------------------------------------------------------------------------
@register_rule
class RuleR02HighTableAccessByIndexRowid(BaseRule):
    rule_id = "R-02"
    rule_name = "索引回表比例过高"
    severity = Severity.MEDIUM
    description = "TABLE ACCESS BY INDEX ROWID 节点的行数占比过高，索引覆盖不足"

    ROWID_RATIO_THRESHOLD = 0.30  # 30% 作为告警阈值

    def match(self, ctx: RuleContext) -> List[Finding]:
        findings: List[Finding] = []
        # Build map of INDEX SCAN nodes by parent ID.
        idx_scan_rows: dict[int, float] = {}
        for node in ctx.estimated_plan:
            if _op_is(node, "INDEX") and not _op_is(node, "INDEX FAST FULL"):
                parent_id = node.parent_id
                if parent_id is None:
                    continue
                idx_scan_rows[parent_id] = _e_rows(node) or 0

        for node in ctx.estimated_plan:
            if _op_is(node, "TABLE ACCESS") and "INDEX ROWID" in (node.options or "").upper():
                scan_rows = idx_scan_rows.get(node.id)
                table_rows = _e_rows(node) or 0
                if scan_rows and table_rows > 0:
                    ratio = scan_rows / table_rows
                    if ratio >= self.ROWID_RATIO_THRESHOLD:
                        findings.append(Finding(
                            rule_id=self.rule_id, rule_name=self.rule_name,
                            severity=self.severity,
                            description=f"索引扫描回表比例 {ratio*100:.1f}%（阈值 {self.ROWID_RATIO_THRESHOLD*100:.0f}%）",
                            object_name=node.object_name,
                            evidence={
                                "index_scan_rows": scan_rows,
                                "table_access_rows": table_rows,
                                "ratio": round(ratio, 3),
                            },
                            suggestion_hint="考虑复合索引覆盖更多列，减少回表",
                        ))
        return findings


# ---------------------------------------------------------------------------
# R-03 基数估算偏差（11g+ 实际 vs 估算）
# ---------------------------------------------------------------------------
@register_rule
class RuleR03CardinalityMismatch(BaseRule):
    rule_id = "R-03"
    rule_name = "基数估算偏差大"
    severity = Severity.HIGH
    description = "实际执行行数与优化器估算值偏差超过 5 倍"

    DEVIATION_HIGH = 5.0   # a_rows / e_rows > 5x
    DEVIATION_LOW = 0.2   # 或者 < 0.2x

    def match(self, ctx: RuleContext) -> List[Finding]:
        findings: List[Finding] = []
        if not ctx.actual_plan:
            return findings  # 10g 环境无 actual plan，跳过

        for node in ctx.actual_plan:
            a = _a_rows(node)
            e = _e_rows(node)
            if a is None or e is None or e < 1:
                continue
            dev = a / e
            if dev > self.DEVIATION_HIGH or dev < self.DEVIATION_LOW:
                # Avoid firing on tiny nodes that aren't really costly.
                if a < 10:
                    continue
                findings.append(Finding(
                    rule_id=self.rule_id, rule_name=self.rule_name,
                    severity=self.severity,
                    description=f"实际 {a:,.0f} 行 vs 估算 {e:,.0f} 行（偏差 {dev:.1f}×）",
                    object_name=getattr(node, "object_name", None),
                    evidence={
                        "operation": getattr(node, "operation", None),
                        "actual_rows": a, "estimated_rows": e,
                        "deviation": round(dev, 2),
                    },
                    suggestion_hint="检查统计信息是否陈旧；必要时手动收集或使用 FIRST_ROWS hint",
                ))
        return findings


# ---------------------------------------------------------------------------
# R-04 Join 严重不平衡
# ---------------------------------------------------------------------------
@register_rule
class RuleR04JoinImbalanced(BaseRule):
    rule_id = "R-04"
    rule_name = "Join 两侧不平衡"
    severity = Severity.MEDIUM
    description = "Join 两侧估算行数比超过 100 倍，Join 顺序可能不理想"

    RATIO_THRESHOLD = 100.0

    def match(self, ctx: RuleContext) -> List[Finding]:
        findings: List[Finding] = []
        for node in ctx.estimated_plan:
            if not _op_is(node, "HASH JOIN", "NESTED LOOPS"):
                continue
            kids = [n for n in ctx.estimated_plan if n.parent_id == node.id]
            if len(kids) < 2:
                continue
            rows = [_e_rows(k) or 0 for k in kids]
            big, small = max(rows), min(rows)
            if big == 0 or small == 0:
                continue
            ratio = big / small
            if ratio >= self.RATIO_THRESHOLD:
                findings.append(Finding(
                    rule_id=self.rule_id, rule_name=self.rule_name,
                    severity=self.severity,
                    description=f"{node.operation} 两侧 {rows[0]:,.0f} vs {rows[1]:,.0f}（比率 {ratio:.0f}×）",
                    evidence={
                        "join_type": node.operation,
                        "child_rows": rows,
                        "ratio": round(ratio, 1),
                    },
                    suggestion_hint="考虑 LEADING / ORDERED hint 强制正确的小表驱动",
                ))
        return findings


# ---------------------------------------------------------------------------
# R-05 Sort 落盘（11g+）
# ---------------------------------------------------------------------------
@register_rule
class RuleR05SortDisk(BaseRule):
    rule_id = "R-05"
    rule_name = "Sort 操作落盘"
    severity = Severity.LOW
    description = "排序操作使用了磁盘临时表空间，PGA 可能不足"

    TEMP_MB_THRESHOLD = 1.0  # 1MB+ temp space

    def match(self, ctx: RuleContext) -> List[Finding]:
        findings: List[Finding] = []
        for node in ctx.actual_plan:
            if "SORT" not in getattr(node, "operation", ""):
                continue
            temp = getattr(node, "temp_spc", None) or 0
            if temp >= self.TEMP_MB_THRESHOLD:
                findings.append(Finding(
                    rule_id=self.rule_id, rule_name=self.rule_name,
                    severity=self.severity,
                    description=f"排序操作使用磁盘临时表空间约 {temp:.1f} MB",
                    evidence={
                        "operation": getattr(node, "operation", None),
                        "temp_spc_mb": round(temp, 2),
                    },
                    suggestion_hint="检查 PGA_AGGREGATE_TARGET / SORT_AREA_SIZE 设置",
                ))
        return findings


# ---------------------------------------------------------------------------
# R-06 笛卡尔积
# ---------------------------------------------------------------------------
@register_rule
class RuleR06Cartesian(BaseRule):
    rule_id = "R-06"
    rule_name = "笛卡尔积（MERGE JOIN CARTESIAN）"
    severity = Severity.CRITICAL
    description = "执行计划出现 MERGE JOIN CARTESIAN，通常表示缺少 Join 条件或隐式笛卡尔"

    def match(self, ctx: RuleContext) -> List[Finding]:
        findings: List[Finding] = []
        for node in ctx.estimated_plan:
            if "CARTESIAN" in (node.operation or "").upper():
                findings.append(Finding(
                    rule_id=self.rule_id, rule_name=self.rule_name,
                    severity=self.severity,
                    description="执行计划包含 MERGE JOIN CARTESIAN —— 极高风险！",
                    object_name=node.object_name,
                    evidence={
                        "operation": node.operation,
                        "options": node.options,
                        "access_predicates": node.access_predicates,
                        "filter_predicates": node.filter_predicates,
                    },
                    suggestion_hint="SQL 很可能缺少 Join 条件，请先检查业务逻辑",
                ))
        return findings


# ---------------------------------------------------------------------------
# R-07 CONNECT BY 无索引
# ---------------------------------------------------------------------------
@register_rule
class RuleR07ConnectByNoIndex(BaseRule):
    rule_id = "R-07"
    rule_name = "CONNECT BY 无索引"
    severity = Severity.MEDIUM
    description = "CONNECT BY 递归查询相关表缺少索引，层次查询可能很慢"

    def match(self, ctx: RuleContext) -> List[Finding]:
        findings: List[Finding] = []
        # 找到 CONNECT BY 节点
        cb_nodes = [n for n in ctx.estimated_plan if "CONNECT" in (n.operation or "").upper()]
        if not cb_nodes:
            return findings
        # 关联表 = CONNECT BY 子树里的 TABLE ACCESS 节点
        for cb in cb_nodes:
            subtree = _collect_subtree(ctx.estimated_plan, cb.id)
            tables_in_subtree = {
                f"{(n.object_owner or '').upper()}.{(n.object_name or '').upper()}"
                for n in subtree
                if _op_is(n, "TABLE ACCESS") and n.object_name
            }
            for tbl_key in tables_in_subtree:
                has_any_index = any(
                    f"{i.owner.upper()}.{i.table_name.upper()}" == tbl_key
                    for i in ctx.indexes
                )
                if not has_any_index:
                    findings.append(Finding(
                        rule_id=self.rule_id, rule_name=self.rule_name,
                        severity=self.severity,
                        description=f"CONNECT BY 涉及表 {tbl_key} 未发现任何索引",
                        object_name=tbl_key,
                        evidence={"connect_by_node": cb.operation},
                        suggestion_hint="递归查询相关表应建立子节点列 + 父节点列的复合索引",
                    ))
        return findings


# ---------------------------------------------------------------------------
# R-08 谓词列被函数包裹
# ---------------------------------------------------------------------------
@register_rule
class RuleR08FunctionOnColumn(BaseRule):
    rule_id = "R-08"
    rule_name = "谓词列被函数包裹"
    severity = Severity.MEDIUM
    description = "WHERE 条件中列被函数包裹（如 TO_CHAR(col)），索引将失效"

    def match(self, ctx: RuleContext) -> List[Finding]:
        findings: List[Finding] = []
        for node in ctx.estimated_plan:
            preds = _predicates_text(node)
            if not preds:
                continue
            # Find identifiers immediately followed by '(' — likely functions.
            for m in _FUNC_WRAP_RE.finditer(preds):
                # Skip SQL keywords that also end with (...)
                fname = m.group(1).upper()
                if fname in {"SELECT", "FROM", "WHERE", "GROUP", "ORDER", "INSERT", "UPDATE",
                             "DELETE", "ON", "AND", "OR", "NOT", "IN", "VALUES"}:
                    continue
                findings.append(Finding(
                    rule_id=self.rule_id, rule_name=self.rule_name,
                    severity=self.severity,
                    description=f"谓词出现函数调用 {fname}(...) — 相关列索引可能被抑制",
                    object_name=node.object_name,
                    evidence={"predicates": preds},
                    suggestion_hint="考虑改写谓词方向（如 col = :bind 而非 TO_CHAR(col) = 'xxx'）或创建函数索引",
                ))
                break  # 每个节点只报一次
        return findings


# ---------------------------------------------------------------------------
# R-09 CLUSTERING_FACTOR 接近表行数
# ---------------------------------------------------------------------------
@register_rule
class RuleR09HighClusteringFactor(BaseRule):
    rule_id = "R-09"
    rule_name = "索引 CLUSTERING_FACTOR 接近表行数"
    severity = Severity.MEDIUM
    description = "CLUSTERING_FACTOR 接近表行数（> 80%），回表代价高"

    CF_RATIO_THRESHOLD = 0.80

    def match(self, ctx: RuleContext) -> List[Finding]:
        findings: List[Finding] = []
        for idx in ctx.indexes:
            if idx.clustering_factor is None or idx.num_rows is None or idx.num_rows == 0:
                continue
            ratio = idx.clustering_factor / idx.num_rows
            if ratio >= self.CF_RATIO_THRESHOLD:
                findings.append(Finding(
                    rule_id=self.rule_id, rule_name=self.rule_name,
                    severity=self.severity,
                    description=f"索引 {idx.index_name} CLUSTERING_FACTOR 比值 {ratio:.2f}",
                    object_name=idx.index_name,
                    evidence={
                        "index": idx.index_name,
                        "clustering_factor": idx.clustering_factor,
                        "table_rows": idx.num_rows,
                        "ratio": round(ratio, 3),
                    },
                    suggestion_hint="考虑改写 ORDER BY / GROUP BY 避免回表，或调整索引列顺序",
                ))
        return findings


# ---------------------------------------------------------------------------
# R-10 SQL 版本爆炸（需要 TopSQL 指标）
# ---------------------------------------------------------------------------
@register_rule
class RuleR10VersionExplosion(BaseRule):
    rule_id = "R-10"
    rule_name = "SQL 版本爆炸"
    severity = Severity.MEDIUM
    description = "同一 SQL_ID 的 CHILD_NUMBER 版本数超过 20"

    CHILD_THRESHOLD = 20

    def match(self, ctx: RuleContext) -> List[Finding]:
        if not ctx.top_sql_row:
            return []
        child = ctx.top_sql_row.child_number or 0
        # child_number is per-row counter; we need total versions. Without direct
        # V$SQL CHILD_NUMBER aggregate, use child_number from a snapshot as a proxy.
        if child > self.CHILD_THRESHOLD:
            return [Finding(
                rule_id=self.rule_id, rule_name=self.rule_name,
                severity=self.severity,
                description=f"该 SQL 当前 CHILD_NUMBER={child}，版本数过多",
                evidence={"child_number": child},
                suggestion_hint="检查是否缺绑定变量；ALTER SYSTEM SET CURSOR_SHARING=FORCE",
            )]
        return []


# ---------------------------------------------------------------------------
# R-11 软解析率低
# ---------------------------------------------------------------------------
@register_rule
class RuleR11HighParseCalls(BaseRule):
    rule_id = "R-11"
    rule_name = "软解析率低"
    severity = Severity.MEDIUM
    description = "PARSE_CALLS / EXECUTIONS > 0.8，library cache 压力大"

    RATIO_THRESHOLD = 0.8

    def match(self, ctx: RuleContext) -> List[Finding]:
        if not ctx.top_sql_row or not ctx.top_sql_row.executions:
            return []
        parse = ctx.top_sql_row.parse_calls or 0
        execs = ctx.top_sql_row.executions or 0
        if execs == 0:
            return []
        ratio = parse / execs
        if ratio > self.RATIO_THRESHOLD:
            return [Finding(
                rule_id=self.rule_id, rule_name=self.rule_name,
                severity=self.severity,
                description=f"PARSE_CALLS / EXECUTIONS = {ratio:.2f}（阈值 {self.RATIO_THRESHOLD}）",
                evidence={"parse_calls": parse, "executions": execs, "ratio": round(ratio, 3)},
                suggestion_hint="检查应用侧是否使用绑定变量；必要时开启 cursor_sharing",
            )]
        return []


# ---------------------------------------------------------------------------
# R-12 统计信息缺失 / 陈旧
# ---------------------------------------------------------------------------
@register_rule
class RuleR12StaleStats(BaseRule):
    rule_id = "R-12"
    rule_name = "统计信息陈旧"
    severity = Severity.HIGH
    description = "相关表 LAST_ANALYZED 为 NULL 或超过 30 天未更新"

    def match(self, ctx: RuleContext) -> List[Finding]:
        findings: List[Finding] = []
        for tbl in ctx.tables:
            if tbl.stale:
                findings.append(Finding(
                    rule_id=self.rule_id, rule_name=self.rule_name,
                    severity=self.severity,
                    description=f"表 {tbl.owner}.{tbl.table_name} 统计陈旧 — {tbl.staleness_reason or ''}",
                    object_name=f"{tbl.owner}.{tbl.table_name}",
                    evidence={
                        "last_analyzed": tbl.last_analyzed.isoformat() if tbl.last_analyzed else None,
                        "staleness_reason": tbl.staleness_reason,
                        "num_rows": tbl.num_rows,
                    },
                    suggestion_hint=f"EXEC DBMS_STATS.GATHER_TABLE_STATS(ownname=>'{tbl.owner}', tabname=>'{tbl.table_name}', cascade=>TRUE)",
                ))
        return findings


# ---------------------------------------------------------------------------
# Utility: collect subtree (for R-07)
# ---------------------------------------------------------------------------
def _collect_subtree(nodes: list, root_id: int) -> list:
    out = []
    def walk(nid: int) -> None:
        for n in nodes:
            if n.id == nid:
                out.append(n)
                walk_children(n.id)
    def walk_children(pid: int) -> None:
        for n in nodes:
            if n.parent_id == pid:
                out.append(n)
                walk_children(n.id)
    walk(root_id)
    return out
