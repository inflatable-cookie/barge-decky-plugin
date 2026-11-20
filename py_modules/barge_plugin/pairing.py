"""Ephemeral X25519 + HKDF-SHA256 pairing key exchange and six-digit code.

Port of `crates/barge-machine/src/pair.rs`. Both sides generate an ephemeral
X25519 keypair, derive the 256-bit client HMAC key with HKDF-SHA256 over
`salt = client_public || daemon_public` and `info = "barge-pair-v1"`, and derive
the six-digit verification code from
`SHA-256("barge-pair-code-v1\\0" || client_public || daemon_public)`.

The derived key and the local secret are never rendered by `repr` or logged.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Literal

from . import auth, x25519
from .errors import BargeError

HKDF_INFO = b"barge-pair-v1"
CODE_PREFIX = b"barge-pair-code-v1\0"
KEY_BYTES_LEN = 32
PUBLIC_KEY_HEX_LEN = 64

PairRole = Literal["client", "daemon"]


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = KEY_BYTES_LEN) -> bytes:
    """HKDF-SHA256 (RFC 5869) extract and expand, returning `length` bytes."""
    if length < 1 or length > 255 * hashlib.sha256().digest_size:
        raise BargeError("usage", "HKDF output length is out of range")
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    output = b""
    block = b""
    counter = 1
    while len(output) < length:
        block = hmac.new(prk, block + info + bytes((counter,)), hashlib.sha256).digest()
        output += block
        counter += 1
    return output[:length]


def verification_code(client_public: bytes, daemon_public: bytes) -> str:
    """Six-digit verification code shown on both sides of a pairing request."""
    digest = hashlib.sha256(CODE_PREFIX + client_public + daemon_public).digest()
    value = int.from_bytes(digest[0:4], "big") % 1_000_000
    return f"{value:06d}"


def parse_public_key(value: str) -> bytes:
    """Decode a 64-character lowercase hex X25519 public key."""
    trimmed = value.strip()
    decoded = auth.decode_lowercase_hex(trimmed, KEY_BYTES_LEN)
    if decoded is None:
        raise BargeError("usage", "public_key must be 64 lowercase hexadecimal characters")
    return decoded


def format_public_key(value: bytes) -> str:
    """Encode a 32-byte X25519 public key as 64 lowercase hex characters."""
    if len(value) != KEY_BYTES_LEN:
        raise BargeError("usage", "X25519 public key must be 32 bytes")
    return value.hex()


@dataclass(frozen=True)
class DerivedPairing:
    """The pairing output both sides must agree on."""

    key: bytes
    code: str

    def __repr__(self) -> str:
        return f"DerivedPairing(code={self.code!r})"


class PairingKeyPair:
    """One ephemeral X25519 keypair used for a single pairing request."""

    def __init__(self, secret: bytes) -> None:
        if len(secret) != 32:
            raise BargeError("usage", "X25519 secret must be 32 bytes")
        self._secret = secret
        self._public = x25519.public_key(secret)

    @classmethod
    def generate(cls) -> PairingKeyPair:
        return cls(secrets.token_bytes(32))

    @classmethod
    def from_secret_hex(cls, secret_hex: str) -> PairingKeyPair:
        """Build a keypair from a 64-character lowercase hex scalar (tests, vectors)."""
        decoded = auth.decode_lowercase_hex(secret_hex.strip(), 32)
        if decoded is None:
            raise BargeError("usage", "X25519 secret must be 64 lowercase hexadecimal characters")
        return cls(decoded)

    def __repr__(self) -> str:
        return f"PairingKeyPair(public={self.public_key_hex()!r})"

    @property
    def public_key(self) -> bytes:
        return self._public

    def public_key_hex(self) -> str:
        return format_public_key(self._public)

    def derive(self, peer_public: bytes, role: PairRole) -> DerivedPairing:
        """Derive the client HMAC key and code, placing the public keys by role."""
        if role not in ("client", "daemon"):
            raise BargeError("usage", f"unknown pairing role {role!r}")
        shared = x25519.shared_secret(self._secret, peer_public)
        if role == "client":
            client_public, daemon_public = self._public, peer_public
        else:
            client_public, daemon_public = peer_public, self._public

        key = hkdf_sha256(shared, client_public + daemon_public, HKDF_INFO)
        return DerivedPairing(key=key, code=verification_code(client_public, daemon_public))
