"""Tests for Barge LAN HMAC request signing and owner-only key storage."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from barge_plugin import auth
from barge_plugin.errors import BargeError

VECTORS_PATH = Path(__file__).resolve().parent / "vectors" / "barge_machine_vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", VECTORS["hmac_cases"], ids=lambda c: c["name"])
def test_signing_matches_barge_machine_rust_vectors(case: dict[str, str]) -> None:
    key = bytes.fromhex(case["key_hex"])
    signature = auth.sign_request(
        key,
        case["method"],
        case["path"],
        case["timestamp"],
        case["nonce"],
        case["body"].encode("utf-8"),
    )
    assert signature == case["signature"]
    assert auth.verify_request(
        key,
        case["method"],
        case["path"],
        case["timestamp"],
        case["nonce"],
        case["body"].encode("utf-8"),
        signature,
    )


def test_verify_binds_all_fields_and_rejects_bad_hex() -> None:
    case = VECTORS["hmac_cases"][1]
    key = bytes.fromhex(case["key_hex"])
    body = case["body"].encode("utf-8")
    sig = case["signature"]

    # Mutating any single byte of the signature must fail.
    raw_sig = bytearray.fromhex(sig)
    for idx in range(len(raw_sig)):
        mutated = bytearray(raw_sig)
        mutated[idx] ^= 0x01
        assert not auth.verify_request(
            key, case["method"], case["path"], case["timestamp"], case["nonce"], body, mutated.hex()
        )

    assert not auth.verify_request(key, "GET", case["path"], case["timestamp"], case["nonce"], body, sig)
    assert not auth.verify_request(key, case["method"], "/other", case["timestamp"], case["nonce"], body, sig)
    assert not auth.verify_request(key, case["method"], case["path"], "0", case["nonce"], body, sig)
    assert not auth.verify_request(key, case["method"], case["path"], case["timestamp"], "other", body, sig)
    assert not auth.verify_request(key, case["method"], case["path"], case["timestamp"], case["nonce"], b"{}", sig)

    assert auth.decode_lowercase_hex("AB", 1) is None
    assert auth.decode_lowercase_hex("abc", 2) is None
    assert not auth.verify_request(key, case["method"], case["path"], case["timestamp"], case["nonce"], body, sig.upper())


def test_decode_key_validation() -> None:
    valid_hex = "ab" * 32
    assert auth.decode_key(f"  {valid_hex}\n") == bytes.fromhex(valid_hex)

    for bad in ("", "ab" * 31, "AB" * 32, "zz" * 32, "ab" * 33):
        with pytest.raises(BargeError) as exc_info:
            auth.decode_key(bad)
        assert exc_info.value.kind == "usage"


def test_key_stored_owner_only_and_replaced_atomically(tmp_path: Path) -> None:
    key_path = tmp_path / "settings" / "lan.key"
    first_key = "12" * 32
    second_key = "cd" * 32

    auth.write_key_owner_only(key_path, f"{first_key}\n")
    assert key_path.read_text(encoding="utf-8") == first_key
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert auth.read_key(key_path) == bytes.fromhex(first_key)

    auth.write_key_owner_only(key_path, second_key)
    assert auth.read_key(key_path) == bytes.fromhex(second_key)
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    # Temp files must not remain behind.
    assert [p.name for p in key_path.parent.iterdir()] == ["lan.key"]

    assert auth.remove_key(key_path) is True
    assert auth.remove_key(key_path) is False
    assert auth.read_key(key_path) is None


def test_corrupt_stored_key_is_unknown_state(tmp_path: Path) -> None:
    key_path = tmp_path / "lan.key"
    key_path.write_text("not-a-valid-key", encoding="utf-8")
    with pytest.raises(BargeError) as exc_info:
        auth.read_key(key_path)
    assert exc_info.value.kind == "unknown_state"
