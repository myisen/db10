"""SQL 归一化：canonical + literal + stmt_type + 指纹。

核心规则（来自设计文档）：
  1. canonical_sql：绑定变量统一为 ? 占位的标准 SQL
     - Oracle：优先 DBMS_SQL.TO_CANONICAL；失败退化为自写正则
     - OceanBase：审计表里的 sql_text 本身就是归一化版本
  2. literal_sql：把 ? 替换为绑定变量真实值
     - 字符串 → 'xxx'（内部单引号转义为 ''）
     - 数字   → 直接写
     - 日期   → TO_DATE('2024-01-01','YYYY-MM-DD')
     - 拿不到绑定值 → literal_sql=None, literal_available=False
  3. stmt_type：SQL 类型，用 COMMAND_TYPE 数值映射（Oracle 标准码）
  4. fingerprint = MD5(canonical_sql.lower())
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any


# Oracle command_type 数值 → 语义
_COMMAND_TYPE_MAP = {
    1: "CREATE TABLE", 2: "INSERT", 3: "SELECT", 4: "CREATE CLUSTER",
    5: "ALTER TABLE", 6: "UPDATE", 7: "DELETE", 8: "DROP TABLE",
    9: "CREATE INDEX", 10: "DROP INDEX", 11: "ALTER INDEX",
    12: "DROP CLUSTER", 13: "CREATE SEQUENCE", 14: "ALTER SEQUENCE",
    15: "DROP SEQUENCE", 16: "DROP INDEX", 17: "GRANT",
    18: "REVOKE", 19: "CREATE SYNONYM", 20: "DROP SYNONYM",
    21: "CREATE VIEW", 22: "DROP VIEW", 23: "VALIDATE INDEX",
    24: "CREATE PROCEDURE", 25: "ALTER PROCEDURE", 26: "DROP PROCEDURE",
    27: "CREATE TRIGGER", 28: "ALTER TRIGGER", 29: "DROP TRIGGER",
    30: "CREATE COMMENT", 31: "COMMENT ON TABLE", 32: "COMMENT ON COLUMN",
    33: "CREATE PROFILE", 34: "ALTER PROFILE", 35: "DROP PROFILE",
    36: "CREATE USER", 37: "ALTER USER", 38: "DROP USER",
    39: "CREATE ROLE", 40: "ALTER ROLE", 41: "DROP ROLE",
    42: "SET ROLE", 43: "CREATE ROLLBACK SEGMENT",
    44: "ALTER ROLLBACK SEGMENT", 45: "DROP ROLLBACK SEGMENT",
    46: "ALTER SYSTEM", 47: "SET TRANSACTION", 48: "COMMIT",
    49: "ROLLBACK", 50: "SAVEPOINT", 51: "CREATE CONTROLFILE",
    52: "ALTER DATABASE", 53: "CREATE DATABASE",
    54: "CREATE DATAFILE", 55: "ALTER DATAFILE", 56: "DROP DATAFILE",
    57: "CREATE TABLESPACE", 58: "ALTER TABLESPACE", 59: "DROP TABLESPACE",
    60: "ALTER SESSION", 61: "ALTER RESOURCE COST",
    62: "CREATE MATERIALIZED VIEW LOG", 63: "DROP MATERIALIZED VIEW LOG",
    64: "CREATE MATERIALIZED VIEW", 65: "ALTER MATERIALIZED VIEW",
    66: "DROP MATERIALIZED VIEW", 67: "CREATE TYPE",
    68: "DROP TYPE", 69: "ALTER TYPE", 70: "CREATE TYPE BODY",
    71: "DROP TYPE BODY", 72: "ALTER TYPE BODY", 73: "CREATE LIBRARY",
    74: "DROP LIBRARY", 75: "CREATE JAVA", 76: "DROP JAVA",
    77: "UPDATE INDEXES", 78: "UPDATE TABLES", 79: "UPDATE TRIGGERS",
    80: "UPDATE CONSTRAINT", 81: "CALL", 82: "MERGE",
    83: "DEFINE", 84: "PREPARE", 85: "FETCH", 86: "EXECUTE",
    87: "SET DESCRIPTOR", 88: "SET TRANSACTION READ ONLY",
    89: "CREATE FUNCTION", 90: "ALTER FUNCTION", 91: "DROP FUNCTION",
    92: "CREATE PACKAGE", 93: "ALTER PACKAGE", 94: "DROP PACKAGE",
    95: "CREATE PACKAGE BODY", 96: "ALTER PACKAGE BODY",
    97: "DROP PACKAGE BODY", 98: "CREATE TYPE", 99: "CREATE RESOURCE COST",
    100: "EXPLAIN", 101: "CREATE SCHEMA", 102: "CREATE TEMPORARY TABLE",
    103: "ALTER SESSION SET NLS", 104: "ALTER TABLE ANY",
    105: "DROP TABLE ANY", 106: "ALTER TRIGGER ANY",
    107: "DROP TRIGGER ANY", 108: "ALTER PROCEDURE ANY",
    109: "DROP PROCEDURE ANY", 110: "ALTER SEQUENCE ANY",
    111: "DROP SEQUENCE ANY", 112: "ALTER INDEX ANY",
    113: "DROP INDEX ANY", 114: "ALTER PACKAGE ANY",
    115: "DROP PACKAGE ANY", 116: "ALTER FUNCTION ANY",
    117: "DROP FUNCTION ANY", 118: "ALTER TYPE ANY",
    119: "DROP TYPE ANY", 120: "ALTER LIBRARY ANY",
    121: "DROP LIBRARY ANY", 122: "CREATE TRIGGER ANY",
    123: "CREATE PROCEDURE ANY", 124: "CREATE FUNCTION ANY",
    125: "CREATE PACKAGE ANY", 126: "CREATE PACKAGE BODY ANY",
    127: "CREATE TRIGGER WITH DEBUG", 128: "ALTER TRIGGER WITH DEBUG",
    129: "DROP TRIGGER ANY CASCADE", 130: "UPDATE TRIGGER ANY CASCADE",
    131: "UPDATE PROCEDURE ANY CASCADE", 132: "UPDATE FUNCTION ANY CASCADE",
    133: "UPDATE PACKAGE ANY CASCADE", 134: "UPDATE PACKAGE BODY ANY CASCADE",
    135: "UPDATE TYPE ANY CASCADE", 136: "UPDATE LIBRARY ANY CASCADE",
    137: "ALTER SYSTEM KILL SESSION", 138: "ALTER SYSTEM DISCONNECT SESSION",
    139: "ALTER SYSTEM UNQUIESCE", 140: "ALTER SYSTEM QUIESCE RESTRICTED",
    141: "ALTER SYSTEM SUSPEND", 142: "ALTER SYSTEM RESUME",
    143: "ALTER SYSTEM CHECKPOINT", 144: "ALTER SYSTEM SET",
    145: "ALTER SYSTEM REGISTER", 146: "ALTER SYSTEM RESTART",
    147: "ALTER SYSTEM FLUSH", 148: "ALTER SYSTEM ARCHIVE LOG",
    149: "ALTER SYSTEM SWITCH LOGFILE", 150: "ALTER SYSTEM ENABLE RESTRICTED SESSION",
    151: "ALTER SYSTEM DISABLE RESTRICTED SESSION",
    152: "ALTER SYSTEM RECOVER", 153: "ALTER SYSTEM SET USE_STORED_OUTLINES",
    154: "ALTER SYSTEM CLEAR SHARED_POOL", 155: "ALTER SYSTEM CLEAR BUFFER CACHE",
    156: "ALTER SYSTEM CLEAR FLASH LOG", 157: "ALTER SYSTEM CLEAR ALL",
    158: "ALTER SYSTEM CLEAR", 159: "ALTER SYSTEM ALTER DATABASE",
    160: "ALTER SYSTEM ALTER SESSION", 161: "ALTER SYSTEM STARTUP",
    162: "ALTER SYSTEM SHUTDOWN", 163: "ALTER SYSTEM MONITOR",
    164: "ALTER SYSTEM DEBUG", 165: "ALTER SYSTEM INSTANCE",
    166: "ALTER SYSTEM RECOVER MANAGED STANDBY DATABASE",
    167: "ALTER SYSTEM RECOVER MANAGED STANDBY DATABASE CANCEL",
    168: "ALTER SYSTEM RECOVER MANAGED STANDBY DATABASE DISCONNECT",
    169: "ALTER SYSTEM RECOVER MANAGED STANDBY DATABASE FINISH",
    170: "ALTER SYSTEM RECOVER MANAGED STANDBY DATABASE IMMEDIATE",
    171: "ALTER SYSTEM RECOVER MANAGED STANDBY DATABASE MANAGED",
    172: "ALTER SYSTEM RECOVER MANAGED STANDBY DATABASE NOPARALLEL",
    173: "ALTER SYSTEM RECOVER MANAGED STANDBY DATABASE PARALLEL",
    174: "ALTER SYSTEM RECOVER MANAGED STANDBY DATABASE THROUGH ALL LOGFILES",
    175: "ALTER SYSTEM RECOVER MANAGED STANDBY DATABASE UNTIL",
    176: "ALTER SYSTEM RECOVER MANAGED STANDBY DATABASE",
    177: "ALTER DATABASE RECOVER", 178: "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE",
    179: "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE CANCEL",
    180: "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE DISCONNECT",
    181: "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE FINISH",
    182: "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE IMMEDIATE",
    183: "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE MANAGED",
    184: "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE NOPARALLEL",
    185: "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE PARALLEL",
    186: "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE THROUGH ALL LOGFILES",
    187: "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE UNTIL",
    188: "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE",
}


def _normalize_stmt_type(cmd: str) -> str:
    """把 command_type 归一到 MVP 的粗粒度类型。"""
    up = cmd.upper().strip()
    if up.startswith("SELECT"):
        return "SELECT"
    if up.startswith("INSERT"):
        return "INSERT"
    if up.startswith("UPDATE"):
        return "UPDATE"
    if up.startswith("DELETE") or up.startswith("MERGE"):
        return "DELETE"
    if up in ("CALL", "EXECUTE") or up.startswith(("CREATE PROCEDURE", "CREATE FUNCTION",
                "CREATE PACKAGE", "ALTER PROCEDURE", "ALTER FUNCTION",
                "ALTER PACKAGE", "CREATE TRIGGER", "ALTER TRIGGER")):
        return "PLSQL"
    if up.startswith(("CREATE ", "DROP ", "ALTER ", "GRANT", "REVOKE",
                      "TRUNCATE", "COMMENT ", "EXPLAIN", "SET ",
                      "COMMIT", "ROLLBACK", "SAVEPOINT")):
        return "DDL"
    return "UNKNOWN"


def command_type_to_stmt_type(command_type_code: int | str | None) -> str:
    """Oracle command_type 数值 / 原始关键字 → MVP 粗粒度类型。"""
    if command_type_code is None:
        return "UNKNOWN"
    if isinstance(command_type_code, int):
        name = _COMMAND_TYPE_MAP.get(command_type_code, "")
        return _normalize_stmt_type(name) if name else "UNKNOWN"
    return _normalize_stmt_type(str(command_type_code))


# 用关键字推断 stmt_type（没有 command_type 时的兜底）
_STMT_KEYWORDS = [
    ("SELECT", re.compile(r"^\s*SELECT\b", re.I)),
    ("INSERT", re.compile(r"^\s*INSERT\b", re.I)),
    ("UPDATE", re.compile(r"^\s*UPDATE\b", re.I)),
    ("DELETE", re.compile(r"^\s*(DELETE|MERGE)\b", re.I)),
    ("CALL", re.compile(r"^\s*CALL\b", re.I)),
    ("DDL", re.compile(r"^\s*(CREATE|DROP|ALTER|GRANT|REVOKE|TRUNCATE|COMMENT)\b", re.I)),
]


def infer_stmt_type(sql: str) -> str:
    for t, pat in _STMT_KEYWORDS:
        if pat.search(sql):
            if t == "CALL":
                return "PLSQL"
            if t in ("DDL", "CREATE", "DROP", "ALTER", "GRANT", "REVOKE"):
                return "DDL"
            return t
    return "UNKNOWN"


# --- 自写 canonical 兜底（没有 DBMS_SQL.TO_CANONICAL 时用） ---
# 处理 Oracle 风格 ':1'、':name'、以及 OB 风格 '?' 占位
_ORA_POSITIONAL = re.compile(r":(\d+)")          # :1 :2 ...
_ORA_NAMED = re.compile(r":[A-Za-z_][A-Za-z0-9_]*")  # :name
# 把字符串字面量里的 :xxx 也替换了？不行，会误伤，所以先把字符串占位掉
_STRING_LITERAL = re.compile(r"'([^']|'')*'")


def canonicalize_simple(sql: str) -> str:
    """朴素归一化：占位字符串/注释后替换绑定变量为 ?。"""
    if not sql:
        return ""
    s = sql

    # 先把字符串字面量替换成占位符，避免误匹配
    placeholders: list[str] = []

    def _save_string(m: re.Match) -> str:
        placeholders.append(m.group(0))
        return f"\x00{len(placeholders) - 1}\x00"

    s = _STRING_LITERAL.sub(_save_string, s)

    # 注释去掉
    s = re.sub(r"--[^\n]*", "", s)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)

    # 替换绑定变量
    s = _ORA_POSITIONAL.sub("?", s)
    s = _ORA_NAMED.sub("?", s)

    # 还原字符串
    for i, lit in enumerate(placeholders):
        s = s.replace(f"\x00{i}\x00", lit)

    # 连续 ? 不处理（IN (:1,:2,:3) 就是 IN (?,?,?)，不做 IN 归一化）
    return s.strip()


# --- literal 渲染 ---
def _quote(val: Any, dtype: str = "str") -> str:
    """把绑定值渲染成字面量 SQL。"""
    if val is None:
        return "NULL"
    if isinstance(val, (int, float)):
        return str(val)
    s = str(val)
    if dtype in ("date", "timestamp"):
        # 尝试识别 ISO 格式，否则直接用 TO_DATE
        s_clean = s.strip().strip("T").strip("Z")
        return f"TO_DATE('{s_clean[:19]}','YYYY-MM-DD HH24:MI:SS')"
    # 默认字符串：内部单引号转义为 ''
    escaped = s.replace("'", "''")
    return f"'{escaped}'"


def render_literal(canonical_sql: str, binds: list[dict[str, Any]]) -> str | None:
    """把 canonical_sql 中的 ? 替换为 binds 列表中的值。

    binds: [{"name": "x", "value": "1", "type": "str"}, ...]
           支持 dict 或 list-of-values 两种格式。
    """
    if not binds:
        return None

    # 支持两种输入
    if isinstance(binds[0], dict):
        values = [b.get("value") for b in binds]
        types = [b.get("type", "str") for b in binds]
    else:
        values = list(binds)
        types = ["str"] * len(binds)

    result = []
    bind_idx = 0
    i = 0
    while i < len(canonical_sql):
        ch = canonical_sql[i]
        # 字符串字面量原样保留
        if ch == "'":
            j = i + 1
            result.append(ch)
            while j < len(canonical_sql):
                cj = canonical_sql[j]
                result.append(cj)
                if cj == "'":
                    if j + 1 < len(canonical_sql) and canonical_sql[j + 1] == "'":
                        result.append("'")
                        j += 2
                        continue
                    i = j
                    break
                j += 1
            i += 1
            continue
        # ? 占位 → 替换
        if ch == "?":
            if bind_idx >= len(values):
                result.append("?")
            else:
                result.append(_quote(values[bind_idx], types[bind_idx]))
                bind_idx += 1
            i += 1
            continue
        result.append(ch)
        i += 1

    if bind_idx == 0:
        return None  # 没消耗任何 bind，说明 ? 没匹配上
    return "".join(result)


def fingerprint(canonical_sql: str) -> str:
    return hashlib.md5(canonical_sql.lower().encode("utf-8")).hexdigest()


@dataclass
class NormalizedSQL:
    canonical_sql: str
    literal_sql: str | None
    literal_available: bool
    stmt_type: str
    fp: str


def normalize(
    raw_sql: str,
    binds: list[dict[str, Any]] | None = None,
    command_type: int | str | None = None,
    canonical_from_db: str | None = None,
) -> NormalizedSQL:
    """一站式归一化。"""
    canonical = canonical_from_db or canonicalize_simple(raw_sql or "")
    stmt_type = (
        command_type_to_stmt_type(command_type)
        if command_type is not None
        else infer_stmt_type(raw_sql or "")
    )
    literal = render_literal(canonical, binds) if binds else None
    return NormalizedSQL(
        canonical_sql=canonical,
        literal_sql=literal,
        literal_available=literal is not None,
        stmt_type=stmt_type,
        fp=fingerprint(canonical),
    )
