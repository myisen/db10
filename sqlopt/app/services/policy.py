"""PolicyService：SQL 类型过滤 + 执行白名单 + 高危关键字拦截。

规则（MVP 固定）：
  - 全局默认策略：
      SELECT   → allow_exec=True
      INSERT/UPDATE/DELETE/DDL/PLSQL → allow_exec=False（自动回滚）
  - 高危关键字硬拦截（无论如何都不准执行）：
      ALTER SYSTEM / DROP / TRUNCATE / CREATE USER / ALTER USER /
      GRANT / REVOKE / CREATE TABLE / CREATE INDEX / ALTER TABLE ...
  - 单语句检查：分号切分只取第一句
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from ..models.models import SqlPolicy


# 高危关键字（大写匹配，独立单词）
_DANGEROUS_PATTERNS = [
    re.compile(r"\bALTER\s+SYSTEM\b", re.I),
    re.compile(r"\bDROP\b", re.I),
    re.compile(r"\bTRUNCATE\b", re.I),
    re.compile(r"\bCREATE\s+USER\b", re.I),
    re.compile(r"\bALTER\s+USER\b", re.I),
    re.compile(r"\bGrant\b", re.I),
    re.compile(r"\bREVOKE\b", re.I),
    re.compile(r"\bCREATE\s+TABLE\b", re.I),
    re.compile(r"\bCREATE\s+INDEX\b", re.I),
    re.compile(r"\bALTER\s+TABLE\b", re.I),
    re.compile(r"\bALTER\s+INDEX\b", re.I),
    re.compile(r"\bCREATE\s+DATABASE\b", re.I),
    re.compile(r"\bCREATE\s+ROLE\b", re.I),
    re.compile(r"\bCREATE\s+PROFILE\b", re.I),
    re.compile(r"\bCREATE\s+PROCEDURE\b", re.I),
    re.compile(r"\bCREATE\s+FUNCTION\b", re.I),
    re.compile(r"\bCREATE\s+TRIGGER\b", re.I),
    re.compile(r"\bCREATE\s+PACKAGE\b", re.I),
    re.compile(r"\bALTER\s+DATABASE\b", re.I),
]


@dataclass
class PolicyDecision:
    allow: bool
    reason: str
    # 是否强制回滚（写 SQL 即使"允许"也会自动回滚）
    force_rollback: bool = False


# 写 SQL 类型 → 强制回滚
_WRITE_TYPES = {"INSERT", "UPDATE", "DELETE"}
_DDL_TYPES = {"DDL", "PLSQL"}


def strip_semicolon(sql: str) -> str:
    """取第一个语句（分号切分，过滤注释）。"""
    # 先去掉 -- 注释
    s = re.sub(r"--[^\n]*", "", sql or "")
    # 按分号取第一段
    if ";" in s:
        s = s.split(";")[0]
    return s.strip()


class PolicyService:
    """统一入口：先高危硬拦 → 再查策略表 → 最后应用层兜底。"""

    def __init__(self, db: Session):
        self.db = db
        self._ensure_default_policies()

    # ------------------------------------------------------------------
    def check_exec(self, stmt_type: str, sql: str, target_id: int | None = None) -> PolicyDecision:
        """评估一条 SQL 是否可以在线执行。"""
        # 0) 单语句检查
        one = strip_semicolon(sql)
        if not one:
            return PolicyDecision(False, "空 SQL")

        # 1) 高危关键字硬拦截
        for pat in _DANGEROUS_PATTERNS:
            if pat.search(one):
                return PolicyDecision(False, f"命中高危关键字: {pat.pattern}")

        # 2) 写 SQL / DDL / PLSQL → 强制回滚
        if stmt_type in _WRITE_TYPES:
            return PolicyDecision(
                True, f"{stmt_type} 类型 SQL 自动回滚（不会修改数据）",
                force_rollback=True,
            )
        if stmt_type in _DDL_TYPES:
            return PolicyDecision(False, f"{stmt_type} 类型 SQL 禁止执行")

        # 3) 查策略表
        allow = self._lookup_allow(stmt_type, target_id, sql)
        if allow is None:
            # 没有匹配策略 → 默认只允许 SELECT
            if stmt_type == "SELECT":
                return PolicyDecision(True, "默认策略：SELECT 允许执行")
            return PolicyDecision(False, f"无匹配策略，{stmt_type} 禁止执行")

        if allow:
            return PolicyDecision(True, "策略允许")
        return PolicyDecision(False, "策略禁止")

    # ------------------------------------------------------------------
    def _lookup_allow(self, stmt_type: str, target_id: int | None, sql: str) -> bool | None:
        """按 target_id 优先 → 全局兜底。"""
        # 按 stmt_type + target_id 精确匹配
        q = self.db.query(SqlPolicy).filter(SqlPolicy.stmt_type == stmt_type)
        if target_id is not None:
            q_specific = q.filter(SqlPolicy.target_id == target_id).order_by(SqlPolicy.id.desc())
            specific = q_specific.first()
            if specific:
                return self._match_pattern(specific, sql)
        # 全局
        global_ = q.filter(SqlPolicy.target_id.is_(None)).order_by(SqlPolicy.id.desc()).first()
        if global_:
            return self._match_pattern(global_, sql)
        return None

    @staticmethod
    def _match_pattern(policy: SqlPolicy, sql: str) -> bool:
        if policy.pattern_regex:
            try:
                if not re.search(policy.pattern_regex, sql, re.I):
                    return False
            except re.error:
                pass
        return policy.allow_exec

    # ------------------------------------------------------------------
    def _ensure_default_policies(self) -> None:
        """首次启动写入默认策略（幂等）。"""
        defaults = [
            ("全局默认-SELECT", None, "SELECT", None, True),
            ("全局默认-INSERT", None, "INSERT", None, False),
            ("全局默认-UPDATE", None, "UPDATE", None, False),
            ("全局默认-DELETE", None, "DELETE", None, False),
            ("全局默认-DDL", None, "DDL", None, False),
            ("全局默认-PLSQL", None, "PLSQL", None, False),
        ]
        for name, tid, st, pat, allow in defaults:
            exists = self.db.query(SqlPolicy).filter(SqlPolicy.name == name).first()
            if not exists:
                self.db.add(SqlPolicy(
                    name=name, target_id=tid, stmt_type=st,
                    pattern_regex=pat, allow_exec=allow,
                ))
        self.db.commit()
