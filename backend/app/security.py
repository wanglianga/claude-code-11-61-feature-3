"""Password hashing and signed bearer tokens (stdlib only, demo-appropriate)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from .config import settings

_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS)
    return f"pbkdf2${_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    try:
        _, iterations, salt, expected = hashed.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def create_token(payload: dict[str, Any]) -> str:
    body = dict(payload)
    body["exp"] = int(time.time()) + settings.token_ttl_hours * 3600
    encoded = _b64(json.dumps(body, separators=(",", ":")).encode())
    signature = _b64(hmac.new(settings.secret_key.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def decode_token(token: str) -> dict[str, Any] | None:
    try:
        encoded, signature = token.split(".")
        expected = _b64(hmac.new(settings.secret_key.encode(), encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_unb64(encoded))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None
