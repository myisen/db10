"""F4 — Rule engine package.

One rule per concern; the engine aggregates Findings ordered by severity.
See ``engine.run_all_rules`` for the public entry point.
"""
from .base import BaseRule, Finding, Severity, get_all_rules, register_rule
from .context import RuleContext
from .engine import run_all_rules, summarize

# Import rules to trigger auto-registration of @register_rule decorators.
from . import rules  # noqa: F401  (side-effect: populates registry)

__all__ = [
    "BaseRule",
    "Finding",
    "Severity",
    "RuleContext",
    "get_all_rules",
    "register_rule",
    "run_all_rules",
    "summarize",
]
