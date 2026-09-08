"""Хеширование паролей пользователей; открытые пароли не сохраняются."""

import hashlib
import hmac
import secrets


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=131072,
        r=8,
        p=1,
        maxmem=256 * 1024 * 1024,
        dklen=32,
    )


def hash_password(password: str) -> str:
    if not 15 <= len(password) <= 128:
        raise ValueError("Пароль должен содержать от 15 до 128 символов")
    salt = secrets.token_bytes(16)
    digest = _derive(password, salt)
    return f"scrypt$131072$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or not 1 <= len(password) <= 128:
        return False
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if (algorithm, n, r, p) != ("scrypt", "131072", "8", "1"):
            return False
        salt, digest = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
        if len(salt) != 16 or len(digest) != 32:
            return False
        return hmac.compare_digest(_derive(password, salt), digest)
    except (ValueError, TypeError, AttributeError):
        return False
