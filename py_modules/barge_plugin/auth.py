"""Shared LAN request authentication and owner-only key storage.

Matches `crates/barge-machine/src/auth.rs` byte-for-byte.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path
from typing import Optional

from .errors import BargeError
from .fs_atomic import atomic_write_owner_only

TIMESTAMP_HEADER = "x-barge-timestamp"
NONCE_HEADER = "x-barge-nonce"
SIGNATURE_HEADER = "x-barge-signature"
CLIENT_ID_HEADER = "x-barge-client-id"

KEY_BYTES_LEN = 32
NONCE_BYTES_LEN = 16
SIGNATURE_BYTES_LEN = 32

_LOWERCASE_HEX = frozenset("0123456789abcdef")


def decode_lowercase_hex(value: str, byte_length: int) -> Optional[bytes]:
    """Decode fixed-length lowercase hexadecimal text, returning None if invalid."""
    if len(value) != byte_length * 2:
        return None
    if not all(ch in _LOWERCASE_HEX for ch in value):
        return None
    return bytes.fromhex(value)


def decode_key(key_hex: str) -> bytes:
    """Validate and decode a 64-character lowercase hex LAN key."""
    trimmed = key_hex.strip()
    decoded = decode_lowercase_hex(trimmed, KEY_BYTES_LEN)
    if decoded is None:
        raise BargeError(
            "usage",
            "LAN key must be 64 lowercase hexadecimal characters",
        )
    return decoded


def generate_nonce() -> str:
    """Generate a 16-byte random nonce encoded as 32 lowercase hex characters."""
    return secrets.token_hex(NONCE_BYTES_LEN)


def _signing_payload(
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return f"{method}\n{path}\n{timestamp}\n{nonce}\n{digest}".encode("utf-8")


def sign_request(
    key: bytes,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> str:
    """Compute the lowercase hex HMAC-SHA256 signature for a barged request."""
    if not key:
        raise BargeError("usage", "LAN key must not be empty")
    payload = _signing_payload(method, path, timestamp, nonce, body)
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_request(
    key: bytes,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
    signature: str,
) -> bool:
    """Verify a request MAC in constant time, rejecting malformed hex."""
    provided = decode_lowercase_hex(signature, SIGNATURE_BYTES_LEN)
    if provided is None or not key:
        return False
    expected = bytes.fromhex(sign_request(key, method, path, timestamp, nonce, body))
    return hmac.compare_digest(expected, provided)


def write_key_owner_only(path: Path, key_hex: str) -> None:
    """Atomically write a validated LAN key with 0o600 permissions."""
    trimmed = key_hex.strip()
    decode_key(trimmed)
    atomic_write_owner_only(
        path,
        trimmed.encode("utf-8"),
        temp_prefix=".barge-key-",
        label="LAN key",
    )


def read_key(path: Path) -> Optional[bytes]:
    """Read and validate the stored LAN key, returning None when unpaired."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BargeError("io", f"cannot read LAN key: {exc}") from exc

    decoded = decode_lowercase_hex(raw.strip(), KEY_BYTES_LEN)
    if decoded is None:
        raise BargeError("unknown_state", "stored LAN key is invalid")
    return decoded


def remove_key(path: Path) -> bool:
    """Remove a stored LAN key file; returns True if a key existed."""
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise BargeError("io", f"cannot remove LAN key: {exc}") from exc
