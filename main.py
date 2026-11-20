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
        """Return the latest cached machine state snapshot."""
        return self.service.snapshot()

    async def refresh_state(self) -> Dict[str, Any]:
        """Poll the daemon immediately and return the updated snapshot."""
        return await self.service.poll_once()

    async def get_settings(self) -> Dict[str, Any]:
        """Return machine configuration and pairing flag (never the key)."""
        return self.service.get_settings()

    async def save_settings(
        self,
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
        """Save machine configuration and optionally store or import the LAN key."""
        return await self.service.save_settings(
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

    async def pair_key(self, lan_key: str) -> Dict[str, Any]:
        """Store a pasted 64-character hex LAN key."""
        return await self.service.pair_key(lan_key)

    async def import_key_file(self, file_path: str) -> Dict[str, Any]:
        """Import a LAN key from a file on disk."""
        return await self.service.import_key_file(file_path)

    async def unpair(self) -> Dict[str, Any]:
        """Remove the stored LAN key."""
        return await self.service.unpair()

    async def wake(self) -> Dict[str, Any]:
        """Send a Wake-on-LAN magic packet."""
        return await self.service.wake()

    async def power(self, action: str) -> Dict[str, Any]:
        """Start a sleep, restart, or shutdown countdown on barged."""
        return await self.service.power(action)

    async def cancel(self) -> Dict[str, Any]:
        """Cancel a pending power countdown on barged."""
        return await self.service.cancel()
