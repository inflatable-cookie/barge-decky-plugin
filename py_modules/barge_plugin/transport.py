"""HTTP transport seam and barged daemon client.

The transport and auth strategies sit behind small protocols so Tailscale mode
can be added later without changing the client or state machine.
"""

from __future__ import annotations

import http.client
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Protocol

from . import auth, protocol
from .errors import BargeError, ErrorKind

STATE_PATH = "/v1/state"
POWER_PATH = "/v1/power"
CANCEL_PATH = "/v1/power/cancel"
PAIR_PATH = "/v1/pair"
PAIR_PENDING_PATH = "/v1/pair/pending"
CLIENTS_PATH = "/v1/clients"
DEFAULT_TIMEOUT_S = 5.0
LAN_CONNECT_TIMEOUT_S = 1.5
_REFUSED_STATUSES = frozenset((400, 401, 403, 405, 409, 413))


@dataclass(frozen=True)
class HttpResponse:
    """Raw HTTP response returned by an HttpTransport."""

    status: int
    body: bytes


class HttpTransport(Protocol):
    """Transport seam for sending HTTP/1.1 requests to barged."""

    def request(
        self,
        method: str,
        host: str,
        port: int,
        path: str,
        headers: Dict[str, str],
        body: bytes,
        timeout_s: float,
    ) -> HttpResponse:
        """Send one HTTP request or raise BargeError('unreachable', ...)."""


class AuthStrategy(Protocol):
    """Authentication header seam (LAN HMAC now; Tailscale noop later)."""

    def headers(self, method: str, path: str, body: bytes) -> Dict[str, str]:
        """Return request headers needed to authorize this call."""


class NoopAuth:
    """Unauthenticated/network-identity auth (Tailscale, or localhost approval)."""

    def headers(self, method: str, path: str, body: bytes) -> Dict[str, str]:
        return {}


class LanHmacAuth:
    """LAN per-client HMAC request signer matching barge-machine.

    The client ID travels in `x-barge-client-id` so the daemon picks the
    matching key; it is not part of the signed payload.
    """

    def __init__(
        self,
        key: bytes,
        client_id: Optional[str] = None,
        clock_s: Optional[Callable[[], int]] = None,
        nonce_fn: Optional[Callable[[], str]] = None,
    ) -> None:
        if not key:
            raise BargeError("usage", "LAN key must not be empty")
        if client_id:
            protocol.validate_path_segment(client_id, "client id")
        self._key = key
        self._client_id = client_id or ""
        self._clock_s = clock_s if clock_s is not None else (lambda: int(time.time()))
        self._nonce_fn = nonce_fn if nonce_fn is not None else auth.generate_nonce

    def headers(self, method: str, path: str, body: bytes) -> Dict[str, str]:
        timestamp = str(self._clock_s())
        nonce = self._nonce_fn()
        signature = auth.sign_request(self._key, method, path, timestamp, nonce, body)
        headers = {
            auth.TIMESTAMP_HEADER: timestamp,
            auth.NONCE_HEADER: nonce,
            auth.SIGNATURE_HEADER: signature,
        }
        if self._client_id:
            headers[auth.CLIENT_ID_HEADER] = self._client_id
        return headers


