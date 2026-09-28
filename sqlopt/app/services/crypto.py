"""Fernet 对称加密工具。用于加密目标实例密码。"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings


def _get_fernet() -> Fernet:
    key = get_settings().fernet_key
    # 兼容用户写了裸 key（去掉引号）
    if key == "CHANGE_ME_IN_PROD":
        # 开发模式下生成一次临时 key 存在内存（重启会变，仅用于本地验证）
        import base64, os

        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    try:
        return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        # 开发模式下如果 key 变了，返回空串，让上层报错
        return ""


def generate_key() -> str:
    """CLI 工具：生成新的 Fernet key。"""
    return Fernet.generate_key().decode()
