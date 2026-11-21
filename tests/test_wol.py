"""Tests for Wake-on-LAN MAC parsing, magic packet construction, and UDP send."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from barge_plugin import wol
from barge_plugin.errors import BargeError

VECTORS = json.loads(
    (Path(__file__).resolve().parent / "vectors" / "barge_machine_vectors.json").read_text(
        encoding="utf-8"
    )
)


class FakeSender:
    def __init__(self, *, send_len: int | None = None, error: OSError | None = None) -> None:
        self.sent: list[tuple[bytes, str, int]] = []
        self._send_len = send_len
        self._error = error

    def send_to(self, data: bytes, host: str, port: int) -> int:
        if self._error is not None:
            raise self._error
        self.sent.append((data, host, port))
        return len(data) if self._send_len is None else self._send_len


def test_parses_common_macs_and_rejects_invalid_values() -> None:
    expected = bytes([0x00, 0x11, 0x22, 0x33, 0x44, 0xFF])
    for raw in ("00:11:22:33:44:ff", "00-11-22-33-44-FF", "0011223344ff", "0011.2233.44ff"):
        parsed = wol.parse_mac(raw)
        assert parsed == expected
        assert wol.format_mac(parsed) == "00:11:22:33:44:ff"

    for bad in ("", "0011223344", "0011223344gg", "00:11:22:33:44:55:66", "00-11:22:33:44:ff"):
        with pytest.raises(BargeError) as exc_info:
            wol.parse_mac(bad)
        assert exc_info.value.kind == "usage"


def test_magic_packet_matches_barge_machine_vector_and_sends_102_bytes() -> None:
    case = VECTORS["wol_case"]
    packet = wol.magic_packet(wol.parse_mac(case["mac"]))
    assert len(packet) == 102
    assert packet.hex() == case["packet_hex"]

    sender = FakeSender()
    host, port, count = wol.wake(case["mac"], "192.0.2.255", 9, sender=sender)
    assert (host, port, count) == ("192.0.2.255", 9, 102)
    assert sender.sent == [(packet, "192.0.2.255", 9)]


def test_wake_rejects_bad_inputs_and_reports_send_failures() -> None:
    sender = FakeSender()
    with pytest.raises(BargeError) as exc_info:
        wol.wake("00:11:22:33:44:ff", "255.255.255.255", 0, sender=sender)
    assert exc_info.value.kind == "usage"

    with pytest.raises(BargeError) as exc_info:
        wol.wake("00:11:22:33:44:ff", "not-an-ip", 9, sender=sender)
    assert exc_info.value.kind == "usage"

    short_sender = FakeSender(send_len=50)
    with pytest.raises(BargeError) as exc_info:
        wol.wake("00:11:22:33:44:ff", sender=short_sender)
    assert exc_info.value.kind == "io"

    err_sender = FakeSender(error=OSError("network unreachable"))
    with pytest.raises(BargeError) as exc_info:
        wol.wake("00:11:22:33:44:ff", sender=err_sender)
    assert exc_info.value.kind == "io"