class StdlibHttpTransport:
    """Plain HTTP/1.1 transport backed by Python stdlib http.client."""

    supports_connect_timeout = True

    def request(
        self,
        method: str,
        host: str,
        port: int,
        path: str,
        headers: Dict[str, str],
        body: bytes,
        timeout_s: float,
        connect_timeout_s: Optional[float] = None,
    ) -> HttpResponse:
        conn: Optional[http.client.HTTPConnection] = None
        connect_timeout = connect_timeout_s if connect_timeout_s is not None else timeout_s
        try:
            conn = http.client.HTTPConnection(host, port, timeout=connect_timeout)
            conn.connect()
        except (OSError, http.client.HTTPException, TimeoutError) as exc:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
            raise BargeError(
                "unreachable",
                f"barged at {self._format_base(host, port)} is unreachable: {exc}",
                not_sent=True,
            ) from exc

        try:
            if conn.sock is not None:
                conn.sock.settimeout(timeout_s)
            conn.timeout = timeout_s
            req_headers = {"Connection": "close", **headers}
            conn.request(method, path, body=body if body else None, headers=req_headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            return HttpResponse(status=resp.status, body=resp_body)
        except (OSError, http.client.HTTPException, TimeoutError) as exc:
            raise BargeError(
                "unreachable",
                f"barged at {self._format_base(host, port)} is unreachable: {exc}",
                not_sent=False,
            ) from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

    @staticmethod
    def _format_base(host: str, port: int) -> str:
        bracketed = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return f"http://{bracketed}:{port}"


class DaemonClient:
    """Client for barged's state, power, pairing and client-management endpoints."""

    def __init__(
        self,
        host: str,
        port: int = protocol.DEFAULT_DAEMON_PORT,
        auth_strategy: Optional[AuthStrategy] = None,
        transport: Optional[HttpTransport] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        connect_timeout_s: Optional[float] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._auth: AuthStrategy = auth_strategy if auth_strategy is not None else NoopAuth()
        self._transport: HttpTransport = (
            transport if transport is not None else StdlibHttpTransport()
        )
        self._timeout_s = timeout_s
        self._connect_timeout_s = connect_timeout_s

    @classmethod
    def for_lan(
        cls,
        host: str,
        port: int,
        key: bytes,
        client_id: Optional[str] = None,
        transport: Optional[HttpTransport] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        connect_timeout_s: Optional[float] = None,
    ) -> DaemonClient:
        """Build a LAN-authenticated client for one paired client key."""
        return cls(
            host=host,
            port=port,
            auth_strategy=LanHmacAuth(key, client_id),
            transport=transport,
            timeout_s=timeout_s,
            connect_timeout_s=connect_timeout_s,
        )

    @classmethod
    def for_lan_unauthenticated(
        cls,
        host: str,
        port: int,
        transport: Optional[HttpTransport] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> DaemonClient:
        """Build a client for the unauthenticated pairing endpoints."""
        return cls(host=host, port=port, transport=transport, timeout_s=timeout_s)

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> str:
        bracketed = (
            f"[{self._host}]"
            if ":" in self._host and not self._host.startswith("[")
            else self._host
        )
        return f"http://{bracketed}:{self._port}"

    def state(self) -> protocol.DaemonState:
        """GET /v1/state."""
        response = self._call("GET", STATE_PATH, b"", {})
        self._check_status(response)
        return protocol.decode_state(response.body)

    def power(self, action: protocol.PowerActionKind, delay_s: int) -> protocol.PowerResult:
        """POST /v1/power."""
        body = protocol.encode_power_request(action, delay_s)
        response = self._call(
            "POST",
            POWER_PATH,
            body,
            {"content-type": "application/json"},
        )
        self._check_status(response)
        return protocol.decode_power_result(response.body)

    def cancel(self) -> protocol.CancelResult:
        """POST /v1/power/cancel."""
        body = protocol.encode_cancel_request()
        response = self._call(
            "POST",
            CANCEL_PATH,
            body,
            {"content-type": "application/json"},
        )
        self._check_status(response)
        return protocol.decode_cancel_result(response.body)

    def start_pair(self, client_name: str, public_key_hex: str) -> protocol.PairResponse:
        """POST /v1/pair (unauthenticated)."""
        body = protocol.encode_pair_request(client_name, public_key_hex)
        response = self._call("POST", PAIR_PATH, body, {"content-type": "application/json"})
        self._check_status(response)
        return protocol.decode_pair_response(response.body)

    def pair_status(self, request_id: str) -> protocol.PairStatusResponse:
        """GET /v1/pair/<request_id> (unauthenticated)."""
        protocol.validate_path_segment(request_id, "request_id")
        response = self._call("GET", f"{PAIR_PATH}/{request_id}", b"", {})
        self._check_status(response)
        return protocol.decode_pair_status(response.body)

    def pending_pairs(self) -> protocol.PendingPairList:
        """GET /v1/pair/pending (signed, or unsigned from localhost)."""
        response = self._call("GET", PAIR_PENDING_PATH, b"", {})
        self._check_status(response)
        return protocol.decode_pending_pairs(response.body)

    def approve_pair(self, request_id: str) -> protocol.PairApproved:
        """POST /v1/pair/<request_id>/approve (signed, or unsigned from localhost)."""
        protocol.validate_path_segment(request_id, "request_id")
        response = self._call(
            "POST",
            f"{PAIR_PATH}/{request_id}/approve",
            protocol.encode_pair_decision(),
            {"content-type": "application/json"},
        )
        self._check_status(response)
        return protocol.decode_pair_approved(response.body)

    def deny_pair(self, request_id: str) -> protocol.PairDenied:
        """POST /v1/pair/<request_id>/deny (signed, or unsigned from localhost)."""
        protocol.validate_path_segment(request_id, "request_id")
        response = self._call(
            "POST",
            f"{PAIR_PATH}/{request_id}/deny",
            protocol.encode_pair_decision(),
            {"content-type": "application/json"},
        )
        self._check_status(response)
        return protocol.decode_pair_denied(response.body)

    def clients(self) -> protocol.ClientList:
        """GET /v1/clients (signed)."""
        response = self._call("GET", CLIENTS_PATH, b"", {})
        self._check_status(response)
        return protocol.decode_clients(response.body)

    def revoke_client(self, client_id: str) -> protocol.ClientRevoked:
        """DELETE /v1/clients/<client_id> (signed)."""
        protocol.validate_path_segment(client_id, "client id")
        response = self._call("DELETE", f"{CLIENTS_PATH}/{client_id}", b"", {})
        self._check_status(response)
        return protocol.decode_client_revoked(response.body)

    def _call(
        self,
        method: str,
        path: str,
        body: bytes,
        extra_headers: Dict[str, str],
    ) -> HttpResponse:
        headers = {**extra_headers, **self._auth.headers(method, path, body)}
        if (
            self._connect_timeout_s is not None
            and getattr(self._transport, "supports_connect_timeout", False)
        ):
            return self._transport.request(  # type: ignore[call-arg]
                method=method,
                host=self._host,
                port=self._port,
                path=path,
                headers=headers,
                body=body,
                timeout_s=self._timeout_s,
                connect_timeout_s=self._connect_timeout_s,
            )
        effective_timeout = (
            min(self._timeout_s, self._connect_timeout_s)
            if self._connect_timeout_s is not None
            else self._timeout_s
        )
        return self._transport.request(
            method=method,
            host=self._host,
            port=self._port,
            path=path,
            headers=headers,
            body=body,
            timeout_s=effective_timeout,
        )

    def _check_status(self, response: HttpResponse) -> None:
        if 200 <= response.status < 300:
            return
        detail = protocol.decode_error_message(response.body)
        kind: ErrorKind
        if response.status in _REFUSED_STATUSES:
            kind = "refused"
        elif response.status == 404:
            kind = "not_found"
        else:
            kind = "io"
        raise BargeError(
            kind,
            f"barged at {self.base_url} refused (HTTP {response.status}): {detail}",
        )
