"""Strict codecs for the machine-state, pairing and client documents."""

from __future__ import annotations

import json
from typing import Any

import pytest

from barge_plugin import protocol
from barge_plugin.errors import BargeError

MACHINE_ID = "ab" * 16
CLIENT_KEY = "cd" * 32


def document(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


def state_document(**overrides: Any) -> dict:
    doc = {
        "schema": protocol.STATE_SCHEMA,
        "power": "awake",
        "observed_at_unix_ms": 1_700_000_000_000,
        "machine_id": MACHINE_ID,
        "mac": "00:11:22:33:44:FF",
        "broadcast": "192.168.1.255",
    }
    doc.update(overrides)
    return doc


def test_state_v2_carries_machine_id_mac_and_broadcast() -> None:
    state = protocol.decode_state(document(state_document()))

    assert state.schema == "barge.machine-state/2"
    assert state.machine_id == MACHINE_ID
    assert state.mac == "00:11:22:33:44:ff"
    assert state.broadcast == "192.168.1.255"
    assert state.pending is None


def test_state_v1_is_still_read_without_the_new_fields() -> None:
    state = protocol.decode_state(
        document(
            {
                "schema": "barge.machine-state/1",
                "power": "asleep",
                "observed_at_unix_ms": 5,
            }
        )
    )
    assert state.schema == "barge.machine-state/1"
    assert state.machine_id == ""
    assert state.mac is None
    assert state.broadcast is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"machine_id": "not-a-machine-id"},
        {"machine_id": "AB" * 16},
        {"mac": "00:11:22:33:44"},
        {"broadcast": "192.168.1.300"},
        {"tailscale_name": 42},
        {"tailscale_ip": ["100.64.0.1"]},
        {"power": "sleeping"},
    ],
)
def test_malformed_state_documents_are_refused(overrides: dict) -> None:
    with pytest.raises(BargeError) as exc_info:
        protocol.decode_state(document(state_document(**overrides)))
    assert exc_info.value.kind == "unknown_state"


def test_state_carries_the_tailnet_fields() -> None:
    state = protocol.decode_state(
        document(
            state_document(
                tailscale_name="bazzite.tail012345.ts.net",
                tailscale_ip="100.64.0.1",
            )
        )
    )
    assert state.tailscale_name == "bazzite.tail012345.ts.net"
    assert state.tailscale_ip == "100.64.0.1"

    absent = protocol.decode_state(document(state_document()))
    assert absent.tailscale_name is None
    assert absent.tailscale_ip is None


def test_state_ignores_unknown_fields_but_still_refuses_a_bad_schema() -> None:
    state = protocol.decode_state(document(state_document(future_field="tomorrow")))
    assert state.power == "awake"
    assert state.machine_id == MACHINE_ID

    with pytest.raises(BargeError) as schema_info:
        protocol.decode_state(
            document(state_document(schema="barge.machine-state/9", future_field="tomorrow"))
        )
    assert schema_info.value.kind == "unknown_state"


def test_state_still_requires_its_required_fields() -> None:
    for payload in (
        {k: v for k, v in state_document().items() if k != "power"},
        {k: v for k, v in state_document().items() if k != "observed_at_unix_ms"},
    ):
        with pytest.raises(BargeError) as exc_info:
            protocol.decode_state(document(payload))
        assert exc_info.value.kind == "unknown_state"


def test_pair_request_encoding_matches_the_daemon_document() -> None:
    encoded = protocol.encode_pair_request("Steam Deck", CLIENT_KEY)
    assert json.loads(encoded) == {
        "schema": "barge.pair-request/1",
        "client_name": "Steam Deck",
        "public_key": CLIENT_KEY,
    }

    with pytest.raises(BargeError):
        protocol.encode_pair_request("", CLIENT_KEY)
    with pytest.raises(BargeError):
        protocol.encode_pair_request("Steam Deck", CLIENT_KEY.upper())


