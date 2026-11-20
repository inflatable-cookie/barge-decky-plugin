"""Async plugin service coordinating machines, polling, Wake-on-LAN, and daemon actions.

Every machine has its own state tracker, LAN key and epoch. The selected
machine drives the panel; all configured machines are polled so the machine
dropdown can show each one's state.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, Optional

from . import protocol, wol
from .config import ConfigStore, MachineConfig, MachinesDocument, machine_id_from_name
from .error_log import ErrorEntry, ErrorLog
from .errors import BargeError
from .state import FAST_POLL_MS, IDLE_POLL_MS, LastAction, MachineTracker, now_ms
from .transport import DaemonClient, HttpTransport

EmitFn = Callable[[str, Any], Awaitable[None]]

NOT_PAIRED_MESSAGE = "Not paired — paste or import your LAN key in Settings."
UNREACHABLE = "unreachable"


class BargeService:
    """Coordinates state tracking, polling, Wake-on-LAN, and daemon actions."""

    def __init__(
        self,
        store: ConfigStore,
        *,
        transport: Optional[HttpTransport] = None,
        sender: Optional[wol.DatagramSender] = None,
        emit_fn: Optional[EmitFn] = None,
        logger: Optional[Any] = None,
        error_log: Optional[ErrorLog] = None,
    ) -> None:
        self._store = store
        self._transport = transport
        self._sender = sender
        self._emit_fn = emit_fn
        self._logger = logger
        self._trackers: Dict[str, MachineTracker] = {}
        self._epochs: Dict[str, int] = {}
        self._errors = error_log if error_log is not None else ErrorLog()
        # Last poll failure per machine ("" for settings-file errors), so a machine that
        # stays unreachable logs once rather than every poll.
        self._last_poll_error: Dict[str, str] = {}
        self._poll_lock = asyncio.Lock()
        self._wake_poller = asyncio.Event()
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._stopped = False

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Start the background polling loop."""
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._stopped = False
        target_loop = loop if loop is not None else asyncio.get_running_loop()
        self._poll_task = target_loop.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop the background polling loop cleanly."""
        self._stopped = True
        self._wake_poller.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    def snapshot(self) -> Dict[str, Any]:
        """Return the current UI state snapshot without touching the network."""
        now = now_ms()
        try:
            doc = self._store.load()
        except BargeError as exc:
            return self._compose(now, MachinesDocument(), None, document_error=exc)
        return self._compose(now, doc, doc.selected, document_error=None)

    def get_settings(self) -> Dict[str, Any]:
        """Return every machine's non-secret settings and pairing flag (never a key)."""
        try:
            doc = self._store.load()
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "selected_id": "", "machines": []}
        return {
            "ok": True,
            "selected_id": doc.selected_id,
            "machines": [self._machine_settings(machine) for machine in doc.machines],
        }

    def get_error_log(self) -> Dict[str, Any]:
        """Return the in-memory error log, newest first."""
        return {"ok": True, "entries": self._errors.entries()}

    async def select_machine(self, machine_id: str) -> Dict[str, Any]:
        """Remember which machine the panel shows."""
        try:
            doc = self._store.load()
            if doc.get(machine_id) is None:
                raise BargeError("usage", "that machine no longer exists")
            if doc.selected_id != machine_id:
                self._store.save(doc.with_selected(machine_id))
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "state": self.snapshot()}
        snap = self.snapshot()
        await self._emit_state(snap)
        return {"ok": True, "state": snap}

    async def save_settings(
        self,
        *,
        machine_id: str = "",
        name: str,
        host: str,
        daemon_port: int,
        mac: str,
        broadcast: str = wol.DEFAULT_BROADCAST,
        wol_port: int = wol.DEFAULT_WOL_PORT,
        delay_s: int = protocol.DEFAULT_POWER_DELAY_S,
        lan_key: str = "",
        key_file_path: str = "",
    ) -> Dict[str, Any]:
        """Add (empty machine_id) or edit one machine, optionally storing its LAN key.

        A newly added machine becomes the selected one.
        """
        target_id = ""
        try:
            doc = self._store.load()
            existing = doc.get(machine_id) if machine_id else None
            if machine_id and existing is None:
                raise BargeError("usage", "that machine no longer exists; reopen Settings")
            if lan_key.strip() and key_file_path.strip():
                raise BargeError(
                    "usage",
                    "provide either a pasted LAN key or a key file path, not both",
                )
            candidate_id = existing.id if existing is not None else machine_id_from_name(
                name, taken=doc.ids()
            )
            validated = MachineConfig.validated(
                machine_id=candidate_id,
                name=name,
                host=host,
                daemon_port=int(daemon_port),
                mac=mac,
                broadcast=broadcast,
                wol_port=int(wol_port),
                delay_s=int(delay_s),
            )
            self._ensure_unique_name(doc, validated)
            is_new = existing is None

            self._persist_machine(
                doc,
                validated,
                is_new=is_new,
                lan_key=lan_key,
                key_file_path=key_file_path,
            )
            tracker = self._tracker(validated.id)
            if (
                existing is None
                or existing.host != validated.host
                or existing.daemon_port != validated.daemon_port
            ):
                tracker.reset()
            self._bump(validated.id)
            target_id = validated.id
            self._log_info(f"Saved machine settings for {validated.name}")
        except (BargeError, ValueError, TypeError) as exc:
            message = exc.message if isinstance(exc, BargeError) else str(exc)
            return {
                "ok": False,
                "error": message,
                "state": self.snapshot(),
                "settings": self.get_settings(),
            }

        await self.poll_once()
        self._wake_poller.set()
        return {
            "ok": True,
            "machine_id": target_id,
            "state": self.snapshot(),
            "settings": self.get_settings(),
        }

    def _persist_machine(
        self,
        doc: MachinesDocument,
        validated: MachineConfig,
        *,
        is_new: bool,
        lan_key: str,
        key_file_path: str,
    ) -> None:
        """Write the key first, then the list; a new machine's key is removed if the list write fails."""
        if lan_key.strip():
            self._store.save_key(validated.id, lan_key)
        elif key_file_path.strip():
            self._store.import_key_file(validated.id, key_file_path)
        try:
            self._store.save(doc.upsert(validated, select=is_new))
        except BargeError:
            if is_new:
                self._store.unpair(validated.id)
            raise

    async def remove_machine(self, machine_id: str) -> Dict[str, Any]:
        """Forget a machine and delete its LAN key. The key goes first, so a failure leaves it intact."""
        try:
            doc = self._store.load()
            existing = doc.get(machine_id)
            if existing is None:
                raise BargeError("usage", "that machine no longer exists")
            self._store.unpair(existing.id)
            self._store.save(doc.without(existing.id))
        except BargeError as exc:
            return {
                "ok": False,
                "error": exc.message,
                "state": self.snapshot(),
                "settings": self.get_settings(),
            }
        self._bump(existing.id)
        self._trackers.pop(existing.id, None)
        self._last_poll_error.pop(existing.id, None)
        self._log_info(f"Removed machine {existing.name}")
        self._wake_poller.set()
        snap = self.snapshot()
        await self._emit_state(snap)
        return {"ok": True, "state": snap, "settings": self.get_settings()}

    async def pair_key(self, lan_key: str, machine_id: str = "") -> Dict[str, Any]:
        """Store a pasted 64-char hex LAN key owner-only without logging it."""
        try:
            config = self._resolve(machine_id)
            self._store.save_key(config.id, lan_key)
            self._bump(config.id)
            self._log_info(f"Updated stored LAN key for {config.name}")
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "state": self.snapshot()}
        await self.poll_once()
        self._wake_poller.set()
        return {"ok": True, "state": self.snapshot()}

    async def import_key_file(self, file_path: str, machine_id: str = "") -> Dict[str, Any]:
        """Import a LAN key from a file path on disk and store it owner-only."""
        try:
            config = self._resolve(machine_id)
            self._store.import_key_file(config.id, file_path)
            self._bump(config.id)
            self._log_info(f"Imported LAN key for {config.name} from file")
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "state": self.snapshot()}
        await self.poll_once()
        self._wake_poller.set()
        return {"ok": True, "state": self.snapshot()}

    async def unpair(self, machine_id: str = "") -> Dict[str, Any]:
        """Remove a machine's stored LAN key and refresh state."""
        try:
            config = self._resolve(machine_id)
            self._store.unpair(config.id)
            self._tracker(config.id).reset()
            self._bump(config.id)
            self._log_info(f"Removed stored LAN key for {config.name}")
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "state": self.snapshot()}
        await self.poll_once()
        self._wake_poller.set()
        return {"ok": True, "state": self.snapshot()}

    async def poll_once(self) -> Dict[str, Any]:
        """Poll GET /v1/state once for every configured machine, concurrently."""
        async with self._poll_lock:
            try:
                doc = self._store.load()
            except BargeError as exc:
                self._record_document_error(exc)
            else:
                await asyncio.gather(*(self._poll_machine(machine) for machine in doc.machines))
            snap = self.snapshot()
            await self._emit_state(snap)
            return snap

    async def wake(self, machine_id: str = "") -> Dict[str, Any]:
        """Send a Wake-on-LAN magic packet, showing Waking immediately."""
        try:
            config = self._resolve(machine_id)
            if not config.can_wake:
                raise BargeError("usage", "configure a machine MAC address first")
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "state": self.snapshot()}

        tracker = self._tracker(config.id)
        self._bump(config.id)
        tracker.begin(LastAction.wake(now_ms()))
        await self._emit_state(self.snapshot())

        try:
            dest, port, _ = await asyncio.to_thread(
                wol.wake,
                config.mac,
                config.broadcast,
                config.wol_port,
                self._sender,
            )
        except BargeError as exc:
            tracker.refused(exc.message, exc.kind)
            self._record_action_error(config, exc)
            snap = self.snapshot()
            await self._emit_state(snap)
            return {"ok": False, "error": exc.message, "state": snap}

        tracker.sent(LastAction.wake(now_ms()))
        self._log_info(f"Sent Wake-on-LAN packet for {config.name} to {dest}:{port}")
        snap = self.snapshot()
        await self._emit_state(snap)
        self._wake_poller.set()
        return {"ok": True, "state": snap}

    async def power(self, action: str, machine_id: str = "") -> Dict[str, Any]:
        """Request sleep, restart, or shutdown with immediate optimistic countdown."""
        try:
            valid_action = protocol.validate_power_action(action)
            config = self._resolve(machine_id)
            client = self._require_client(config)
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "state": self.snapshot()}

        tracker = self._tracker(config.id)
        self._bump(config.id)
        tracker.begin(LastAction.power(valid_action, now_ms(), config.delay_s))
        await self._emit_state(self.snapshot())

        try:
            await asyncio.to_thread(client.power, valid_action, config.delay_s)
        except BargeError as exc:
            tracker.refused(exc.message, exc.kind)
            self._record_action_error(config, exc)
            snap = self.snapshot()
            await self._emit_state(snap)
            return {"ok": False, "error": exc.message, "state": snap}

        tracker.sent(LastAction.power(valid_action, now_ms(), config.delay_s))
        self._log_info(f"Requested {valid_action} for {config.name} ({config.delay_s}s)")
        snap = self.snapshot()
        await self._emit_state(snap)
        self._wake_poller.set()
        return {"ok": True, "state": snap}

    async def cancel(self, machine_id: str = "") -> Dict[str, Any]:
        """Cancel a pending power action on the daemon."""
        try:
            config = self._resolve(machine_id)
            client = self._require_client(config)
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "state": self.snapshot()}

        tracker = self._tracker(config.id)
        try:
            result = await asyncio.to_thread(client.cancel)
        except BargeError as exc:
            tracker.failed(exc.message, exc.kind)
            self._record_action_error(config, exc)
            snap = self.snapshot()
            await self._emit_state(snap)
            return {"ok": False, "error": exc.message, "state": snap}

        self._bump(config.id)
        tracker.cancelled()
        self._log_info(f"Cancel response for {config.name}: cancelled={result.cancelled}")
        snap = await self.poll_once()
        self._wake_poller.set()
        return {"ok": True, "cancelled": result.cancelled, "state": snap}

    async def _poll_machine(self, config: MachineConfig) -> None:
        started_at_ms = now_ms()
        epoch = self._epoch_of(config.id)
        try:
            key = self._store.read_key(config.id)
            if key is None:
                raise BargeError("usage", NOT_PAIRED_MESSAGE)
            client = self._build_client(config, key)
            daemon_state = await asyncio.to_thread(client.state)
        except BargeError as exc:
            if epoch == self._epoch_of(config.id):
                self._tracker(config.id).apply_poll_error(started_at_ms, exc.message, exc.kind)
                self._record_poll_failure(config, exc)
            return

        if epoch == self._epoch_of(config.id):
            self._tracker(config.id).apply_poll_ok(started_at_ms, daemon_state)
            self._last_poll_error.pop(config.id, None)

    def _compose(
        self,
        now: int,
        doc: MachinesDocument,
        selected: Optional[MachineConfig],
        *,
        document_error: Optional[BargeError],
    ) -> Dict[str, Any]:
        if selected is not None:
            tracker = self._tracker(selected.id)
            snap = tracker.to_dict(
                now,
                configured=selected.is_configured,
                paired=self._store.is_paired(selected.id),
                machine=selected.to_public_dict(),
            )
            attention = self._attention(tracker)
        else:
            tracker = MachineTracker()
            snap = tracker.to_dict(
                now,
                configured=False,
                paired=False,
                machine=MachineConfig().to_public_dict(),
            )
            attention = None
        if document_error is not None:
            snap["error"] = document_error.message
            attention = {"message": document_error.message}

        machines = [self._summary(machine, now) for machine in doc.machines]
        intervals = [FAST_POLL_MS if item["waiting"] else IDLE_POLL_MS for item in machines]
        snap["selected_id"] = selected.id if selected is not None else ""
        snap["machines"] = machines
        snap["attention"] = attention
        snap["poll_interval_ms"] = min(intervals) if intervals else IDLE_POLL_MS
        return snap

    def _summary(self, config: MachineConfig, now: int) -> Dict[str, Any]:
        tracker = self._tracker(config.id)
        status = tracker.status(now)
        return {
            "id": config.id,
            "name": config.name,
            "host": config.host,
            "configured": config.is_configured,
            "paired": self._store.is_paired(config.id),
            "reachable": tracker.reachable,
            "phase": status.phase,
            "action": status.action,
            "deadline_unix_ms": status.deadline_unix_ms,
            "last_seen_unix_ms": status.last_seen_unix_ms,
            "waiting": status.waiting,
        }

    def _machine_settings(self, machine: MachineConfig) -> Dict[str, Any]:
        return {**machine.to_public_dict(), "paired": self._store.is_paired(machine.id)}

    @staticmethod
    def _attention(tracker: MachineTracker) -> Optional[Dict[str, str]]:
        """Only errors the user must act on reach the panel; unreachable is just Offline."""
        if tracker.error is None or tracker.error_kind == UNREACHABLE:
            return None
        return {"message": tracker.error}

    @staticmethod
    def _ensure_unique_name(doc: MachinesDocument, config: MachineConfig) -> None:
        wanted = config.name.lower()
        for other in doc.machines:
            if other.id != config.id and other.name.lower() == wanted:
                raise BargeError("usage", f"another machine is already named {other.name}")

    def _resolve(self, machine_id: str) -> MachineConfig:
        """Return the named machine, or the selected one when no id is given."""
        doc = self._store.load()
        if machine_id:
            config = doc.get(machine_id)
            if config is None:
                raise BargeError("usage", "that machine no longer exists")
            return config
        if doc.selected is None:
            raise BargeError("usage", "add a machine in Settings first")
        return doc.selected

    def _tracker(self, machine_id: str) -> MachineTracker:
        tracker = self._trackers.get(machine_id)
        if tracker is None:
            tracker = MachineTracker()
            self._trackers[machine_id] = tracker
        return tracker

    def _epoch_of(self, machine_id: str) -> int:
        return self._epochs.get(machine_id, 0)

    def _bump(self, machine_id: str) -> None:
        """Invalidate in-flight polls for a machine after a config change or action."""
        self._epochs[machine_id] = self._epoch_of(machine_id) + 1

    def _build_client(self, config: MachineConfig, key: bytes) -> DaemonClient:
        return DaemonClient.for_lan(
            host=config.host,
            port=config.daemon_port,
            key=key,
            transport=self._transport,
        )

    def _require_client(self, config: MachineConfig) -> DaemonClient:
        if not config.can_poll:
            raise BargeError("usage", "configure a machine host first")
        key = self._store.read_key(config.id)
        if key is None:
            raise BargeError("refused", "machine is not paired; add the LAN key in Settings")
        return self._build_client(config, key)

    def _record_poll_failure(self, config: MachineConfig, exc: BargeError) -> None:
        if self._last_poll_error.get(config.id) == exc.message:
            return
        self._last_poll_error[config.id] = exc.message
        self._add_error(config.id, config.name, exc.kind, exc.message)

    def _record_document_error(self, exc: BargeError) -> None:
        if self._last_poll_error.get("") == exc.message:
            return
        self._last_poll_error[""] = exc.message
        self._add_error("", "", exc.kind, exc.message)

    def _record_action_error(self, config: MachineConfig, exc: BargeError) -> None:
        self._add_error(config.id, config.name, exc.kind, exc.message)

    def _add_error(self, machine_id: str, machine_name: str, kind: str, message: str) -> None:
        self._errors.add(
            ErrorEntry(
                at_unix_ms=now_ms(),
                machine_id=machine_id,
                machine_name=machine_name,
                kind=kind,
                message=message,
            )
        )

    async def _poll_loop(self) -> None:
        while not self._stopped:
            snap = await self.poll_once()
            interval_ms = int(snap.get("poll_interval_ms", IDLE_POLL_MS))
            self._wake_poller.clear()
            if self._stopped:
                break
            try:
                await asyncio.wait_for(
                    self._wake_poller.wait(),
                    timeout=max(0.2, interval_ms / 1000.0),
                )
            except asyncio.TimeoutError:
                pass

    async def _emit_state(self, snapshot: Dict[str, Any]) -> None:
        if self._emit_fn is not None:
            await self._emit_fn("barge_state", snapshot)

    def _log_info(self, message: str) -> None:
        if self._logger is not None:
            self._logger.info(message)
