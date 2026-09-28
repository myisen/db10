"""适配器工厂。"""
from __future__ import annotations

from .oracle_adapter import BaseAdapter
from .oracle_adapter import OracleAdapter
from .oceanbase_adapter import OceanBaseAdapter


def make_adapter(db_type: str, conn_url: str, username: str, password_enc: str,
                 extra_conf: dict | None = None) -> BaseAdapter:
    if db_type.lower() == "oracle":
        return OracleAdapter(conn_url, username, password_enc, extra_conf)
    if db_type.lower() == "oceanbase":
        return OceanBaseAdapter(conn_url, username, password_enc, extra_conf)
    raise ValueError(f"Unsupported db_type: {db_type}")
