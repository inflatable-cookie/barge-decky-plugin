"""Tests for the pure machine state model, actions per state, labels, and timeouts."""

from __future__ import annotations

from barge_plugin.protocol import STATE_SCHEMA, DaemonState, Pending
from barge_plugin.state import (
    RESTART_TIMEOUT_MS,
    SLEEP_TIMEOUT_MS,
    WAKE_TIMEOUT_MS,
    LastAction,
    MachineTracker,
    completes,
    compute_status,
    countdown_seconds,
    status_label,
)

T0 = 1_000_000


def awake(observed: int = T0) -> DaemonState:
    return DaemonState(schema=STATE_SCHEMA, power="awake", observed_at_unix_ms=observed)


def pending_state(action: str, observed: int, deadline: int) -> DaemonState:
    return DaemonState(
        schema=STATE_SCHEMA,
        power="awake",
        observed_at_unix_ms=observed,
        pending=Pending(action=action, deadline_unix_ms=deadline),  # type: ignore[arg-type]
    )


def test_every_state_offers_only_what_machines_md_allows() -> None:
    checking = compute_status(T0, False, None, None, None, has_polled=False)
    assert checking.phase == "checking"
    assert checking.offers == ["wake"]
    assert status_label(checking, T0) == "Checking…"
    assert not checking.waiting

    offline = compute_status(T0 + 300_000, False, awake(T0), T0, None)
    assert offline.phase == "offline"
    assert offline.offers == ["wake"]
    assert offline.last_seen_unix_ms == T0
    assert status_label(offline, T0 + 300_000) == "Offline for 5 min"
    assert "asleep" not in status_label(offline, T0 + 300_000).lower()
    assert not offline.waiting

    waking = compute_status(T0 + 2_000, False, None, None, LastAction.wake(T0))
    assert waking.phase == "waking"
    assert waking.offers == []
    assert waking.waiting
    assert status_label(waking, T0 + 2_000) == "Waking…"

    awake_status = compute_status(T0 + 5_000, True, awake(T0), T0, None)
    assert awake_status.phase == "awake"
    assert awake_status.offers == ["sleep", "restart", "shutdown"]
    assert awake_status.last_seen_unix_ms is None
    assert status_label(awake_status, T0 + 5_000) == "Awake"

    pending = compute_status(
        T0, True, pending_state("sleep", T0, T0 + 12_000), T0, None
    )
    assert pending.phase == "pending"
    assert pending.offers == ["cancel"]
    assert status_label(pending, T0) == "Sleeping in 12 s"
    assert not pending.waiting

    going_sleep = compute_status(
        T0 + 16_000,
        True,
        awake(T0 + 16_000),
        T0 + 16_000,
        LastAction.power("sleep", T0, 15),
    )
    assert going_sleep.phase == "going_down"
    assert going_sleep.offers == []
    assert going_sleep.waiting
    assert status_label(going_sleep, T0 + 16_000) == "Going to sleep…"

    going_shutdown = compute_status(
        T0 + 16_000,
        True,
        awake(T0 + 16_000),
        T0 + 16_000,
        LastAction.power("shutdown", T0, 15),
    )
    assert going_shutdown.phase == "going_down"
    assert going_shutdown.offers == []
    assert status_label(going_shutdown, T0 + 16_000) == "Shutting down…"

    restarting = compute_status(
        T0 + 16_000,
        True,
        awake(T0 + 16_000),
        T0 + 16_000,
        LastAction.power("restart", T0, 15),
    )
    assert restarting.phase == "restarting"
    assert restarting.offers == []
    assert restarting.waiting
    assert status_label(restarting, T0 + 16_000) == "Restarting…"

    did_not_wake = compute_status(
        T0 + WAKE_TIMEOUT_MS, False, None, None, LastAction.wake(T0)
    )
    assert did_not_wake.phase == "did_not_wake"
    assert did_not_wake.offers == ["wake"]
    assert not did_not_wake.waiting
    assert status_label(did_not_wake, T0 + WAKE_TIMEOUT_MS) == "Didn't wake"

    still_on = compute_status(
        T0 + SLEEP_TIMEOUT_MS,
        True,
        awake(T0 + SLEEP_TIMEOUT_MS),
        T0 + SLEEP_TIMEOUT_MS,
        LastAction.power("sleep", T0, 15),
    )
    assert still_on.phase == "still_on"
    assert still_on.offers == ["sleep", "restart", "shutdown"]
    assert not still_on.waiting
    assert status_label(still_on, T0 + SLEEP_TIMEOUT_MS) == "Still on"

    unknown = compute_status(
        T0,
        True,
        DaemonState(schema=STATE_SCHEMA, power="unknown", observed_at_unix_ms=T0),
        T0,
        None,
    )
    assert unknown.phase == "unknown"
    assert unknown.offers == []
    assert status_label(unknown, T0) == "Unknown"


