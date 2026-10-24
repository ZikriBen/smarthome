"""HMAC-SHA256 signed stream URLs (per variant, static, rotate = rewrite .strm)."""

import hashlib
import hmac
import os
import re
from pathlib import Path

SIG_RE = re.compile(r"^[0-9a-f]{32}$")
MIN_KEY_BYTES = 32


class HmacKeyError(Exception):
    pass


def load_key(path: str | None = None) -> bytes | None:
    """Read the key from TG_STREAM_HMAC_KEY_FILE. Never logged or returned to clients."""
    path = path if path is not None else os.getenv("TG_STREAM_HMAC_KEY_FILE", "")
    if not path:
        return None
    try:
        data = Path(path).read_bytes().strip()
    except OSError:
        return None
    if len(data) < MIN_KEY_BYTES:
        raise HmacKeyError("TG_STREAM_HMAC_KEY_FILE is too short (need >= 32 bytes)")
    return data


def sign(key: bytes, variant_id: int) -> str:
    mac = hmac.new(key, f"v1|{int(variant_id)}".encode(), hashlib.sha256)
    return mac.hexdigest()[:32]  # 128 bits


def verify(key: bytes | None, variant_id: int, sig: str) -> bool:
    if not key or not isinstance(sig, str) or not SIG_RE.match(sig):
        return False
    return hmac.compare_digest(sign(key, variant_id), sig)


def redact_path(path: str) -> str:
    """Strip the signature from /s/<id>/<sig> for logs."""
    return re.sub(r"^(/s/\d+/)[^/?#\s]+", r"\1<sig>", path)
