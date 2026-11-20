"""Plugin configuration persistence and validation.

Non-secret machine configuration is stored in `machine.json` inside Decky's
settings directory; the LAN HMAC key is stored separately in `lan.key` with
owner-only (0o600) permissions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from . import auth, protocol, wol
from .errors import BargeError
from .fs_atomic import atomic_write_owner_only

CONFIG_SCHEMA = "barge-decky.config/1"
CONFIG_FILENAME = "machine.json"
KEY_FILENAME = "lan.key"

_ALLOWED_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)


def validate_name(name: str) -> str:
    """Validate machine display name (1-64 characters: letters, digits, '.', '_' or '-')."""
    trimmed = name.strip()
    if not trimmed or len(trimmed) > 64 or not all(ch in _ALLOWED_NAME_CHARS for ch in trimmed):
        raise BargeError(
            "usage",
            "machine name must be 1-64 letters, digits, '.', '_' or '-'",
        )
    return trimmed


def validate_host(host: str) -> str:
    """Validate daemon hostname or IP address."""
    trimmed = host.strip()
    if not trimmed or any(ch.isspace() for ch in trimmed):
        raise BargeError("usage", "host must not be empty or contain spaces")
    return trimmed


def validate_port(port: int, label: str) -> int:
    """Validate TCP/UDP port number (1-65535)."""
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise BargeError("usage", f"{label} must be 1-65535")
    return port


@dataclass(frozen=True)
class MachineConfig:
    """Configured target machine for the Barge Decky plugin."""

    name: str = ""
    host: str = ""
    daemon_port: int = protocol.DEFAULT_DAEMON_PORT
    mac: str = ""
    broadcast: str = wol.DEFAULT_BROADCAST
    wol_port: int = wol.DEFAULT_WOL_PORT
    delay_s: int = protocol.DEFAULT_POWER_DELAY_S

    @property
    def is_configured(self) -> bool:
        """True when both daemon host and MAC address are set."""
        return bool(self.name and self.host and self.mac)

    @property
    def can_wake(self) -> bool:
        """True when a valid MAC address is configured for Wake-on-LAN."""
        return bool(self.mac)

    @property
    def can_poll(self) -> bool:
        """True when daemon host and port are configured."""
        return bool(self.host)

    @classmethod
    def validated(
        cls,
        *,
        name: str,
        host: str,
        daemon_port: int = protocol.DEFAULT_DAEMON_PORT,
        mac: str,
        broadcast: str = wol.DEFAULT_BROADCAST,
        wol_port: int = wol.DEFAULT_WOL_PORT,
        delay_s: int = protocol.DEFAULT_POWER_DELAY_S,
    ) -> MachineConfig:
        """Build and validate a MachineConfig from user input."""
        valid_name = validate_name(name)
        valid_host = validate_host(host)
        valid_daemon_port = validate_port(daemon_port, "daemon port")
        canonical_mac = wol.format_mac(wol.parse_mac(mac))
        valid_broadcast = wol.validate_broadcast(broadcast)
        valid_wol_port = validate_port(wol_port, "Wake-on-LAN port")
        valid_delay = protocol.validate_delay_s(delay_s)
        return cls(
            name=valid_name,
            host=valid_host,
            daemon_port=valid_daemon_port,
            mac=canonical_mac,
            broadcast=valid_broadcast,
            wol_port=valid_wol_port,
            delay_s=valid_delay,
        )

    def to_public_dict(self) -> Dict[str, Any]:
        """Serialize non-secret machine fields for UI or disk."""
        return {
            "name": self.name,
            "host": self.host,
            "daemon_port": self.daemon_port,
            "mac": self.mac,
            "broadcast": self.broadcast,
            "wol_port": self.wol_port,
            "delay_s": self.delay_s,
        }


class ConfigStore:
    """Reads and writes machine configuration and the separate owner-only LAN key."""

    def __init__(self, settings_dir: Path) -> None:
        self._dir = settings_dir
        self._config_path = settings_dir / CONFIG_FILENAME
        self._key_path = settings_dir / KEY_FILENAME

    @property
    def key_path(self) -> Path:
        return self._key_path

    def load(self) -> MachineConfig:
        """Load machine config, returning an empty MachineConfig if none exists yet."""
        try:
            raw = self._config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return MachineConfig()
        except OSError as exc:
            raise BargeError("io", f"cannot read machine settings: {exc}") from exc

        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BargeError("unknown_state", f"cannot parse machine settings: {exc}") from exc

        if not isinstance(doc, dict) or doc.get("schema") != CONFIG_SCHEMA:
            raise BargeError("unknown_state", "machine settings have an unknown schema")

        machine = doc.get("machine")
        if not isinstance(machine, dict):
            raise BargeError("unknown_state", "machine settings are malformed")

        return MachineConfig.validated(
            name=str(machine.get("name", "")),
            host=str(machine.get("host", "")),
            daemon_port=int(machine.get("daemon_port", protocol.DEFAULT_DAEMON_PORT)),
            mac=str(machine.get("mac", "")),
            broadcast=str(machine.get("broadcast", wol.DEFAULT_BROADCAST)),
            wol_port=int(machine.get("wol_port", wol.DEFAULT_WOL_PORT)),
            delay_s=int(machine.get("delay_s", protocol.DEFAULT_POWER_DELAY_S)),
        )

    def save(self, config: MachineConfig) -> None:
        """Atomically persist machine config without storing the LAN key."""
        document = {
            "schema": CONFIG_SCHEMA,
            "machine": config.to_public_dict(),
        }
        encoded = (json.dumps(document, indent=2) + "\n").encode("utf-8")
        atomic_write_owner_only(
            self._config_path,
            encoded,
            temp_prefix=".machine-",
            label="machine settings",
        )

    def is_paired(self) -> bool:
        """Return True when a valid LAN key file exists."""
        try:
            return auth.read_key(self._key_path) is not None
        except BargeError:
            return False

    def read_key(self) -> Optional[bytes]:
        """Read the stored 32-byte LAN key, or None if unpaired."""
        return auth.read_key(self._key_path)

    def save_key(self, key_hex: str) -> None:
        """Validate and store a 64-char hex LAN key with owner-only permissions."""
        auth.write_key_owner_only(self._key_path, key_hex)

    def import_key_file(self, file_path: str) -> None:
        """Import a LAN key from a file on disk and store it owner-only."""
        trimmed = file_path.strip()
        if not trimmed:
            raise BargeError("usage", "key file path must not be empty")
        source = Path(trimmed).expanduser()
        try:
            content = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise BargeError("io", f"cannot read key file {source}: {exc}") from exc
        auth.write_key_owner_only(self._key_path, content)

    def unpair(self) -> bool:
        """Remove the stored LAN key."""
        return auth.remove_key(self._key_path)
