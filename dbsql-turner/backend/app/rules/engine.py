"""Rule engine entry point — dispatch all rules over a RuleContext."""
from __future__ import annotations

from typing import List

from loguru import logger

from .base import Finding, Severity, get_all_rules
from .context import RuleContext


SEVERITY_ORDER = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW]


def run_all_rules(ctx: RuleContext) -> List[Finding]:
    """Run every registered rule against ``ctx`` and return Findings sorted by severity desc."""
    findings: List[Finding] = []
    for rule in get_all_rules():
        try:
            matched = rule.match(ctx)
            if matched:
                findings.extend(matched)
                logger.debug(f"Rule {rule.rule_id}: {len(matched)} finding(s)")
        except Exception as e:
            # Never let one rule crash the whole engine — log and continue.
            logger.warning(f"Rule {rule.rule_id} raised: {e}")
    findings.sort(key=lambda f: (f.severity.value, f.rule_id), reverse=True)
    logger.info(f"Diagnosis complete. {len(findings)} finding(s), {len(get_all_rules())} rule(s) registered.")
    return findings


def summarize(findings: List[Finding]) -> str:
    """Short human summary for the API response."""
    counts = {sev: 0 for sev in SEVERITY_ORDER}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    total = len(findings)
    if total == 0:
        return "✅ 无诊断发现，SQL 结构看起来健康"
    parts = []
    for sev in SEVERITY_ORDER:
        if counts.get(sev, 0):
            parts.append(f"{sev.label}: {counts[sev]}")
    return f"共发现 {total} 个诊断问题 —— " + " | ".join(parts)
