"""Barge daemon protocol schemas, document types, and strict codecs.

Matches `crates/barge-machine/src/protocol.rs`. Pairing and client-management
documents are encoded and decoded exactly as `barged` sends them; every decoder
rejects unknown fields and a non-matching schema rather than guessing.

Two deliberate exceptions, both on the state document only. The state decoder
also reads the pre-discovery `barge.machine-state/1` shape, so a machine paired
with an older daemon keeps working without the machine ID, MAC and broadcast
fields. And since installing a plugin update on the Deck is awkward, the state
decoder carries the additive `tailscale_name` / `tailscale_ip` tailnet fields and
ignores any other unknown top-level field on a machine-state document, so a
newer daemon cannot break state reads. A wrong schema name or version, a missing
required field, or a wrong type still fails. Writes always use the current
documents.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional

from . import wol
from .errors import BargeError

STATE_SCHEMA = "barge.machine-state/2"
_LEGACY_STATE_SCHEMA = "barge.machine-state/1"
POWER_REQUEST_SCHEMA = "barge.power-request/1"
POWER_RESULT_SCHEMA = "barge.power-result/1"
CANCEL_SCHEMA = "barge.power-cancel/1"
CANCEL_RESULT_SCHEMA = "barge.power-cancel-result/1"
PAIR_REQUEST_SCHEMA = "barge.pair-request/1"
PAIR_RESPONSE_SCHEMA = "barge.pair-response/1"
PAIR_STATUS_SCHEMA = "barge.pair-status/1"
PAIR_PENDING_SCHEMA = "barge.pair-pending/1"
PAIR_DECISION_SCHEMA = "barge.pair-decision/1"
PAIR_APPROVED_SCHEMA = "barge.pair-approved/1"
PAIR_DENIED_SCHEMA = "barge.pair-denied/1"
CLIENTS_SCHEMA = "barge.clients/1"
CLIENT_REVOKED_SCHEMA = "barge.client-revoked/1"
DAEMON_ERROR_SCHEMA = "barge.daemon-error/1"

DEFAULT_DAEMON_PORT = 9777
DEFAULT_POWER_DELAY_S = 15
MAX_POWER_DELAY_S = 3600
MACHINE_ID_HEX_LEN = 32

PowerActionKind = Literal["sleep", "restart", "shutdown"]
PowerState = Literal["awake", "asleep", "unknown"]
PairStatus = Literal["pending", "approved", "denied", "expired"]

_VALID_ACTIONS = frozenset(("sleep", "restart", "shutdown"))
_VALID_POWER_STATES = frozenset(("awake", "asleep", "unknown"))
_VALID_PAIR_STATUSES = frozenset(("pending", "approved", "denied", "expired"))
_SUPPORTED_STATE_SCHEMAS = frozenset((STATE_SCHEMA, _LEGACY_STATE_SCHEMA))
_PATH_SEGMENT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _reject_unknown_fields(doc: Dict[str, Any], allowed: frozenset, label: str) -> None:
    extra = set(doc.keys()) - allowed
    if extra:
        names = ", ".join(sorted(extra))
        raise BargeError("unknown_state", f"malformed {label}: unexpected field(s): {names}")


def _require_schema(doc: Dict[str, Any], expected: str) -> None:
    actual = doc.get("schema", "")
    if not isinstance(actual, str):
        actual = ""
    if actual != expected:
        raise BargeError(
            "unknown_state",
            f"daemon answered with schema {actual!r}, not {expected}",
        )


def _require_schema_in(doc: Dict[str, Any], expected: frozenset, label: str) -> str:
    actual = doc.get("schema", "")
    if not isinstance(actual, str) or actual not in expected:
        raise BargeError(
            "unknown_state",
            f"daemon answered with schema {actual!r}; expected one of "
            f"{', '.join(sorted(expected))} ({label})",
        )
    return actual


def _parse_json_object(body: bytes, context: str) -> Dict[str, Any]:
    try:
        text = body.decode("utf-8")
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BargeError(
            "unknown_state",
            f"{context}: document is not valid JSON: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise BargeError("unknown_state", f"{context}: expected JSON object")
    return value


def _require_text(doc: Dict[str, Any], field: str, label: str, *, max_len: int = 256) -> str:
    value = doc.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise BargeError("unknown_state", f"malformed {label}: invalid {field}")
    return value


def _require_object(doc: Dict[str, Any], field: str, label: str) -> Dict[str, Any]:
    value = doc.get(field)
    if not isinstance(value, dict):
        raise BargeError("unknown_state", f"malformed {label}: invalid {field}")
    return value


def _require_list(doc: Dict[str, Any], field: str, label: str) -> List[Any]:
    value = doc.get(field)
    if not isinstance(value, list):
        raise BargeError("unknown_state", f"malformed {label}: invalid {field}")
    return value


def validate_path_segment(value: str, label: str) -> str:
    """Reject identifiers that cannot be one daemon path segment (matching `barged`)."""
    if not value or len(value) > 128 or any(ch not in _PATH_SEGMENT_CHARS for ch in value):
        raise BargeError(
            "usage",
            f"{label} must be a non-empty alphanumeric, '-' or '_' identifier",
        )
    return value


def is_machine_id(value: str) -> bool:
    """True for barged's stable machine ID: 32 lowercase hex characters."""
    return (
        isinstance(value, str)
        and len(value) == MACHINE_ID_HEX_LEN
        and all(ch in "0123456789abcdef" for ch in value)
    )


