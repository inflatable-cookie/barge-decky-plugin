"""Pure machine state model and observation tracker.

Ported from Barge's `apps/barge-app/src-tauri/src/machine_state.rs`,
`machines.rs`, and `apps/barge-app/src/machineModel.ts`. Open Moonlight is
intentionally omitted (Steam Deck Gaming Mode launches Moonlight itself).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Literal, Optional, Tuple

from .protocol import DaemonState, PowerActionKind

WAKE_TIMEOUT_MS = 120_000
SLEEP_TIMEOUT_MS = 60_000
RESTART_TIMEOUT_MS = 180_000

FAST_POLL_MS = 2_000
IDLE_POLL_MS = 10_000

Phase = Literal[
    "offline",
    "checking",
    "waking",
    "awake",
    "pending",
    "going_down",
    "restarting",
    "did_not_wake",
    "still_on",
    "unknown",
]

Offer = Literal["wake", "sleep", "restart", "shutdown", "cancel"]

_AWAKE_OFFERS: Tuple[Offer, ...] = ("sleep", "restart", "shutdown")


def now_ms() -> int:
    """Current Unix timestamp in milliseconds on the Deck's clock."""
    return int(time.time() * 1000)


@dataclass(frozen=True)
class LastAction:
    """The last wake or power action sent by this client."""

    kind: Literal["wake", "sleep", "restart", "shutdown"]
    sent_at_ms: int
    fires_at_ms: int
    dropped: bool = False

    @classmethod
    def wake(cls, sent_at_ms: int) -> LastAction:
        return cls(kind="wake", sent_at_ms=sent_at_ms, fires_at_ms=sent_at_ms, dropped=False)

    @classmethod
    def power(cls, action: PowerActionKind, sent_at_ms: int, delay_s: int) -> LastAction:
        return cls(
            kind=action,
            sent_at_ms=sent_at_ms,
            fires_at_ms=sent_at_ms + delay_s * 1000,
            dropped=False,
        )

    def with_dropped(self, dropped: bool = True) -> LastAction:
        return replace(self, dropped=dropped)


@dataclass(frozen=True)
class MachineStatus:
    """Derived UI status for a machine at a point in time."""

    phase: Phase
    action: Optional[PowerActionKind]
    deadline_unix_ms: Optional[int]
    last_seen_unix_ms: Optional[int]
    offers: List[Offer]
    waiting: bool


def _is_awake(observation: Optional[DaemonState]) -> bool:
    return observation is not None and observation.power == "awake"


def _last_seen(reachable: bool, seen_at_ms: Optional[int]) -> Optional[int]:
    return None if reachable else seen_at_ms


def _plain(
    phase: Phase,
    offers: List[Offer],
    reachable: bool,
    seen_at_ms: Optional[int],
) -> MachineStatus:
    return MachineStatus(
        phase=phase,
        action=None,
        deadline_unix_ms=None,
        last_seen_unix_ms=_last_seen(reachable, seen_at_ms),
        offers=offers,
        waiting=False,
    )


def _waiting_status(
    phase: Phase,
    reachable: bool,
    seen_at_ms: Optional[int],
    action: Optional[PowerActionKind] = None,
) -> MachineStatus:
    return MachineStatus(
        phase=phase,
        action=action,
        deadline_unix_ms=None,
        last_seen_unix_ms=_last_seen(reachable, seen_at_ms),
        offers=[],
        waiting=True,
    )


def _countdown(
    action: PowerActionKind,
    deadline_ms: int,
    reachable: bool,
    seen_at_ms: Optional[int],
    app_sent: bool,
) -> MachineStatus:
    return MachineStatus(
        phase="pending",
        action=action,
        deadline_unix_ms=deadline_ms,
        last_seen_unix_ms=_last_seen(reachable, seen_at_ms),
        offers=["cancel"],
        waiting=app_sent,
    )


def _settled(
    reachable: bool,
    observation: Optional[DaemonState],
    seen_at_ms: Optional[int],
    last_action: Optional[LastAction],
    has_polled: bool,
) -> MachineStatus:
    if not reachable:
        if observation is None and last_action is None and not has_polled:
            return _plain("checking", ["wake"], reachable, seen_at_ms)
        return _plain("offline", ["wake"], reachable, seen_at_ms)
    if _is_awake(observation):
        return _plain("awake", list(_AWAKE_OFFERS), reachable, seen_at_ms)
    return _plain("unknown", [], reachable, seen_at_ms)


