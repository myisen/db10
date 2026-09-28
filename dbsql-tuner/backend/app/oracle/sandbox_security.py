"""F6 — SQL 安全校验（沙箱执行器的第一道门）。

沙箱账号（sandbox_user）的设计目标：只能跑 SELECT / EXPLAIN PLAN / ALTER SESSION，
绝不能碰 DDL / DML / ALTER SYSTEM / GRANT / 动态拼 SQL 的 DBMS_SQL。

两层防线：
  1) 白名单 —— 第一关键字必须在 ALLOWED_FIRST_KEYWORDS 内
  2) 黑名单 —— 整段 SQL 里禁止出现 FORBIDDEN_TOKENS（不区分大小写、多词也能抓）

这是运行前静态校验，不依赖 Oracle 权限错误再报 —— 因为 Oracle 权限错误在 DBA 那边
排障成本高；我们在应用层先挡掉，对用户返回清晰的 "SQL X 被禁止：Y"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

ALLOWED_FIRST_KEYWORDS = {
    "SELECT",
    "WITH",           # CTE
    "EXPLAIN",        # EXPLAIN PLAN
    "ALTER",          # ALTER SESSION ... （黑名单会继续筛掉 ALTER SYSTEM / ALTER USER ...）
    "DECLARE",        # PL/SQL BEGIN ... END 里只允许 SELECT INTO + OPEN ... FOR + FETCH
}

# 不区分大小写的正则；\b 边界避免误伤（比如 "INSERT" 在 "INSERTED" 里）。
FORBIDDEN_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(DROP|CREATE|TRUNCATE|RENAME)\b", re.I), "DDL 操作被禁止"),
    (re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE)\b", re.I), "DML 写操作被禁止"),
    (re.compile(r"\bALTER\s+(SYSTEM|USER|TABLE|INDEX|VIEW)\b", re.I), "ALTER SYSTEM/TABLE/INDEX/VIEW/USER 被禁止"),
    (re.compile(r"\b(CONNECT|GRANT|REVOKE|AUDIT|NOAUDIT)\b", re.I), "权限/审计操作被禁止"),
    (re.compile(r"\bEXEC(UTE)?\s+", re.I), "动态 EXECUTE 被禁止（仅允许 DBMS_XPLAN/DBMS_MONITOR）"),
    (re.compile(r"DBMS_SQL|DBMS_OUTPUT\.PUT", re.I), "DBMS_SQL / DBMS_OUTPUT.PUT* 被禁止（动态拼 SQL）"),
    (re.compile(r"\b(SHUTDOWN|STARTUP|OPEN|CLOSE)\b", re.I), "启动/关闭操作被禁止"),
    (re.compile(r"\bCOMMIT\b|\bROLLBACK\b", re.I), "COMMIT/ROLLBACK 被禁止（沙箱 autocommit=False）"),
    # 多语句风险 —— 禁止同一字符串里出现多个顶层 SQL（分号 + 关键字分隔）
    (re.compile(r";\s*\n?\s*(SELECT|WITH|ALTER|INSERT|UPDATE|DELETE|CREATE|DROP)\s", re.I), "检测到多条 SQL（分号分隔）"),
]


@dataclass
class ValidationResult:
    ok: bool
    reason: Optional[str] = None


def validate_sql(sql: str) -> ValidationResult:
    """静态安全校验。

    Returns ``ValidationResult(ok=True)`` if the SQL is safe to run in sandbox,
    ``ValidationResult(ok=False, reason=...)`` otherwise.
    """
    if not sql or not sql.strip():
        return ValidationResult(ok=False, reason="SQL 为空")

    # Strip SQL comments (line -- and block /* */) before keyword detection.
    cleaned = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    cleaned = re.sub(r"--[^\n]*", " ", cleaned)
    cleaned = cleaned.strip()

    if not cleaned:
        return ValidationResult(ok=False, reason="SQL 只有注释")

    # Extract first keyword.
    first_word_match = re.match(r"([A-Za-z]+)", cleaned)
    if not first_word_match:
        return ValidationResult(ok=False, reason="无法识别 SQL 类型")
    first_kw = first_word_match.group(1).upper()

    if first_kw not in ALLOWED_FIRST_KEYWORDS:
        return ValidationResult(ok=False, reason=f"第一关键字 {first_kw} 不在白名单内")

    # Blocklist scan — short-circuit on first hit.
    for pat, reason in FORBIDDEN_PATTERNS:
        if pat.search(cleaned):
            # Special-case: ALTER SESSION SET current_schema = xxx 是安全的
            if "ALTER" in first_kw and "ALTER SESSION" in cleaned.upper():
                continue
            return ValidationResult(ok=False, reason=reason)

    # EXECUTE 白名单 —— 仅允许 DBMS_XPLAN.DISPLAY_CURSOR / DBMS_MONITOR.SESSION_TRACE_DISABLE
    exec_match = re.search(r"\bEXEC(UTE)?\b", cleaned, re.I)
    if exec_match:
        if not re.search(r"DBMS_(XPLAN|MONITOR)\.", cleaned, re.I):
            return ValidationResult(ok=False, reason="EXECUTE 仅允许 DBMS_XPLAN / DBMS_MONITOR")

    return ValidationResult(ok=True)