def _optional_machine_id(doc: Dict[str, Any], field: str, label: str) -> str:
    value = doc.get(field, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BargeError("unknown_state", f"malformed {label}: invalid {field}")
    if value and not is_machine_id(value):
        raise BargeError(
            "unknown_state",
            f"malformed {label}: {field} must be 32 lowercase hex characters",
        )
    return value


def _optional_mac(doc: Dict[str, Any], field: str, label: str) -> Optional[str]:
    value = doc.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BargeError("unknown_state", f"malformed {label}: invalid {field}")
    try:
        return wol.format_mac(wol.parse_mac(value))
    except BargeError:
        raise BargeError("unknown_state", f"malformed {label}: invalid {field}") from None


def _optional_text(doc: Dict[str, Any], field: str, label: str) -> Optional[str]:
    """Read an additive optional string (such as a tailnet field) or None."""
    value = doc.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BargeError("unknown_state", f"malformed {label}: invalid {field}")
    return value


def _optional_broadcast(doc: Dict[str, Any], field: str, label: str) -> Optional[str]:
    value = doc.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BargeError("unknown_state", f"malformed {label}: invalid {field}")
    try:
        return wol.validate_broadcast(value)
    except BargeError:
        raise BargeError("unknown_state", f"malformed {label}: invalid {field}") from None


@dataclass(frozen=True)
class Pending:
    """A power action counting down on barged."""

    action: PowerActionKind
    deadline_unix_ms: int


@dataclass(frozen=True)
class DaemonState:
    """Machine state reported by GET /v1/state."""

    schema: str
    power: PowerState
    observed_at_unix_ms: int
    pending: Optional[Pending] = None
    machine_id: str = ""
    mac: Optional[str] = None
    broadcast: Optional[str] = None
    tailscale_name: Optional[str] = None
    tailscale_ip: Optional[str] = None


@dataclass(frozen=True)
class PowerResult:
    """Acceptance response from POST /v1/power."""

    schema: str
    action: PowerActionKind
    deadline_unix_ms: int


@dataclass(frozen=True)
class CancelResult:
    """Answer from POST /v1/power/cancel."""

    schema: str
    cancelled: bool


@dataclass(frozen=True)
class PairResponse:
    """Answer to POST /v1/pair: the daemon's ephemeral public key and request ID."""

    schema: str
    request_id: str
    public_key: str
    expires_at_unix_ms: int


@dataclass(frozen=True)
class PairStatusResponse:
    """Answer to GET /v1/pair/<request_id>."""

    schema: str
    request_id: str
    status: PairStatus
    client_id: Optional[str] = None


@dataclass(frozen=True)
class PendingPairRequest:
    """One request waiting for an operator decision."""

    request_id: str
    client_name: str
    code: str
    created_at_unix_ms: int
    expires_at_unix_ms: int


@dataclass(frozen=True)
class PendingPairList:
    """Answer to GET /v1/pair/pending."""

    schema: str
    requests: List[PendingPairRequest]


@dataclass(frozen=True)
class PairedClient:
    """A paired client's public metadata (never includes the key)."""

    id: str
    name: str
    paired_at_unix_ms: int


@dataclass(frozen=True)
class PairApproved:
    """Answer to POST /v1/pair/<request_id>/approve."""

    schema: str
    request_id: str
    client: PairedClient


@dataclass(frozen=True)
class PairDenied:
    """Answer to POST /v1/pair/<request_id>/deny."""

    schema: str
    request_id: str


@dataclass(frozen=True)
class ClientList:
    """Answer to GET /v1/clients."""

    schema: str
    clients: List[PairedClient]


@dataclass(frozen=True)
class ClientRevoked:
    """Answer to DELETE /v1/clients/<client_id>."""

    schema: str
    client_id: str


def validate_power_action(action: str) -> PowerActionKind:
    """Validate that a power action is in the closed set barged accepts."""
    if action not in _VALID_ACTIONS:
        raise BargeError(
            "usage",
            f"invalid power action {action!r}; expected sleep, restart, or shutdown",
        )
    return action  # type: ignore[return-value]


def validate_delay_s(delay_s: int) -> int:
    """Validate countdown delay seconds (0-3600)."""
    if isinstance(delay_s, bool) or not isinstance(delay_s, int) or not (0 <= delay_s <= MAX_POWER_DELAY_S):
        raise BargeError(
            "usage",
            f"delay must be 0-{MAX_POWER_DELAY_S} seconds, not {delay_s!r}",
        )
    return delay_s


def encode_power_request(action: PowerActionKind, delay_s: int) -> bytes:
    """Encode POST /v1/power JSON request bytes."""
    validate_power_action(action)
    validate_delay_s(delay_s)
    payload = {
        "schema": POWER_REQUEST_SCHEMA,
        "action": action,
        "delay_s": delay_s,
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def encode_cancel_request() -> bytes:
    """Encode POST /v1/power/cancel JSON request bytes."""
    payload = {"schema": CANCEL_SCHEMA}
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def encode_pair_request(client_name: str, public_key_hex: str) -> bytes:
    """Encode POST /v1/pair JSON request bytes."""
    name = client_name.strip()
    if not name or len(name) > 128:
        raise BargeError("usage", "client name must be 1-128 characters")
    key = public_key_hex.strip()
    if len(key) != 64 or any(ch not in "0123456789abcdef" for ch in key):
        raise BargeError("usage", "public_key must be 64 lowercase hexadecimal characters")
    payload = {
        "schema": PAIR_REQUEST_SCHEMA,
        "client_name": name,
        "public_key": key,
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def encode_pair_decision() -> bytes:
    """Encode the optional approve/deny body."""
    return json.dumps({"schema": PAIR_DECISION_SCHEMA}, separators=(",", ":")).encode("utf-8")


def decode_state(body: bytes) -> DaemonState:
    """Decode `barge.machine-state/2` (or the pre-discovery `/1` shape).

    The tailnet fields (`tailscale_name`, `tailscale_ip`) are carried when
    present; any other unknown top-level field is ignored so additive daemon
    fields never break state reads. The Deck stays LAN-only: callers must not
    use the tailnet addresses for transport.
    """
    doc = _parse_json_object(body, "state response")
    schema = _require_schema_in(doc, _SUPPORTED_STATE_SCHEMAS, "state response")
    # No unknown-field rejection here: see the module docstring.

    power = doc.get("power")
    if not isinstance(power, str) or power not in _VALID_POWER_STATES:
        raise BargeError("unknown_state", f"malformed {schema}: invalid power {power!r}")

    observed_at = doc.get("observed_at_unix_ms")
    if not _is_non_negative_int(observed_at):
        raise BargeError(
            "unknown_state",
            f"malformed {schema}: invalid observed_at_unix_ms",
        )

    pending_raw = doc.get("pending")
    pending: Optional[Pending] = None
    if pending_raw is not None:
        if not isinstance(pending_raw, dict):
            raise BargeError("unknown_state", f"malformed {schema}: invalid pending")
        _reject_unknown_fields(
            pending_raw,
            frozenset(("action", "deadline_unix_ms")),
            "pending",
        )
        action = pending_raw.get("action")
        deadline = pending_raw.get("deadline_unix_ms")
        if not isinstance(action, str) or action not in _VALID_ACTIONS:
            raise BargeError("unknown_state", f"malformed {schema}: invalid pending action")
        if not _is_non_negative_int(deadline):
            raise BargeError("unknown_state", f"malformed {schema}: invalid pending deadline")
        pending = Pending(action=action, deadline_unix_ms=deadline)  # type: ignore[arg-type]

    on_v2 = schema == STATE_SCHEMA
    return DaemonState(
        schema=schema,
        power=power,  # type: ignore[arg-type]
        observed_at_unix_ms=observed_at,
        pending=pending,
        machine_id=_optional_machine_id(doc, "machine_id", schema) if on_v2 else "",
        mac=_optional_mac(doc, "mac", schema) if on_v2 else None,
        broadcast=_optional_broadcast(doc, "broadcast", schema) if on_v2 else None,
        tailscale_name=_optional_text(doc, "tailscale_name", schema) if on_v2 else None,
        tailscale_ip=_optional_text(doc, "tailscale_ip", schema) if on_v2 else None,
    )


def decode_power_result(body: bytes) -> PowerResult:
    """Decode and validate a barge.power-result/1 document."""
    doc = _parse_json_object(body, "power response")
    _require_schema(doc, POWER_RESULT_SCHEMA)
    _reject_unknown_fields(
        doc,
        frozenset(("schema", "action", "deadline_unix_ms")),
        POWER_RESULT_SCHEMA,
    )
    action = doc.get("action")
    deadline = doc.get("deadline_unix_ms")
    if not isinstance(action, str) or action not in _VALID_ACTIONS:
        raise BargeError("unknown_state", f"malformed {POWER_RESULT_SCHEMA}: invalid action")
    if not _is_non_negative_int(deadline):
        raise BargeError("unknown_state", f"malformed {POWER_RESULT_SCHEMA}: invalid deadline")
    return PowerResult(
        schema=POWER_RESULT_SCHEMA,
        action=action,  # type: ignore[arg-type]
        deadline_unix_ms=deadline,
    )


def decode_cancel_result(body: bytes) -> CancelResult:
    """Decode and validate a barge.power-cancel-result/1 document."""
    doc = _parse_json_object(body, "cancel response")
    _require_schema(doc, CANCEL_RESULT_SCHEMA)
    _reject_unknown_fields(
        doc,
        frozenset(("schema", "cancelled")),
        CANCEL_RESULT_SCHEMA,
    )
    cancelled = doc.get("cancelled")
    if not isinstance(cancelled, bool):
        raise BargeError("unknown_state", f"malformed {CANCEL_RESULT_SCHEMA}: invalid cancelled")
    return CancelResult(schema=CANCEL_RESULT_SCHEMA, cancelled=cancelled)


def decode_pair_response(body: bytes) -> PairResponse:
    """Decode and validate a barge.pair-response/1 document."""
    doc = _parse_json_object(body, "pair response")
    _require_schema(doc, PAIR_RESPONSE_SCHEMA)
    _reject_unknown_fields(
        doc,
        frozenset(("schema", "request_id", "public_key", "expires_at_unix_ms")),
        PAIR_RESPONSE_SCHEMA,
    )
    request_id = validate_path_segment(
        _require_text(doc, "request_id", PAIR_RESPONSE_SCHEMA), "request_id"
    )
    public_key = doc.get("public_key")
    if (
        not isinstance(public_key, str)
        or len(public_key) != 64
        or any(ch not in "0123456789abcdef" for ch in public_key)
    ):
        raise BargeError(
            "unknown_state",
            f"malformed {PAIR_RESPONSE_SCHEMA}: public_key must be 64 lowercase hex characters",
        )
    expires_at = doc.get("expires_at_unix_ms")
    if not _is_non_negative_int(expires_at):
        raise BargeError(
            "unknown_state",
            f"malformed {PAIR_RESPONSE_SCHEMA}: invalid expires_at_unix_ms",
        )
    return PairResponse(
        schema=PAIR_RESPONSE_SCHEMA,
        request_id=request_id,
        public_key=public_key,
        expires_at_unix_ms=expires_at,
    )


def decode_pair_status(body: bytes) -> PairStatusResponse:
    """Decode and validate a barge.pair-status/1 document."""
    doc = _parse_json_object(body, "pair status response")
    _require_schema(doc, PAIR_STATUS_SCHEMA)
    _reject_unknown_fields(
        doc,
        frozenset(("schema", "request_id", "status", "client_id")),
        PAIR_STATUS_SCHEMA,
    )
    request_id = validate_path_segment(
        _require_text(doc, "request_id", PAIR_STATUS_SCHEMA), "request_id"
    )
    status = doc.get("status")
    if not isinstance(status, str) or status not in _VALID_PAIR_STATUSES:
        raise BargeError(
            "unknown_state",
            f"malformed {PAIR_STATUS_SCHEMA}: invalid status {status!r}",
        )
    client_id_raw = doc.get("client_id")
    client_id: Optional[str] = None
    if client_id_raw is not None:
        if not isinstance(client_id_raw, str):
            raise BargeError("unknown_state", f"malformed {PAIR_STATUS_SCHEMA}: invalid client_id")
        client_id = validate_path_segment(client_id_raw, "client_id")
    if status == "approved" and client_id is None:
        raise BargeError(
            "unknown_state",
            f"malformed {PAIR_STATUS_SCHEMA}: approved without a client_id",
        )
    return PairStatusResponse(
        schema=PAIR_STATUS_SCHEMA,
        request_id=request_id,
        status=status,  # type: ignore[arg-type]
        client_id=client_id,
    )


def _decode_pending_request(entry: Any) -> PendingPairRequest:
    label = f"{PAIR_PENDING_SCHEMA} request"
    if not isinstance(entry, dict):
        raise BargeError("unknown_state", f"malformed {label}")
    _reject_unknown_fields(
        entry,
        frozenset(
            (
                "request_id",
                "client_name",
                "code",
                "created_at_unix_ms",
                "expires_at_unix_ms",
            )
        ),
        label,
    )
    request_id = validate_path_segment(_require_text(entry, "request_id", label), "request_id")
    client_name = _require_text(entry, "client_name", label, max_len=128)
    code = entry.get("code")
    if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
        raise BargeError("unknown_state", f"malformed {label}: code must be six digits")
    created = entry.get("created_at_unix_ms")
    expires = entry.get("expires_at_unix_ms")
    if not _is_non_negative_int(created) or not _is_non_negative_int(expires):
        raise BargeError("unknown_state", f"malformed {label}: invalid timestamps")
    return PendingPairRequest(
        request_id=request_id,
        client_name=client_name,
        code=code,
        created_at_unix_ms=created,
        expires_at_unix_ms=expires,
    )


def decode_pending_pairs(body: bytes) -> PendingPairList:
    """Decode and validate a barge.pair-pending/1 document."""
    doc = _parse_json_object(body, "pending pair response")
    _require_schema(doc, PAIR_PENDING_SCHEMA)
    _reject_unknown_fields(doc, frozenset(("schema", "requests")), PAIR_PENDING_SCHEMA)
    requests = [
        _decode_pending_request(entry)
        for entry in _require_list(doc, "requests", PAIR_PENDING_SCHEMA)
    ]
    return PendingPairList(schema=PAIR_PENDING_SCHEMA, requests=requests)


def _decode_paired_client(entry: Any) -> PairedClient:
    label = "paired client"
    if not isinstance(entry, dict):
        raise BargeError("unknown_state", f"malformed {label}")
    _reject_unknown_fields(
        entry,
        frozenset(("id", "name", "paired_at_unix_ms")),
        label,
    )
    client_id = validate_path_segment(_require_text(entry, "id", label), "client id")
    paired_at = entry.get("paired_at_unix_ms")
    if not _is_non_negative_int(paired_at):
        raise BargeError("unknown_state", f"malformed {label}: invalid paired_at_unix_ms")
    return PairedClient(
        id=client_id,
        name=_require_text(entry, "name", label, max_len=128),
        paired_at_unix_ms=paired_at,
    )


def decode_pair_approved(body: bytes) -> PairApproved:
    """Decode and validate a barge.pair-approved/1 document."""
    doc = _parse_json_object(body, "pair approval response")
    _require_schema(doc, PAIR_APPROVED_SCHEMA)
    _reject_unknown_fields(
        doc,
        frozenset(("schema", "request_id", "client")),
        PAIR_APPROVED_SCHEMA,
    )
    return PairApproved(
        schema=PAIR_APPROVED_SCHEMA,
        request_id=validate_path_segment(
            _require_text(doc, "request_id", PAIR_APPROVED_SCHEMA), "request_id"
        ),
        client=_decode_paired_client(_require_object(doc, "client", PAIR_APPROVED_SCHEMA)),
    )


def decode_pair_denied(body: bytes) -> PairDenied:
    """Decode and validate a barge.pair-denied/1 document."""
    doc = _parse_json_object(body, "pair denial response")
    _require_schema(doc, PAIR_DENIED_SCHEMA)
    _reject_unknown_fields(doc, frozenset(("schema", "request_id")), PAIR_DENIED_SCHEMA)
    return PairDenied(
        schema=PAIR_DENIED_SCHEMA,
        request_id=validate_path_segment(
            _require_text(doc, "request_id", PAIR_DENIED_SCHEMA), "request_id"
        ),
    )


def decode_clients(body: bytes) -> ClientList:
    """Decode and validate a barge.clients/1 document."""
    doc = _parse_json_object(body, "clients response")
    _require_schema(doc, CLIENTS_SCHEMA)
    _reject_unknown_fields(doc, frozenset(("schema", "clients")), CLIENTS_SCHEMA)
    clients = [_decode_paired_client(entry) for entry in _require_list(doc, "clients", CLIENTS_SCHEMA)]
    return ClientList(schema=CLIENTS_SCHEMA, clients=clients)


def decode_client_revoked(body: bytes) -> ClientRevoked:
    """Decode and validate a barge.client-revoked/1 document."""
    doc = _parse_json_object(body, "client revocation response")
    _require_schema(doc, CLIENT_REVOKED_SCHEMA)
    _reject_unknown_fields(doc, frozenset(("schema", "client_id")), CLIENT_REVOKED_SCHEMA)
    return ClientRevoked(
        schema=CLIENT_REVOKED_SCHEMA,
        client_id=validate_path_segment(
            _require_text(doc, "client_id", CLIENT_REVOKED_SCHEMA), "client id"
        ),
    )


def decode_error_message(body: bytes) -> str:
    """Extract the human message from a barge.daemon-error/1 body or fallback text."""
    try:
        doc = json.loads(body.decode("utf-8"))
        if isinstance(doc, dict) and doc.get("schema") == DAEMON_ERROR_SCHEMA:
            msg = doc.get("message")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    text = body.decode("utf-8", errors="replace").strip()
    return text if text else "no detail"