def compute_status(
    now: int,
    reachable: bool,
    observation: Optional[DaemonState],
    seen_at_ms: Optional[int],
    last_action: Optional[LastAction],
    has_polled: bool = True,
) -> MachineStatus:
    """Pure state function matching Barge's `machine_state::status`."""
    seen = seen_at_ms if seen_at_ms is not None else now
    if observation is not None and observation.pending is not None:
        pending = observation.pending
        # Place the daemon's deadline onto the client's clock relative to
        # receipt time, so a skewed PC clock does not shift the countdown.
        distance_ms = max(0, pending.deadline_unix_ms - observation.observed_at_unix_ms)
        deadline = seen + distance_ms
        if reachable or now < deadline:
            app_sent = last_action is not None and last_action.kind != "wake"
            return _countdown(pending.action, deadline, reachable, seen_at_ms, app_sent)

    if last_action is None:
        return _settled(reachable, observation, seen_at_ms, None, has_polled)

    if last_action.kind != "wake":
        seen_since = seen_at_ms is not None and seen_at_ms >= last_action.sent_at_ms
        if now < last_action.fires_at_ms and not seen_since:
            return _countdown(
                last_action.kind,
                last_action.fires_at_ms,
                reachable,
                seen_at_ms,
                True,
            )

    elapsed = max(0, now - last_action.sent_at_ms)
    awake = reachable and _is_awake(observation)

    if last_action.kind == "wake":
        if awake:
            return _settled(reachable, observation, seen_at_ms, last_action, has_polled)
        if elapsed < WAKE_TIMEOUT_MS:
            return _waiting_status("waking", reachable, seen_at_ms)
        if reachable:
            return _settled(reachable, observation, seen_at_ms, last_action, has_polled)
        return _plain("did_not_wake", ["wake"], reachable, seen_at_ms)

    if last_action.kind == "restart":
        if now < last_action.fires_at_ms or (awake and last_action.dropped):
            return _settled(reachable, observation, seen_at_ms, last_action, has_polled)
        if elapsed < RESTART_TIMEOUT_MS:
            return _waiting_status("restarting", reachable, seen_at_ms, action="restart")
        if not reachable:
            return _plain("did_not_wake", ["wake"], reachable, seen_at_ms)
        if last_action.dropped:
            return _settled(reachable, observation, seen_at_ms, last_action, has_polled)
        return _plain("still_on", list(_AWAKE_OFFERS), reachable, seen_at_ms)

    # sleep or shutdown
    if now < last_action.fires_at_ms or not reachable:
        return _settled(reachable, observation, seen_at_ms, last_action, has_polled)
    if elapsed < SLEEP_TIMEOUT_MS:
        return _waiting_status("going_down", reachable, seen_at_ms, action=last_action.kind)
    return _plain("still_on", list(_AWAKE_OFFERS), reachable, seen_at_ms)


def completes(
    action: LastAction,
    now: int,
    reachable: bool,
    observation: Optional[DaemonState],
) -> bool:
    """Whether the current observation completes `action`, allowing its record to clear."""
    awake = reachable and _is_awake(observation)
    if action.kind == "wake":
        return awake
    if action.kind == "restart":
        return action.dropped and awake
    return not reachable and now >= action.fires_at_ms


def countdown_seconds(deadline_ms: int, now: int) -> int:
    """Whole seconds remaining, rounded up so a countdown never reads 0 before firing."""
    if deadline_ms <= now:
        return 0
    return math.ceil((deadline_ms - now) / 1000)


def offline_for(at_ms: int, now: int) -> str:
    """Format offline duration using the Mac app's concise age buckets."""
    seconds = max(0, (now - at_ms) // 1000)
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} d"


def status_label(status: MachineStatus, now: int) -> str:
    """Format state text using Barge's exact wording."""
    phase = status.phase
    if phase == "offline":
        if status.last_seen_unix_ms is None or now - status.last_seen_unix_ms < 5_000:
            return "Offline"
        return f"Offline for {offline_for(status.last_seen_unix_ms, now)}"
    if phase == "checking":
        return "Checking…"
    if phase == "waking":
        return "Waking…"
    if phase == "awake":
        return "Awake"
    if phase == "unknown":
        return "Unknown"
    if phase == "did_not_wake":
        return "Didn't wake"
    if phase == "still_on":
        return "Still on"
    if phase == "restarting":
        return "Restarting…"
    if phase == "going_down":
        return "Shutting down…" if status.action == "shutdown" else "Going to sleep…"
    if phase == "pending":
        verb = (
            "Restarting"
            if status.action == "restart"
            else "Shutting down"
            if status.action == "shutdown"
            else "Sleeping"
        )
        seconds = (
            0
            if status.deadline_unix_ms is None
            else countdown_seconds(status.deadline_unix_ms, now)
        )
        return f"{verb} in {seconds} s"
    return "Unknown"