def test_first_failed_poll_transitions_checking_to_offline() -> None:
    tracker = MachineTracker()
    assert tracker.status(T0).phase == "checking"
    tracker.apply_poll_error(T0 + 100, "connection refused")
    st = tracker.status(T0 + 100)
    assert st.phase == "offline"
    assert st.offers == ["wake"]
    assert status_label(st, T0 + 100) == "Offline"


def test_wake_timeout_and_arrival() -> None:
    sent = LastAction.wake(T0)
    before = compute_status(T0 + WAKE_TIMEOUT_MS - 1, False, None, None, sent)
    assert before.phase == "waking"
    assert before.waiting

    timed_out = compute_status(T0 + WAKE_TIMEOUT_MS, False, None, None, sent)
    assert timed_out.phase == "did_not_wake"
    assert not timed_out.waiting

    arrived = compute_status(
        T0 + 60_000, True, awake(T0 + 60_000), T0 + 60_000, sent
    )
    assert arrived.phase == "awake"
    assert completes(sent, T0 + 60_000, True, awake(T0 + 60_000))


def test_sleep_and_shutdown_timeouts() -> None:
    for kind in ("sleep", "shutdown"):
        sent = LastAction.power(kind, T0, 15)  # type: ignore[arg-type]
        waiting = compute_status(
            T0 + SLEEP_TIMEOUT_MS - 1,
            True,
            awake(T0 + SLEEP_TIMEOUT_MS - 1),
            T0 + SLEEP_TIMEOUT_MS - 1,
            sent,
        )
        assert waiting.phase == "going_down"
        assert waiting.waiting

        timed_out = compute_status(
            T0 + SLEEP_TIMEOUT_MS,
            True,
            awake(T0 + SLEEP_TIMEOUT_MS),
            T0 + SLEEP_TIMEOUT_MS,
            sent,
        )
        assert timed_out.phase == "still_on"
        assert not timed_out.waiting


def test_restart_passes_through_unreachable_and_has_180s_timeout() -> None:
    sent = LastAction.power("restart", T0, 15)
    before_drop = compute_status(
        T0 + 16_000, True, awake(T0 + 16_000), T0 + 16_000, sent
    )
    assert before_drop.phase == "restarting"
    assert before_drop.waiting

    dropped = sent.with_dropped(True)
    while_down = compute_status(T0 + 60_000, False, awake(T0), T0, dropped)
    assert while_down.phase == "restarting"
    assert status_label(while_down, T0 + 60_000) == "Restarting…"

    still_restarting = compute_status(
        T0 + RESTART_TIMEOUT_MS - 1, False, awake(T0), T0, dropped
    )
    assert still_restarting.phase == "restarting"

    never_came_back = compute_status(
        T0 + RESTART_TIMEOUT_MS, False, awake(T0), T0, dropped
    )
    assert never_came_back.phase == "did_not_wake"
    assert never_came_back.offers == ["wake"]

    never_dropped = compute_status(
        T0 + RESTART_TIMEOUT_MS,
        True,
        awake(T0 + RESTART_TIMEOUT_MS),
        T0 + RESTART_TIMEOUT_MS,
        sent,
    )
    assert never_dropped.phase == "still_on"

    recovered = compute_status(
        T0 + 90_000, True, awake(T0 + 90_000), T0 + 90_000, dropped
    )
    assert recovered.phase == "awake"
    assert completes(dropped, T0 + 90_000, True, awake(T0 + 90_000))


def test_daemon_clock_skew_and_countdown_rounding() -> None:
    # Daemon clock is 9 s ahead of Deck clock; 12 s left on daemon -> 12 s left here.
    daemon_now = T0 + 9_000
    st = compute_status(
        T0, True, pending_state("restart", daemon_now, daemon_now + 12_000), T0, None
    )
    assert status_label(st, T0) == "Restarting in 12 s"
    assert countdown_seconds(1_500, 0) == 2
    assert countdown_seconds(1_000, 0) == 1
    assert countdown_seconds(0, 10) == 0


def test_unreachable_pending_past_deadline_becomes_offline() -> None:
    state = pending_state("sleep", T0, T0 + 12_000)
    before = compute_status(T0 + 1_000, False, state, T0, None)
    assert before.phase == "pending"
    assert before.offers == ["cancel"]

    past = compute_status(T0 + 13_000, False, state, T0, None)
    assert past.phase == "offline"
    assert past.offers == ["wake"]


def test_tracker_optimistic_begin_and_refused_rollback() -> None:
    tracker = MachineTracker()
    tracker.apply_poll_ok(T0, awake(T0))
    tracker.apply_poll_error(T0 + 500, "timeout")
    assert tracker.status(T0 + 1_000).phase == "offline"

    tracker.begin(LastAction.wake(T0 + 1_000))
    assert tracker.status(T0 + 1_000).phase == "waking"

    tracker.refused("send failed")
    back = tracker.status(T0 + 2_000)
    assert back.phase == "offline"
    assert back.offers == ["wake"]
    assert tracker.error == "send failed"
