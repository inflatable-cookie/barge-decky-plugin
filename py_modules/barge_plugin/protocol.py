"""Barge daemon protocol schemas, document types, and strict codecs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Optional

from .errors import BargeError

STATE_SCHEMA = "barge.machine-state/1"
POWER_REQUEST_SCHEMA = "barge.power-request/1"
POWER_RESULT_SCHEMA = "barge.power-result/1"
CANCEL_SCHEMA = "barge.power-cancel/1"
CANCEL_RESULT_SCHEMA = "barge.power-cancel-result/1"
DAEMON_ERROR_SCHEMA = "barge.daemon-error/1"

DEFAULT_DAEMON_PORT = 9777
DEFAULT_POWER_DELAY_S = 15
MAX_POWER_DELAY_S = 3600

PowerActionKind = Literal["sleep", "restart", "shutdown"]
PowerState = Literal["awake", "asleep", "unknown"]

_VALID_ACTIONS = frozenset(("sleep", "restart", "shutdown"))
_VALID_POWER_STATES = frozenset(("awake", "asleep", "unknown"))


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _reject_unknown_fields(doc: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    extra = set(doc.keys()) - allowed
    if extra:
        names = ", ".join(sorted(extra))
        raise BargeError("unknown_state", f"malformed {label}: unexpected field(s): {names}")


def _require_schema(doc: dict[str, Any], expected: str) -> None:
    actual = doc.get("schema", "")
    if not isinstance(actual, str):
        actual = ""
    if actual != expected:
        raise BargeError(
            "unknown_state",
            f"daemon answered with schema {actual!r}, not {expected}",
        )


def _parse_json_object(body: bytes, context: str) -> dict[str, Any]:
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


def decode_state(body: bytes) -> DaemonState:
    """Decode and validate a barge.machine-state/1 document."""
    doc = _parse_json_object(body, "state response")
    _require_schema(doc, STATE_SCHEMA)
    _reject_unknown_fields(
        doc,
        frozenset(("schema", "power", "observed_at_unix_ms", "pending")),
        STATE_SCHEMA,
    )
    power = doc.get("power")
    if not isinstance(power, str) or power not in _VALID_POWER_STATES:
        raise BargeError("unknown_state", f"malformed {STATE_SCHEMA}: invalid power {power!r}")

    observed_at = doc.get("observed_at_unix_ms")
    if not _is_non_negative_int(observed_at):
        raise BargeError(
            "unknown_state",
            f"malformed {STATE_SCHEMA}: invalid observed_at_unix_ms",
        )

    pending_raw = doc.get("pending")
    pending: Optional[Pending] = None
    if pending_raw is not None:
        if not isinstance(pending_raw, dict):
            raise BargeError("unknown_state", f"malformed {STATE_SCHEMA}: invalid pending")
        _reject_unknown_fields(
            pending_raw,
            frozenset(("action", "deadline_unix_ms")),
            "pending",
        )
        action = pending_raw.get("action")
        deadline = pending_raw.get("deadline_unix_ms")
        if not isinstance(action, str) or action not in _VALID_ACTIONS:
            raise BargeError("unknown_state", f"malformed {STATE_SCHEMA}: invalid pending action")
        if not _is_non_negative_int(deadline):
            raise BargeError("unknown_state", f"malformed {STATE_SCHEMA}: invalid pending deadline")
        pending = Pending(action=action, deadline_unix_ms=deadline)  # type: ignore[arg-type]

    return DaemonState(
        schema=STATE_SCHEMA,
        power=power,  # type: ignore[arg-type]
        observed_at_unix_ms=observed_at,
        pending=pending,
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
