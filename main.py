"""Decky Loader plugin entrypoint for Barge."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

import decky  # type: ignore[import-not-found]

from barge_plugin.config import ConfigStore
from barge_plugin.protocol import DEFAULT_DAEMON_PORT, DEFAULT_POWER_DELAY_S
from barge_plugin.service import BargeService
from barge_plugin.wol import DEFAULT_BROADCAST, DEFAULT_WOL_PORT


class Plugin:
    """Decky Loader plugin wiring RPC methods and lifecycle hooks to BargeService."""

    service: BargeService

    async def _main(self) -> None:
        settings_dir = Path(decky.DECKY_PLUGIN_SETTINGS_DIR)
        self.service = BargeService(
            ConfigStore(settings_dir),
            emit_fn=decky.emit,
            logger=decky.logger,
        )
        self.service.start(asyncio.get_running_loop())
        decky.logger.info("Barge plugin started")

    async def _unload(self) -> None:
        if hasattr(self, "service"):
            await self.service.stop()
        decky.logger.info("Barge plugin unloaded")

    async def _uninstall(self) -> None:
        if hasattr(self, "service"):
            await self.service.stop()

    async def get_state(self) -> Dict[str, Any]:
        """Return the latest cached state snapshot for the selected machine and all machines."""
        return self.service.snapshot()

    async def refresh_state(self) -> Dict[str, Any]:
        """Poll every machine immediately and return the updated snapshot."""
        return await self.service.poll_once()

    async def get_settings(self) -> Dict[str, Any]:
        """Return every machine's configuration and pairing flag (never a key)."""
        return self.service.get_settings()

    async def discover_machines(self) -> Dict[str, Any]:
        """Browse `_barged._tcp` and return found machines plus the refreshed state."""
        return await self.service.discover()

    async def start_pairing(self, discovered_id: str, client_name: str = "") -> Dict[str, Any]:
        """Start pairing with a discovered machine and return the six-digit code."""
        return await self.service.start_pairing(discovered_id, client_name)

    async def pairing_status(self) -> Dict[str, Any]:
        """Check the waiting pairing request; saves the machine once approved."""
        return await self.service.pairing_status()

    async def cancel_pairing(self) -> Dict[str, Any]:
        """Stop showing the waiting pairing request."""
        return await self.service.cancel_pairing()

    async def get_clients(self, machine_id: str = "") -> Dict[str, Any]:
        """List a machine's paired clients (default: selected)."""
        return await self.service.get_clients(machine_id)

    async def revoke_client(self, client_id: str, machine_id: str = "") -> Dict[str, Any]:
        """Revoke a paired client on a machine (default: selected)."""
        return await self.service.revoke_client(client_id, machine_id)

    async def approve_pair(self, request_id: str) -> Dict[str, Any]:
        """Approve a pairing request on this PC's own barged."""
        return await self.service.approve_pair(request_id)

    async def deny_pair(self, request_id: str) -> Dict[str, Any]:
        """Deny a pairing request on this PC's own barged."""
        return await self.service.deny_pair(request_id)

    async def get_error_log(self) -> Dict[str, Any]:
        """Return the in-memory error log, newest first."""
        return self.service.get_error_log()

    async def select_machine(self, machine_id: str) -> Dict[str, Any]:
        """Make a machine the one shown on the panel."""
        return await self.service.select_machine(machine_id)

    async def save_settings(
        self,
        machine_id: str,
        name: str,
        host: str,
        daemon_port: int = DEFAULT_DAEMON_PORT,
        mac: str = "",
        broadcast: str = DEFAULT_BROADCAST,
        wol_port: int = DEFAULT_WOL_PORT,
        delay_s: int = DEFAULT_POWER_DELAY_S,
        lan_key: str = "",
        key_file_path: str = "",
    ) -> Dict[str, Any]:
        """Add a machine (empty machine_id) or edit one, optionally storing its LAN key."""
        return await self.service.save_settings(
            machine_id=machine_id,
            name=name,
            host=host,
            daemon_port=daemon_port,
            mac=mac,
            broadcast=broadcast,
            wol_port=wol_port,
            delay_s=delay_s,
            lan_key=lan_key,
            key_file_path=key_file_path,
        )

    async def remove_machine(self, machine_id: str) -> Dict[str, Any]:
        """Forget a machine and delete its LAN key."""
        return await self.service.remove_machine(machine_id)

    async def pair_key(self, lan_key: str, machine_id: str = "") -> Dict[str, Any]:
        """Store a pasted 64-character hex LAN key for a machine (default: selected)."""
        return await self.service.pair_key(lan_key, machine_id)

    async def import_key_file(self, file_path: str, machine_id: str = "") -> Dict[str, Any]:
        """Import a LAN key from a file on disk for a machine (default: selected)."""
        return await self.service.import_key_file(file_path, machine_id)

    async def unpair(self, machine_id: str = "") -> Dict[str, Any]:
        """Remove a machine's stored LAN key (default: selected)."""
        return await self.service.unpair(machine_id)

    async def wake(self, machine_id: str = "") -> Dict[str, Any]:
        """Send a Wake-on-LAN magic packet to a machine (default: selected)."""
        return await self.service.wake(machine_id)

    async def power(self, action: str, machine_id: str = "") -> Dict[str, Any]:
        """Start a sleep, restart, or shutdown countdown on barged (default: selected)."""
        return await self.service.power(action, machine_id)

    async def cancel(self, machine_id: str = "") -> Dict[str, Any]:
        """Cancel a pending power countdown on barged (default: selected)."""
        return await self.service.cancel(machine_id)
