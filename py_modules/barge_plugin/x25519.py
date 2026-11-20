"""X25519 (RFC 7748) Diffie-Hellman over Curve25519 in pure Python.

`barged` pairs over an ephemeral X25519 exchange (`crates/barge-machine/src/pair.rs`).
The plugin keeps its runtime dependency-free, so this is the small RFC 7748
subset it needs: scalar multiplication on the Montgomery ladder and the base
point. It follows the RFC pseudocode, including clamping the scalar, masking
the top bit of the peer's u-coordinate and rejecting an all-zero shared secret.

Python integers are not constant time. The secret is an ephemeral pairing
scalar held only for the two-minute pairing window, and the caller compares only
its own freshly generated secret against a peer public key, so the exposure the
ladder's data-dependent path could leak is a short-lived key that dies with the
request. The daemon side (the long-lived surface) is the one that needs
constant-time arithmetic, and it is Rust.
"""

from __future__ import annotations

from .errors import BargeError

FIELD_PRIME = 2**255 - 19
_BASE_POINT = (9).to_bytes(32, "little")
_A24 = 121665
_POINT_BITS = 255


def _decode_scalar(secret: bytes) -> int:
    if len(secret) != 32:
        raise BargeError("usage", "X25519 secret must be 32 bytes")
    clamped = bytearray(secret)
    clamped[0] &= 248
    clamped[31] &= 127
    clamped[31] |= 64
    return int.from_bytes(bytes(clamped), "little")


def _decode_u(coordinate: bytes) -> int:
    if len(coordinate) != 32:
        raise BargeError("usage", "X25519 public key must be 32 bytes")
    # RFC 7748: mask the most significant bit before decoding the u-coordinate.
    value = int.from_bytes(coordinate, "little") & ((1 << _POINT_BITS) - 1)
    return value % FIELD_PRIME


def x25519(secret: bytes, peer_public: bytes) -> bytes:
    """Return the 32-byte X25519 output for `secret` and the peer's public key.

    The result may be all zero when the peer supplied a low-order point; callers
    must reject that (see `shared_secret`).
    """
    scalar = _decode_scalar(secret)
    x1 = _decode_u(peer_public)

    x2, z2 = 1, 0
    x3, z3 = x1, 1
    swap = 0
    for bit in range(_POINT_BITS - 1, -1, -1):
        k_t = (scalar >> bit) & 1
        swap ^= k_t
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = k_t

        a = (x2 + z2) % FIELD_PRIME
        aa = a * a % FIELD_PRIME
        b = (x2 - z2) % FIELD_PRIME
        bb = b * b % FIELD_PRIME
        e = (aa - bb) % FIELD_PRIME
        c = (x3 + z3) % FIELD_PRIME
        d = (x3 - z3) % FIELD_PRIME
        da = d * a % FIELD_PRIME
        cb = c * b % FIELD_PRIME
        x3 = (da + cb) ** 2 % FIELD_PRIME
        z3 = x1 * (da - cb) ** 2 % FIELD_PRIME
        x2 = aa * bb % FIELD_PRIME
        z2 = e * (aa + _A24 * e) % FIELD_PRIME

    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, FIELD_PRIME - 2, FIELD_PRIME) % FIELD_PRIME).to_bytes(32, "little")


def public_key(secret: bytes) -> bytes:
    """Derive the X25519 public key for a 32-byte secret scalar."""
    return x25519(secret, _BASE_POINT)


def shared_secret(secret: bytes, peer_public: bytes) -> bytes:
    """Return the shared secret, refusing a non-contributory all-zero output."""
    shared = x25519(secret, peer_public)
    if shared == b"\x00" * 32:
        raise BargeError("refused", "peer supplied an invalid X25519 public key")
    return shared
