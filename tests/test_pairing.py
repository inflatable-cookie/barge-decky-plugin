"""Pairing crypto vectors: X25519, HKDF-SHA256 and the six-digit code.

The fixed key, code and public keys are the ones Barge commits in
`docs/knowledge/machines.md` (and `crates/barge-machine/src/pair.rs`) so every
client is checked against the same exchange.
"""

from __future__ import annotations

import hashlib

import pytest

from barge_plugin import pairing, x25519
from barge_plugin.errors import BargeError

CLIENT_SECRET = "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a"
CLIENT_PUBLIC = "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
DAEMON_SECRET = "5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb"
DAEMON_PUBLIC = "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f"
DERIVED_KEY = "18d60e9b82dc130c6972596a7b47f3c1381520faa8704ded8429c85231323ea8"
CODE = "340092"


def test_x25519_matches_rfc7748_vectors() -> None:
    cases = [
        (
            "a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4",
            "e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c",
            "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552",
        ),
        (
            "4b66e9d4d1b4673c5ad22691957d6af5c11b6421e0ea01d42ca4169e7918ba0d",
            "e5210f12786811d3f4b7959d0538ae2c31dbe7106fc03c3efc4cd549c715a493",
            "95cbde9476e8907d7aade45cb4b873f88b595a68799fa152e6f8f7647aac7957",
        ),
    ]
    for secret, peer, expected in cases:
        assert x25519.x25519(bytes.fromhex(secret), bytes.fromhex(peer)).hex() == expected


def test_x25519_refuses_a_low_order_public_key() -> None:
    with pytest.raises(BargeError) as exc_info:
        x25519.shared_secret(bytes.fromhex(CLIENT_SECRET), b"\x00" * 32)
    assert exc_info.value.kind == "refused"


def test_hkdf_sha256_matches_rfc5869_case_one() -> None:
    ikm = bytes.fromhex("0b" * 22)
    salt = bytes.fromhex("000102030405060708090a0b0c")
    info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    expected = bytes.fromhex(
        "3cb25f25faacd57a90434f64d0362f2a"
        "2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
        "34007208d5b887185865"
    )
    assert pairing.hkdf_sha256(ikm, salt, info, 42) == expected


def test_derived_key_and_code_match_barge_vector() -> None:
    client = pairing.PairingKeyPair.from_secret_hex(CLIENT_SECRET)
    daemon = pairing.PairingKeyPair.from_secret_hex(DAEMON_SECRET)

    assert client.public_key_hex() == CLIENT_PUBLIC
    assert daemon.public_key_hex() == DAEMON_PUBLIC

    from_client = client.derive(daemon.public_key, "client")
    from_daemon = daemon.derive(client.public_key, "daemon")

    assert from_client == from_daemon
    assert from_client.key.hex() == DERIVED_KEY
    assert from_client.code == CODE


def test_code_is_six_zero_padded_digits() -> None:
    # The code is a pure function of the two public keys; these two produce a
    # value below 100000, so the padding is exercised.
    client_public = bytes([16]) * 32
    daemon_public = bytes([17]) * 32
    code = pairing.verification_code(client_public, daemon_public)
    digest = hashlib.sha256(b"barge-pair-code-v1\0" + client_public + daemon_public).digest()
    expected = f"{int.from_bytes(digest[0:4], 'big') % 1_000_000:06d}"
    assert code == expected == "099393"
    assert len(code) == 6 and code.isdigit()


def test_repr_never_includes_the_key_or_secret() -> None:
    client = pairing.PairingKeyPair.from_secret_hex(CLIENT_SECRET)
    daemon = pairing.PairingKeyPair.from_secret_hex(DAEMON_SECRET)
    derived = client.derive(daemon.public_key, "client")

    assert CLIENT_SECRET not in repr(client)
    assert DERIVED_KEY not in repr(derived)
    assert CODE in repr(derived)


def test_public_key_parsing_rejects_uppercase_and_wrong_length() -> None:
    with pytest.raises(BargeError):
        pairing.parse_public_key(CLIENT_PUBLIC.upper())
    with pytest.raises(BargeError):
        pairing.parse_public_key(CLIENT_PUBLIC[:-2])
