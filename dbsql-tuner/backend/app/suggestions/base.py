"""Suggestion DTO + risk level.

Four suggestion types cover all 12 rules:
  - HINT     — inline /*+ */ hint to attach to the SQL
  - INDEX    — CREATE INDEX DDL (run by DBA)
  - STATS    — DBMS_STATS.GATHER... call
  - REWRITE  — SQL 改写模板（返回改写后的完整 SQL）

Each suggestion carries ``risk`` so the UI can warn about production impact:
  - LOW    — DBMS_STATS (safe, read-only)
  - MEDIUM — Hint (inline, scoped to this SQL, easy to revert)
  - HIGH   — CREATE INDEX (schema change + maintenance burden)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SuggestionType(str, Enum):
    HINT = "hint"
    INDEX = "index"
    STATS = "stats"
    REWRITE = "rewrite"


class Risk(str, Enum):
    LOW = "low"        # 收集统计信息等
    MEDIUM = "medium"  # Hint，只影响当前 SQL
    HIGH = "high"      # CREATE INDEX / DDL 变更
    CRITICAL = "critical"  # 业务逻辑改写


@dataclass
class Suggestion:
    """One actionable fix."""

    suggestion_type: SuggestionType
    rule_id: str                  # 对应的规则
    rule_name: str
    risk: Risk
    title: str                    # 一句话标题
    description: str              # 为什么建议这个
    runnable_sql: str              # 直接能执行的 SQL / hint 片段
    params: dict = field(default_factory=dict)  # 结构化参数（比如 index columns）
    estimated_benefit: Optional[str] = None     # 定性收益描述
    side_effects: Optional[str] = None          # 副作用提醒（索引空间占用等）

    def to_dict(self) -> dict:
        return {
            "suggestion_type": self.suggestion_type.value,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "risk": self.risk.value,
            "title": self.title,
            "description": self.description,
            "runnable_sql": self.runnable_sql,
            "params": self.params,
            "estimated_benefit": self.estimated_benefit,
            "side_effects": self.side_effects,
        }
