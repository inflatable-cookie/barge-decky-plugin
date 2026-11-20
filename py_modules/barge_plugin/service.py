"""Async plugin service: machines, discovery, pairing, polling, and daemon actions.

Every machine has its own state tracker, LAN key and epoch. The selected
machine drives the panel; all configured machines are polled so the machine
dropdown can show each one's state.

The pairing surfaces are delegated to cohesive modules that this service
wires together:

- `mdns` browses `_barged._tcp`; `discovery` matches answers to machines.
- `pair_flow` runs this Deck's pairing request and saves the approved machine.
- `local_approval` finds this PC's own barged on loopback and decides requests.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import discovery, local_approval, mdns, pair_flow, protocol, wol
from .config import ConfigStore, MachineConfig, MachinesDocument, machine_id_from_name
from .error_log import ErrorEntry, ErrorLog
from .errors import BargeError
from .state import FAST_POLL_MS, IDLE_POLL_MS, LastAction, MachineTracker, now_ms
from .transport import DaemonClient, HttpTransport, StdlibHttpTransport

EmitFn = Callable[[str, Any], Awaitable[None]]

NOT_PAIRED_MESSAGE = "Not paired — paste or import your LAN key in Settings."
UNREACHABLE = "unreachable"


class BargeService:
    """Coordinates state tracking, polling, discovery, pairing and daemon actions."""

    def __init__(
        self,
        store: ConfigStore,
        *,
        transport: Optional[HttpTransport] = None,
        local_transport: Optional[HttpTransport] = None,
        sender: Optional[wol.DatagramSender] = None,
        browser: Optional[mdns.MdnsBrowser] = None,
        browse_timeout_s: float = mdns.DEFAULT_BROWSE_TIMEOUT_S,
        emit_fn: Optional[EmitFn] = None,
        logger: Optional[Any] = None,
        error_log: Optional[ErrorLog] = None,
    ) -> None:
        self._store = store
        self._transport = transport
        # Approving a pair request talks to this PC's own barged over loopback, so it
        # keeps its own transport: the machine transport may later become Tailscale.
        self._local_transport: HttpTransport = (
            local_transport if local_transport is not None else StdlibHttpTransport()
        )
        self._sender = sender
        self._browser: mdns.MdnsBrowser = browser if browser is not None else mdns.UdpMdnsBrowser()
        self._browse_timeout_s = browse_timeout_s
        self._emit_fn = emit_fn
        self._logger = logger
        self._trackers: Dict[str, MachineTracker] = {}
        self._epochs: Dict[str, int] = {}
        self._errors = error_log if error_log is not None else ErrorLog()
        # Last poll failure per machine ("" for settings-file errors), so a machine that
        # stays unreachable logs once rather than every poll.
        self._last_poll_error: Dict[str, str] = {}
        self._discovered: List[mdns.DiscoveredMachine] = []
        self._discover_error: Optional[str] = None
        self._pair_flow = pair_flow.PairFlow(
            store, transport, on_paired=self._machine_paired
        )
        self._local_approvals = local_approval.LocalApprovals(store, local_transport)
        self._last_store_error: Optional[str] = None
        self._poll_lock = asyncio.Lock()
        self._wake_poller = asyncio.Event()
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._discover_task: Optional[asyncio.Task[None]] = None
        self._stopped = False

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Start the polling loop and one startup browse of the LAN."""
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._stopped = False
        target_loop = loop if loop is not None else asyncio.get_running_loop()
        self._poll_task = target_loop.create_task(self._poll_loop())
        # The panel approves this PC's pair requests, so learn its advertised
        # port even when Settings has never been opened.
        self._discover_task = target_loop.create_task(self._startup_discovery())

    async def stop(self) -> None:
        """Stop the background polling loop and startup browse cleanly."""
        self._stopped = True
        self._wake_poller.set()
        for task_name in ("_discover_task", "_poll_task"):
            task = getattr(self, task_name)
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            setattr(self, task_name, None)

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

    async def discover(self) -> Dict[str, Any]:
        """Browse `_barged._tcp`, then keep addresses current by machine ID."""
        try:
            found = await asyncio.to_thread(self._browser.browse, self._browse_timeout_s)
        except BargeError as exc:
            self._discover_error = exc.message
            snap = self.snapshot()
            await self._emit_state(snap)
            return {"ok": False, "error": exc.message, "state": snap}
        self._discovered = list(found)
        self._discover_error = None
        self._apply_discovered_addresses()
        self._local_approvals.invalidate()
        snap = await self.poll_once()
        return {"ok": True, "state": snap}

    async def start_pairing(
        self,
        discovered_id: str,
        client_name: str = "",
    ) -> Dict[str, Any]:
        """Add a discovered machine and open a pairing request with it."""
        try:
            found = self._find_discovered(discovered_id)
            view = await self._pair_flow.start(found, client_name)
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "state": self.snapshot()}

        self._log_info(f"Started pairing with {found.name}; waiting for approval")
        snap = self.snapshot()
        await self._emit_state(snap)
        return {"ok": True, "pairing": view, "state": snap}

    async def pairing_status(self) -> Dict[str, Any]:
        """Poll the waiting pairing request and save the machine once it is approved."""
        session = self._pair_flow.view()
        if session is None:
            return {"ok": True, "pairing": None, "state": self.snapshot()}
        if session["status"] != "waiting":
            return {"ok": True, "pairing": session, "state": self.snapshot()}

        update = await self._pair_flow.poll()
        snap = self.snapshot()
        await self._emit_state(snap)
        # A transient poll miss keeps the request open; report it without ending it.
        ok = not (update.status == "waiting" and update.error)
        return {
            "ok": ok,
            "error": update.error or None,
            "pairing": self._pair_flow.view(),
            "state": snap,
        }

    async def cancel_pairing(self) -> Dict[str, Any]:
        """Stop showing a waiting pairing request; barged lets it expire."""
        session = self._pair_flow.view()
        if session is not None and session["status"] == "waiting":
            self._log_info(f"Cancelled pairing with {session['name']}")
        self._pair_flow.cancel()
        snap = self.snapshot()
        await self._emit_state(snap)
        return {"ok": True, "pairing": None, "state": snap}

    async def get_clients(self, machine_id: str = "") -> Dict[str, Any]:
        """List one machine's paired clients (never a key)."""
        try:
            config = self._resolve(machine_id)
            client = self._require_client(config)
            clients = await asyncio.to_thread(client.clients)
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "clients": []}
        return {
            "ok": True,
            "clients": [
                {
                    "id": item.id,
                    "name": item.name,
                    "paired_at_unix_ms": item.paired_at_unix_ms,
                    "this_deck": item.id == config.client_id,
                }
                for item in clients.clients
            ],
        }

    async def revoke_client(self, client_id: str, machine_id: str = "") -> Dict[str, Any]:
        """Revoke one paired client on a machine and return the remaining list."""
        try:
            config = self._resolve(machine_id)
            client = self._require_client(config)
            revoked = await asyncio.to_thread(client.revoke_client, client_id)
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "clients": []}
        self._log_info(f"Revoked client {revoked.client_id} on {config.name}")
        refreshed = await self.get_clients(config.id)
        return {
            "ok": refreshed["ok"],
            "error": refreshed.get("error"),
            "revoked_is_this_deck": client_id == config.client_id,
            "clients": refreshed["clients"],
        }

    async def approve_pair(self, request_id: str) -> Dict[str, Any]:
        """Approve a pending request on this PC's own barged."""
        return await self._decide_pair(request_id, approve=True)

    async def deny_pair(self, request_id: str) -> Dict[str, Any]:
        """Deny a pending request on this PC's own barged."""
        return await self._decide_pair(request_id, approve=False)

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

        A newly added machine becomes the selected one. This is the manual
        entry fallback; discovery pairing goes through `start_pairing`.
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
                daemon_machine_id=existing.daemon_machine_id if existing is not None else "",
                client_id=existing.client_id if existing is not None else "",
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
        """Poll every configured machine and this PC's pending pair requests, concurrently."""
        async with self._poll_lock:
            try:
                doc = self._store.load()
            except BargeError as exc:
                self._record_document_error(exc)
            else:
                await asyncio.gather(*(self._poll_machine(machine) for machine in doc.machines))
                await self._local_approvals.poll(doc, self._discovered)
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
            self._learn_from_state(config, daemon_state)

    async def _decide_pair(self, request_id: str, *, approve: bool) -> Dict[str, Any]:
        try:
            client_name = await self._local_approvals.decide(request_id, approve)
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "state": self.snapshot()}

        verb = "Approved" if approve else "Denied"
        detail = f" for {client_name}" if client_name else ""
        self._log_info(f"{verb} pairing request {request_id}{detail}")
        snap = self.snapshot()
        await self._emit_state(snap)
        return {
            "ok": True,
            "approved": approve,
            "client_name": client_name,
            "state": snap,
        }

    def _find_discovered(self, discovered_id: str) -> mdns.DiscoveredMachine:
        if not protocol.is_machine_id(discovered_id):
            raise BargeError("usage", "that machine is not a valid discovery id")
        for found in self._discovered:
            if found.id == discovered_id:
                return found
        raise BargeError("usage", "that machine is no longer visible; discover again")

    def _apply_discovered_addresses(self) -> None:
        """Update configured machines whose daemon reappeared at a new address."""
        try:
            doc = self._store.load()
        except BargeError as exc:
            self._record_store_error(exc)
            return
        updated, moved = discovery.sync_addresses(doc, self._discovered)
        if not moved:
            return
        try:
            self._store.save(updated)
        except BargeError as exc:
            self._record_store_error(exc)
            return
        for machine_id in moved:
            machine = updated.get(machine_id)
            self._tracker(machine_id).reset()
            self._bump(machine_id)
            if machine is not None:
                self._log_info(f"{machine.name} is now at {machine.host}:{machine.daemon_port}")

    def _learn_from_state(self, config: MachineConfig, state: protocol.DaemonState) -> None:
        """Record the daemon's stable ID, MAC and broadcast once a poll reports them."""
        updates: Dict[str, str] = {}
        if state.machine_id and state.machine_id != config.daemon_machine_id:
            updates["daemon_machine_id"] = state.machine_id
        if state.mac and state.mac != config.mac:
            updates["mac"] = state.mac
        if state.broadcast and state.broadcast != config.broadcast:
            updates["broadcast"] = state.broadcast
        if not updates:
            return
        try:
            doc = self._store.load()
            current = doc.get(config.id)
            if current is None:
                return
            merged = replace(current, **updates)
            self._store.save(doc.upsert(merged, select=False))
        except BargeError as exc:
            self._record_store_error(exc)

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
        if self._pair_flow.is_waiting():
            intervals.append(FAST_POLL_MS)
        if self._local_approvals.has_pending():
            intervals.append(FAST_POLL_MS)
        snap["selected_id"] = selected.id if selected is not None else ""
        snap["machines"] = machines
        snap["attention"] = attention
        snap["poll_interval_ms"] = min(intervals) if intervals else IDLE_POLL_MS
        snap["discovered"] = self._discovered_view(doc)
        snap["discover_error"] = self._discover_error
        snap["pairing"] = self._pair_flow.view()
        snap["this_pc"] = self._local_approvals.view()
        return snap

    def _discovered_view(self, doc: MachinesDocument) -> List[Dict[str, Any]]:
        configured = {
            machine.daemon_machine_id: machine
            for machine in doc.machines
            if machine.daemon_machine_id
        }
        rows: List[Dict[str, Any]] = []
        for found in self._discovered:
            existing = configured.get(found.id)
            rows.append(
                {
                    **found.to_dict(),
                    "added": existing is not None,
                    "machine_id": existing.id if existing is not None else "",
                    "paired": self._store.is_paired(existing.id) if existing is not None else False,
                }
            )
        return rows

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

    def _machine_paired(self, config: MachineConfig, client_id: str) -> None:
        """Bookkeeping after PairFlow saved an approved machine."""
        self._tracker(config.id).reset()
        self._bump(config.id)
        self._log_info(f"Paired {config.name} as client {client_id}")
        self._wake_poller.set()

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
            client_id=config.client_id or None,
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

    def _record_store_error(self, exc: BargeError) -> None:
        """Log a failed settings write once per distinct message, without flooding the log."""
        if self._last_store_error == exc.message:
            return
        self._last_store_error = exc.message
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

    async def _startup_discovery(self) -> None:
        """Browse once at startup; a failure is recorded in the snapshot, not raised."""
        try:
            await self.discover()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a browser failure must not kill the poll loop
            self._discover_error = str(exc) or "mDNS browse failed"

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