def test_pair_response_and_status_decode() -> None:
    response = protocol.decode_pair_response(
        document(
            {
                "schema": protocol.PAIR_RESPONSE_SCHEMA,
                "request_id": "req-1",
                "public_key": CLIENT_KEY,
                "expires_at_unix_ms": 120_000,
            }
        )
    )
    assert response.request_id == "req-1"
    assert response.expires_at_unix_ms == 120_000

    approved = protocol.decode_pair_status(
        document(
            {
                "schema": protocol.PAIR_STATUS_SCHEMA,
                "request_id": "req-1",
                "status": "approved",
                "client_id": "a1b2",
            }
        )
    )
    assert approved.status == "approved"
    assert approved.client_id == "a1b2"

    pending = protocol.decode_pair_status(
        document(
            {
                "schema": protocol.PAIR_STATUS_SCHEMA,
                "request_id": "req-1",
                "status": "pending",
            }
        )
    )
    assert pending.client_id is None


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema": protocol.PAIR_STATUS_SCHEMA,
            "request_id": "req-1",
            "status": "approved",
        },
        {
            "schema": protocol.PAIR_STATUS_SCHEMA,
            "request_id": "req-1",
            "status": "thinking",
        },
        {
            "schema": protocol.PAIR_STATUS_SCHEMA,
            "request_id": "../state",
            "status": "pending",
        },
    ],
)
def test_pair_status_refuses_an_unusable_document(payload: dict) -> None:
    with pytest.raises(BargeError):
        protocol.decode_pair_status(document(payload))


def test_pending_pairs_decode_and_validate_the_code() -> None:
    request = {
        "request_id": "req-1",
        "client_name": "Steam Deck",
        "code": "340092",
        "created_at_unix_ms": 10,
        "expires_at_unix_ms": 120_010,
    }
    parsed = protocol.decode_pending_pairs(
        document(
            {
                "schema": protocol.PAIR_PENDING_SCHEMA,
                "requests": [request],
            }
        )
    )
    assert parsed.requests[0].client_name == "Steam Deck"
    assert parsed.requests[0].code == "340092"

    for broken in (
        {**request, "code": "34009"},
        {**request, "code": "34009x"},
        {**request, "client_name": ""},
        {**request, "unexpected": 1},
    ):
        with pytest.raises(BargeError):
            protocol.decode_pending_pairs(
                document(
                    {
                        "schema": protocol.PAIR_PENDING_SCHEMA,
                        "requests": [broken],
                    }
                )
            )


def test_pair_decisions_and_client_documents_decode() -> None:
    approved = protocol.decode_pair_approved(
        document(
            {
                "schema": protocol.PAIR_APPROVED_SCHEMA,
                "request_id": "req-1",
                "client": {"id": "cid-1", "name": "Steam Deck", "paired_at_unix_ms": 10},
            }
        )
    )
    assert approved.client.id == "cid-1"

    denied = protocol.decode_pair_denied(
        document({"schema": protocol.PAIR_DENIED_SCHEMA, "request_id": "req-1"})
    )
    assert denied.request_id == "req-1"

    clients = protocol.decode_clients(
        document(
            {
                "schema": protocol.CLIENTS_SCHEMA,
                "clients": [
                    {"id": "cid-1", "name": "Steam Deck", "paired_at_unix_ms": 10},
                    {"id": "legacy", "name": "legacy", "paired_at_unix_ms": 5},
                ],
            }
        )
    )
    assert [client.id for client in clients.clients] == ["cid-1", "legacy"]

    revoked = protocol.decode_client_revoked(
        document({"schema": protocol.CLIENT_REVOKED_SCHEMA, "client_id": "cid-1"})
    )
    assert revoked.client_id == "cid-1"

    with pytest.raises(BargeError):
        protocol.decode_clients(
            document(
                {
                    "schema": protocol.CLIENTS_SCHEMA,
                    "clients": [
                        {"id": "cid-1", "name": "Steam Deck", "paired_at_unix_ms": 10, "key": "x"}
                    ],
                }
            )
        )


def test_pair_decision_body_matches_the_optional_document() -> None:
    assert json.loads(protocol.encode_pair_decision()) == {
        "schema": protocol.PAIR_DECISION_SCHEMA
    }


def test_path_segments_cannot_escape_the_route() -> None:
    for value in ("", "a/b", "../state", "a b", "a?b"):
        with pytest.raises(BargeError):
            protocol.validate_path_segment(value, "request_id")
    assert protocol.validate_path_segment("req-1_AB", "request_id") == "req-1_AB"
