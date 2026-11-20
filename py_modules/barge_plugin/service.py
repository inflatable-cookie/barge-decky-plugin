"""Async plugin service coordinating config, polling, WoL, and barged actions."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, Optional

from . import protocol, wol
from .config import ConfigStore, MachineConfig
from .errors import BargeError
from .state import (
    IDLE_POLL_MS,
    LastAction,
    MachineTracker,
    now_ms,
)
from .transport import DaemonClient, HttpTransport

EmitFn = Callable[[str, Any], Awaitable[None]]


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
    ) -> None:
        self._store = store
        self._transport = transport
        self._sender = sender
        self._emit_fn = emit_fn
        self._logger = logger
        self._tracker = MachineTracker()
        self._epoch = 0
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
        config = self._safe_load_config()
        paired = self._store.is_paired()
        return self._tracker.to_dict(
            configured=config.is_configured,
            paired=paired,
            machine=config.to_public_dict(),
        )

    def get_settings(self) -> Dict[str, Any]:
        """Return non-secret settings and pairing status (never returns the key)."""
        try:
            config = self._store.load()
            return {
                "ok": True,
                "paired": self._store.is_paired(),
                "machine": config.to_public_dict(),
            }
        except BargeError as exc:
            return {
                "ok": False,
                "error": exc.message,
                "paired": self._store.is_paired(),
                "machine": MachineConfig().to_public_dict(),
            }

    async def save_settings(
        self,
        *,
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
        """Validate and save machine config and optional pasted/imported LAN key."""
        try:
            previous = self._safe_load_config()
            validated = MachineConfig.validated(
                name=name,
                host=host,
                daemon_port=int(daemon_port),
                mac=mac,
                broadcast=broadcast,
                wol_port=int(wol_port),
                delay_s=int(delay_s),
            )
            if lan_key.strip() and key_file_path.strip():
                raise BargeError(
                    "usage",
                    "provide either a pasted LAN key or a key file path, not both",
                )
            if lan_key.strip():
                self._store.save_key(lan_key)
            elif key_file_path.strip():
                self._store.import_key_file(key_file_path)

            self._store.save(validated)
            if (
                previous.host != validated.host
                or previous.daemon_port != validated.daemon_port
                or not previous.is_configured
            ):
                self._tracker.reset()
            self._epoch += 1
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
            "state": self.snapshot(),
            "settings": self.get_settings(),
        }

    async def pair_key(self, lan_key: str) -> Dict[str, Any]:
        """Store a pasted 64-char hex LAN key owner-only without logging it."""
        try:
            self._store.save_key(lan_key)
            self._epoch += 1
            self._log_info("Updated stored LAN key")
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "state": self.snapshot()}
        await self.poll_once()
        self._wake_poller.set()
        return {"ok": True, "state": self.snapshot()}

    async def import_key_file(self, file_path: str) -> Dict[str, Any]:
        """Import a LAN key from a file path on disk and store it owner-only."""
        try:
            self._store.import_key_file(file_path)
            self._epoch += 1
            self._log_info("Imported LAN key from file")
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "state": self.snapshot()}
        await self.poll_once()
        self._wake_poller.set()
        return {"ok": True, "state": self.snapshot()}

    async def unpair(self) -> Dict[str, Any]:
        """Remove the stored LAN key and refresh state."""
        try:
            self._store.unpair()
            self._tracker.reset()
            self._epoch += 1
            self._log_info("Removed stored LAN key")
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "state": self.snapshot()}
        await self.poll_once()
        self._wake_poller.set()
        return {"ok": True, "state": self.snapshot()}

    async def poll_once(self) -> Dict[str, Any]:
        """Poll GET /v1/state once if configured and paired."""
        async with self._poll_lock:
            try:
                config = self._store.load()
            except BargeError as exc:
                self._tracker.apply_poll_error(now_ms(), exc.message)
                snap = self.snapshot()
                await self._emit_state(snap)
                return snap

            if not config.can_poll:
                snap = self.snapshot()
                await self._emit_state(snap)
                return snap

            started_at_ms = now_ms()
            epoch = self._epoch
            try:
                key = self._store.read_key()
            except BargeError as exc:
                if epoch == self._epoch:
                    self._tracker.apply_poll_error(started_at_ms, exc.message)
                snap = self.snapshot()
                await self._emit_state(snap)
                return snap

            if key is None:
                if epoch == self._epoch:
                    self._tracker.apply_poll_error(
                        started_at_ms,
                        "Not paired — paste or import your LAN key in Settings.",
                    )
                snap = self.snapshot()
                await self._emit_state(snap)
                return snap

            client = self._build_client(config, key)
            try:
                daemon_state = await asyncio.to_thread(client.state)
            except BargeError as exc:
                if epoch == self._epoch:
                    self._tracker.apply_poll_error(started_at_ms, exc.message)
            else:
                if epoch == self._epoch:
                    self._tracker.apply_poll_ok(started_at_ms, daemon_state)

            snap = self.snapshot()
            await self._emit_state(snap)
            return snap

    async def wake(self) -> Dict[str, Any]:
        """Send a Wake-on-LAN magic packet, showing Waking immediately."""
        try:
            config = self._store.load()
            if not config.can_wake:
                raise BargeError("usage", "configure a machine MAC address first")
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "state": self.snapshot()}

        self._epoch += 1
        self._tracker.begin(LastAction.wake(now_ms()))
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
            self._tracker.refused(exc.message)
            snap = self.snapshot()
            await self._emit_state(snap)
            return {"ok": False, "error": exc.message, "state": snap}

        self._tracker.sent(LastAction.wake(now_ms()))
        self._log_info(f"Sent Wake-on-LAN packet to {dest}:{port}")
        snap = self.snapshot()
        await self._emit_state(snap)
        self._wake_poller.set()
        return {"ok": True, "state": snap}

    async def power(self, action: str) -> Dict[str, Any]:
        """Request sleep, restart, or shutdown with immediate optimistic countdown."""
        try:
            valid_action = protocol.validate_power_action(action)
            config = self._store.load()
            client = self._require_client(config)
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "state": self.snapshot()}

        self._epoch += 1
        self._tracker.begin(LastAction.power(valid_action, now_ms(), config.delay_s))
        await self._emit_state(self.snapshot())

        try:
            await asyncio.to_thread(client.power, valid_action, config.delay_s)
        except BargeError as exc:
            self._tracker.refused(exc.message)
            snap = self.snapshot()
            await self._emit_state(snap)
            return {"ok": False, "error": exc.message, "state": snap}

        self._tracker.sent(LastAction.power(valid_action, now_ms(), config.delay_s))
        self._log_info(f"Requested {valid_action} ({config.delay_s}s)")
        snap = self.snapshot()
        await self._emit_state(snap)
        self._wake_poller.set()
        return {"ok": True, "state": snap}

    async def cancel(self) -> Dict[str, Any]:
        """Cancel a pending power action on the daemon."""
        try:
            config = self._store.load()
            client = self._require_client(config)
        except BargeError as exc:
            return {"ok": False, "error": exc.message, "state": self.snapshot()}

        try:
            result = await asyncio.to_thread(client.cancel)
        except BargeError as exc:
            self._tracker.error = exc.message
            snap = self.snapshot()
            await self._emit_state(snap)
            return {"ok": False, "error": exc.message, "state": snap}

        self._epoch += 1
        self._tracker.cancelled()
        self._log_info(f"Cancel response: cancelled={result.cancelled}")
        snap = await self.poll_once()
        self._wake_poller.set()
        return {"ok": True, "cancelled": result.cancelled, "state": snap}

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
        key = self._store.read_key()
        if key is None:
            raise BargeError("refused", "machine is not paired; add the LAN key in Settings")
        return self._build_client(config, key)

    def _safe_load_config(self) -> MachineConfig:
        try:
            return self._store.load()
        except BargeError:
            return MachineConfig()

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
