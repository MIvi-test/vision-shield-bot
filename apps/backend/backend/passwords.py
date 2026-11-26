"""Salted password hashes; no default or recoverable passwords."""

import hashlib
import hmac
import secrets


def password_hash(password: str, salt: str | None = None) -> str:
    if not 1 <= len(password) <= 256:
        raise ValueError("Пароль должен содержать 1–256 символов")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1
    ).hex()
    return f"scrypt16384${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, digest = encoded.split("$")
        if algorithm != "scrypt16384" or len(salt) != 32 or len(digest) != 128:
            return False
        return hmac.compare_digest(password_hash(password, salt), encoded)
    except ValueError, TypeError:
        return False


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
