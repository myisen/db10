"""Rule base class + registry + Finding DTO.

Rule design goals:
- Each rule is a tiny Python function (via subclass).
- Each rule produces 0..N Findings — one per occurrence.
- Rules *must* be safe to call when the data they need is missing
  (return an empty list rather than raising).
- Registry is populated by ``@register_rule`` at import time; rules.py
  does ``from . import rules`` in __init__.py as a side-effect.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Optional

from loguru import logger

from .context import RuleContext


class Severity(IntEnum):
    """Higher number = more severe."""
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @property
    def label(self) -> str:
        return {
            Severity.LOW: "低",
            Severity.MEDIUM: "中",
            Severity.HIGH: "高",
            Severity.CRITICAL: "极高",
        }[self]


@dataclass
class Finding:
    """One rule hit."""

    rule_id: str
    rule_name: str
    severity: Severity
    description: str               # Why we flagged this — human-readable
    object_name: Optional[str] = None   # Table/index name, if applicable
    evidence: dict = field(default_factory=dict)  # Raw values that led to the hit (front-end debugging)
    suggestion_hint: Optional[str] = None   # Hint/action suggestion placeholder for F5

    # Convenience for API layer
    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "severity": self.severity.label,
            "severity_code": self.severity.value,
            "description": self.description,
            "object_name": self.object_name,
            "evidence": self.evidence,
            "suggestion_hint": self.suggestion_hint,
        }


class BaseRule:
    """All rules inherit from this."""

    rule_id: str = ""            # e.g. "R-01"
    rule_name: str = ""          # e.g. "大表全表扫描"
    severity: Severity = Severity.MEDIUM
    description: str = ""        # one-line explanation of what the rule checks

    def match(self, ctx: RuleContext) -> List[Finding]:
        """Override in subclasses.  Return a list (0..N). Never raise."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, BaseRule] = {}


def register_rule(cls: type[BaseRule]) -> type[BaseRule]:
    """Class decorator.  Subclasses ``BaseRule`` and registers themselves."""
    instance = cls()
    if not instance.rule_id:
        raise ValueError(f"Rule {cls.__name__} missing rule_id")
    if instance.rule_id in _REGISTRY:
        raise ValueError(f"Duplicate rule_id: {instance.rule_id}")
    _REGISTRY[instance.rule_id] = instance
    logger.debug(f"Registered rule {instance.rule_id} ({instance.rule_name})")
    return cls


def get_all_rules() -> list[BaseRule]:
    """Return all registered rules, sorted by rule_id."""
    return [_REGISTRY[k] for k in sorted(_REGISTRY.keys())]


def get_rule(rule_id: str) -> Optional[BaseRule]:
    return _REGISTRY.get(rule_id)


def clear_rules() -> None:
    """Tests only."""
    _REGISTRY.clear()
