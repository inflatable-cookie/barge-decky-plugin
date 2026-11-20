"""A loopback fake of `barged` for pairing, approval and client-management tests.

It speaks the same documents and checks the same HMAC as the daemon
(`crates/barged/src/server.rs`), but keeps state in memory and lets a test decide
a pending request instead of waiting for an operator. Tests drive it from the
main thread; no sleeps are needed to order it.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from barge_plugin import auth, pairing, protocol
from barge_plugin.errors import BargeError


def _now_ms() -> int:
    return int(time.time() * 1000)


class _Refusal(Exception):
    """A non-2xx answer with barged's error document shape."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class FakeBarged:
    """One in-process `barged` listening on a random loopback port."""

    def __init__(
        self,
        *,
        machine_id: str = "ab" * 16,
        name: str = "Bazzite",
        mac: str = "00:11:22:33:44:ff",
        broadcast: str = "192.168.1.255",
    ) -> None:
        self.machine_id = machine_id
        self.name = name
        self.mac = mac
        self.broadcast = broadcast
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.clients: Dict[str, Dict[str, Any]] = {}
        self.client_keys: Dict[str, bytes] = {}
        self.requests: List[Tuple[str, str, Dict[str, str]]] = []
        self.power_requests: List[Tuple[str, str]] = []
        self.state_status: Optional[int] = None
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.01), daemon=True
        )

    def __enter__(self) -> FakeBarged:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def decide(self, request_id: str, status: str, client_id: str = "") -> None:
        """Force a pending request's outcome (`approved`, `denied`, `expired`)."""
        entry = self.pending[request_id]
        entry["status"] = status
        if status == "approved":
            assigned = client_id or secrets.token_hex(16)
            entry["client_id"] = assigned
            self.clients[assigned] = {
                "id": assigned,
                "name": entry["client_name"],
                "paired_at_unix_ms": _now_ms(),
            }
            self.client_keys[assigned] = entry["derived"].key

    def expire(self, request_id: str) -> None:
        self.decide(request_id, "expired")

    def pending_codes(self) -> Dict[str, str]:
        return {
            request_id: entry["code"]
            for request_id, entry in self.pending.items()
            if entry["status"] == "pending"
        }

    def _handler(self) -> type:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                pass

            def do_GET(self) -> None:
                fake._route(self, "GET")

            def do_POST(self) -> None:
                fake._route(self, "POST")

            def do_DELETE(self) -> None:
                fake._route(self, "DELETE")

        return Handler

    def _route(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        path = handler.path.split("?", 1)[0]
        length = int(handler.headers.get("content-length") or 0)
        body = handler.rfile.read(length) if length else b""
        headers = {key.lower(): value for key, value in handler.headers.items()}
        self.requests.append((method, path, headers))
        try:
            status, payload = self._answer(method, path, headers, body)
        except _Refusal as exc:
            status, payload = exc.status, self._error(exc.code, exc.message)
        except BargeError as exc:
            status, payload = 400, self._error("bad_request", exc.message)
        encoded = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(encoded)

    def _answer(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        body: bytes,
    ) -> Tuple[int, Dict[str, Any]]:
        if path == "/v1/pair" and method == "POST":
            return self._start_pair(body)
        if path == "/v1/pair/pending" and method == "GET":
            self._authorize_or_localhost(method, path, headers, body)
            return 200, {
                "schema": protocol.PAIR_PENDING_SCHEMA,
                "requests": [
                    {
                        "request_id": request_id,
                        "client_name": entry["client_name"],
                        "code": entry["code"],
                        "created_at_unix_ms": entry["created_at_unix_ms"],
                        "expires_at_unix_ms": entry["expires_at_unix_ms"],
                    }
                    for request_id, entry in self.pending.items()
                    if entry["status"] == "pending"
                ],
            }
        if path.startswith("/v1/pair/"):
            rest = path[len("/v1/pair/") :]
            if rest.endswith("/approve") and method == "POST":
                self._authorize_or_localhost(method, path, headers, body)
                return self._decide(rest[: -len("/approve")], approve=True)
            if rest.endswith("/deny") and method == "POST":
                self._authorize_or_localhost(method, path, headers, body)
                return self._decide(rest[: -len("/deny")], approve=False)
            if method == "GET":
                return self._pair_status(rest)
        if path == "/v1/state" and method == "GET":
            self._authorize_lan(method, path, headers, body)
            if self.state_status is not None:
                return self.state_status, self._error("internal", "state failed")
            return 200, self._state_document()
        if path == "/v1/clients" and method == "GET":
            self._authorize_lan(method, path, headers, body)
            return 200, {
                "schema": protocol.CLIENTS_SCHEMA,
                "clients": list(self.clients.values()),
            }
        if path.startswith("/v1/clients/") and method == "DELETE":
            self._authorize_lan(method, path, headers, body)
            client_id = path[len("/v1/clients/") :]
            if client_id not in self.clients:
                return 404, self._error("not_found", "client is not paired")
            del self.clients[client_id]
            return 200, {"schema": protocol.CLIENT_REVOKED_SCHEMA, "client_id": client_id}
        return 404, self._error("not_found", f"{path} is not an endpoint")

    def _start_pair(self, body: bytes) -> Tuple[int, Dict[str, Any]]:
        document = json.loads(body)
        assert document["schema"] == protocol.PAIR_REQUEST_SCHEMA
        client_public = pairing.parse_public_key(document["public_key"])
        daemon_keys = pairing.PairingKeyPair.generate()
        derived = daemon_keys.derive(client_public, "daemon")
        request_id = f"req-{len(self.pending) + 1}"
        self.pending[request_id] = {
            "request_id": request_id,
            "client_name": document["client_name"],
            "code": derived.code,
            "derived": derived,
            "client_public": client_public,
            "status": "pending",
            "client_id": "",
            "created_at_unix_ms": _now_ms(),
            "expires_at_unix_ms": _now_ms() + 120_000,
        }
        return 202, {
            "schema": protocol.PAIR_RESPONSE_SCHEMA,
            "request_id": request_id,
            "public_key": daemon_keys.public_key_hex(),
            "expires_at_unix_ms": self.pending[request_id]["expires_at_unix_ms"],
        }

    def _pair_status(self, request_id: str) -> Tuple[int, Dict[str, Any]]:
        entry = self.pending.get(request_id)
        if entry is None:
            return 404, self._error("not_found", "unknown pairing request")
        document: Dict[str, Any] = {
            "schema": protocol.PAIR_STATUS_SCHEMA,
            "request_id": request_id,
            "status": entry["status"],
        }
        if entry["status"] == "approved":
            document["client_id"] = entry["client_id"]
        return 200, document

    def _decide(self, request_id: str, *, approve: bool) -> Tuple[int, Dict[str, Any]]:
        entry = self.pending.get(request_id)
        if entry is None:
            return 404, self._error("not_found", "unknown pairing request")
        if entry["status"] != "pending":
            return 409, self._error("already_decided", "pairing request was already decided")
        if approve:
            self.decide(request_id, "approved")
            return 200, {
                "schema": protocol.PAIR_APPROVED_SCHEMA,
                "request_id": request_id,
                "client": self.clients[entry["client_id"]],
            }
        self.decide(request_id, "denied")
        return 200, {"schema": protocol.PAIR_DENIED_SCHEMA, "request_id": request_id}

    def _state_document(self) -> Dict[str, Any]:
        return {
            "schema": protocol.STATE_SCHEMA,
            "power": "awake",
            "observed_at_unix_ms": _now_ms(),
            "machine_id": self.machine_id,
            "mac": self.mac,
            "broadcast": self.broadcast,
        }

    def _authorize_or_localhost(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        body: bytes,
    ) -> None:
        if auth.SIGNATURE_HEADER not in headers:
            return  # the daemon trusts an unsigned loopback caller for approvals
        self._authorize_lan(method, path, headers, body)

    def _authorize_lan(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        body: bytes,
    ) -> None:
        client_id = headers.get(auth.CLIENT_ID_HEADER, "")
        key = self.client_keys.get(client_id)
        if key is None:
            raise _Refusal(401, "unauthorized", "unknown client")
        verified = auth.verify_request(
            key,
            method,
            path,
            headers.get(auth.TIMESTAMP_HEADER, ""),
            headers.get(auth.NONCE_HEADER, ""),
            body,
            headers.get(auth.SIGNATURE_HEADER, ""),
        )
        if not verified:
            raise _Refusal(401, "unauthorized", "bad signature")

    @staticmethod
    def _error(code: str, message: str) -> Dict[str, Any]:
        return {"schema": protocol.DAEMON_ERROR_SCHEMA, "error": code, "message": message}