class MachineTracker:
    """Mutable observation state for one configured machine."""

    def __init__(self) -> None:
        self.reachable: bool = False
        self.error: Optional[str] = None
        self.error_kind: Optional[str] = None
        self.observation: Optional[DaemonState] = None
        self.seen_at_ms: Optional[int] = None
        self.last_action: Optional[LastAction] = None
        self.has_polled: bool = False
        self._sending: bool = False
        self._replaced: Optional[LastAction] = None

    def reset(self) -> None:
        """Clear cached observations when the target machine changes."""
        self.reachable = False
        self.error = None
        self.error_kind = None
        self.observation = None
        self.seen_at_ms = None
        self.last_action = None
        self.has_polled = False
        self._sending = False
        self._replaced = None

    def begin(self, action: LastAction) -> None:
        """Record an action as soon as the operator clicks, before network answers."""
        self._replaced = self.last_action
        self._sending = True
        self.last_action = action
        self.error = None
        self.error_kind = None

    def sent(self, action: LastAction) -> None:
        """Commit an accepted action at its accepted time."""
        self.last_action = action
        self._sending = False
        self._replaced = None
        self.error = None
        self.error_kind = None

    def refused(self, error_message: str, error_kind: Optional[str] = None) -> None:
        """Roll back an optimistic click when sending fails."""
        if self._sending:
            self.last_action = self._replaced
            self._sending = False
            self._replaced = None
        self.error = error_message
        self.error_kind = error_kind

    def failed(self, error_message: str, error_kind: Optional[str] = None) -> None:
        """Record a failed action that never changed the optimistic state (such as cancel)."""
        self.error = error_message
        self.error_kind = error_kind

    def cancelled(self) -> None:
        """Clear pending countdown and in-flight action after confirmed cancel."""
        self.last_action = None
        self._sending = False
        self._replaced = None
        self.error = None
        self.error_kind = None
        if self.observation is not None and self.observation.pending is not None:
            self.observation = replace(self.observation, pending=None)

    def apply_poll_ok(self, started_at_ms: int, state: DaemonState) -> None:
        """Apply a successful GET /v1/state poll."""
        self.has_polled = True
        self.reachable = True
        self.error = None
        self.error_kind = None
        self.observation = state
        self.seen_at_ms = started_at_ms
        if self.last_action is not None and completes(
            self.last_action, started_at_ms, True, state
        ):
            self.last_action = None

    def apply_poll_error(
        self,
        started_at_ms: int,
        error_message: str,
        error_kind: Optional[str] = None,
    ) -> None:
        """Apply a failed poll while preserving the last successful observation."""
        self.has_polled = True
        self.reachable = False
        self.error = error_message
        self.error_kind = error_kind
        if self.last_action is not None and started_at_ms >= self.last_action.fires_at_ms:
            self.last_action = self.last_action.with_dropped(True)
        if self.last_action is not None and completes(
            self.last_action, started_at_ms, False, self.observation
        ):
            self.last_action = None

    def status(self, now: Optional[int] = None) -> MachineStatus:
        """Compute current status at `now` (defaults to current clock)."""
        current = now_ms() if now is None else now
        return compute_status(
            now=current,
            reachable=self.reachable,
            observation=self.observation,
            seen_at_ms=self.seen_at_ms,
            last_action=self.last_action,
            has_polled=self.has_polled,
        )

    def to_dict(
        self,
        now: Optional[int] = None,
        *,
        configured: bool,
        paired: bool,
        machine: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Serialize snapshot for the Decky frontend (never includes secret key)."""
        current = now_ms() if now is None else now
        derived = self.status(current)
        return {
            "configured": configured,
            "paired": paired,
            "machine": machine,
            "reachable": self.reachable,
            "error": self.error,
            "seen_at_unix_ms": self.seen_at_ms,
            "server_now_unix_ms": current,
            "phase": derived.phase,
            "action": derived.action,
            "deadline_unix_ms": derived.deadline_unix_ms,
            "last_seen_unix_ms": derived.last_seen_unix_ms,
            "offers": list(derived.offers),
            "waiting": derived.waiting,
            "label": status_label(derived, current),
            "poll_interval_ms": FAST_POLL_MS if derived.waiting else IDLE_POLL_MS,
        }
