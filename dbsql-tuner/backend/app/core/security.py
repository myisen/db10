"""Lightweight encryption helper for Oracle passwords stored at rest.

Uses Fernet (symmetric AES-128-CBC + HMAC-SHA256).
Replaces the plaintext stored in ``oracle_instances.read_password``.
"""
from __future__ import annotations

from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


def _get_fernet() -> Fernet:
    key = get_settings().secret_key.encode("utf-8")
    return Fernet(key)


def encrypt_password(plain: str) -> str:
    """Encrypt an Oracle password for storage. Returns url-safe base64."""
    return _get_fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_password(token: str) -> Optional[str]:
    """Decrypt a stored Oracle password. Returns None on corruption."""
    try:
        return _get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None
