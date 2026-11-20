"""Tests for protocol codecs and DaemonClient over loopback HTTP."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from barge_plugin import auth, protocol
from barge_plugin.errors import BargeError
from barge_plugin.transport import DaemonClient


class _StubDaemonState:
    def __init__(self, key: bytes) -> None:
        self.key = key
        self.pending: dict[str, Any] | None = None
        self.seen_nonces: set[str] = set()
        self.requests: list[tuple[str, str, bytes]] = []
        self.next_override: tuple[int, bytes] | None = None


def _start_stub_server(key: bytes) -> tuple[HTTPServer, _StubDaemonState]:
    state = _StubDaemonState(key)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_json(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorize(self, method: str, path: str, body: bytes) -> bool:
            ts = self.headers.get(auth.TIMESTAMP_HEADER, "")
            nonce = self.headers.get(auth.NONCE_HEADER, "")
            sig = self.headers.get(auth.SIGNATURE_HEADER, "")
            if not ts.isdigit() or abs(int(ts) - int(time.time())) > 60:
                return False
            if auth.decode_lowercase_hex(nonce, 16) is None or nonce in state.seen_nonces:
                return False
            if not auth.verify_request(state.key, method, path, ts, nonce, body, sig):
                return False
            state.seen_nonces.add(nonce)
            return True

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length > 0 else b""
            state.requests.append((method, self.path, body))

            if state.next_override is not None:
                status, payload = state.next_override
                state.next_override = None
                self._send_json(status, payload)
                return

            if not self._authorize(method, self.path, body):
                err = {
                    "schema": protocol.DAEMON_ERROR_SCHEMA,
                    "error": "not_authorized",
                    "message": "authentication failed",
                }
                self._send_json(401, json.dumps(err).encode("utf-8"))
                return

            now_ms = int(time.time() * 1000)
            if method == "GET" and self.path == "/v1/state":
                doc: dict[str, Any] = {
                    "schema": protocol.STATE_SCHEMA,
                    "power": "awake",
                    "observed_at_unix_ms": now_ms,
                }
                if state.pending is not None:
                    doc["pending"] = state.pending
                self._send_json(200, json.dumps(doc).encode("utf-8"))
                return

            if method == "POST" and self.path == "/v1/power":
                req = json.loads(body.decode("utf-8"))
                if state.pending is not None:
                    err = {
                        "schema": protocol.DAEMON_ERROR_SCHEMA,
                        "error": "pending",
                        "message": "a power action is already pending",
                    }
                    self._send_json(409, json.dumps(err).encode("utf-8"))
                    return
                deadline = now_ms + int(req["delay_s"]) * 1000
                state.pending = {"action": req["action"], "deadline_unix_ms": deadline}
                res = {
                    "schema": protocol.POWER_RESULT_SCHEMA,
                    "action": req["action"],
                    "deadline_unix_ms": deadline,
                }
                self._send_json(202, json.dumps(res).encode("utf-8"))
                return

            if method == "POST" and self.path == "/v1/power/cancel":
                was_pending = state.pending is not None
                state.pending = None
                res = {"schema": protocol.CANCEL_RESULT_SCHEMA, "cancelled": was_pending}
                self._send_json(200, json.dumps(res).encode("utf-8"))
                return

            err = {
                "schema": protocol.DAEMON_ERROR_SCHEMA,
                "error": "not_found",
                "message": "unknown endpoint",
            }
            self._send_json(404, json.dumps(err).encode("utf-8"))

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state


def test_lan_client_round_trips_state_power_and_cancel_with_hmac() -> None:
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
    server, stub = _start_stub_server(key)
    try:
        port = server.server_address[1]
        client = DaemonClient.for_lan("127.0.0.1", port, key)

        st = client.state()
        assert st.power == "awake"
        assert st.pending is None

        pwr = client.power("sleep", 15)
        assert pwr.action == "sleep"

        st_pending = client.state()
        assert st_pending.pending is not None
        assert st_pending.pending.action == "sleep"

        with pytest.raises(BargeError) as conflict:
            client.power("restart", 15)
        assert conflict.value.kind == "refused"
        assert "already pending" in conflict.value.message

        cancelled = client.cancel()
        assert cancelled.cancelled is True
        assert client.cancel().cancelled is False

        wrong_client = DaemonClient.for_lan("127.0.0.1", port, b"\x00" * 32)
        with pytest.raises(BargeError) as unauth:
            wrong_client.state()
        assert unauth.value.kind == "refused"
        assert "HTTP 401" in unauth.value.message
        assert len(stub.seen_nonces) == 6
    finally:
        server.shutdown()
        server.server_close()


def test_unknown_schema_is_refused_as_unknown_state() -> None:
    key = b"\x07" * 32
    server, stub = _start_stub_server(key)
    try:
        client = DaemonClient.for_lan("127.0.0.1", server.server_address[1], key)
        stub.next_override = (
            200,
            b'{"schema":"barge.machine-state/2","power":"awake","observed_at_unix_ms":1}',
        )
        with pytest.raises(BargeError) as exc_info:
            client.state()
        assert exc_info.value.kind == "unknown_state"
        assert "barge.machine-state/2" in exc_info.value.message
    finally:
        server.shutdown()
        server.server_close()


def test_unreachable_port_reports_unreachable_never_asleep() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]

    client = DaemonClient.for_lan("127.0.0.1", closed_port, b"\x07" * 32, timeout_s=1.0)
    with pytest.raises(BargeError) as exc_info:
        client.state()
    assert exc_info.value.kind == "unreachable"
    assert "asleep" not in exc_info.value.message.lower()
