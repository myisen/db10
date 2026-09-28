"""F5 — Suggestion generator package.

Input : a list of Findings (from F4 rules engine) + RuleContext (F2/F3 data).
Output: a list of actionable Suggestions — each one carries runnable SQL
        (hint / CREATE INDEX / DBMS_STATS / rewrite template).

One generator per suggestion type; ``engine.generate()`` is the entry point.
"""
from .base import Suggestion, SuggestionType
from .generators import generate_all

__all__ = ["Suggestion", "SuggestionType", "generate_all"]
