"""Salted password hashes; no default or recoverable passwords."""

import hashlib
import hmac
import secrets


def password_hash(password: str, salt: str | None = None) -> str:
    if not 1 <= len(password) <= 256:
        raise ValueError("Пароль должен содержать 1–256 символов")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=5
    ).hex()
    return f"scrypt16384p5${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, digest = encoded.split("$")
        if (
            algorithm not in {"scrypt16384p5", "scrypt16384"}
            or len(salt) != 32
            or len(digest) != 128
        ):
            return False
        if not 1 <= len(password) <= 256:
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            n=16384,
            r=8,
            p=5 if algorithm == "scrypt16384p5" else 1,
        ).hex()
        return hmac.compare_digest(actual, digest)
    except ValueError, TypeError:
        return False


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
