"""Plugin machine list persistence, v0.1.0 migration, and per-machine LAN keys.

Schema v2 (current) keeps the machine list and the remembered selection in
`machines.json`. Each machine's LAN key is stored separately in
`keys/<machine-id>.key` with owner-only (0o600) permissions.

Schema v1 (v0.1.0) kept one machine in `machine.json` and its key in `lan.key`.
`ConfigStore.load()` migrates that layout in place on first use: the key is
copied verbatim before the new list is written, and the old files are removed
only after the new list exists.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import auth, protocol, wol
from .errors import BargeError
from .fs_atomic import atomic_write_owner_only

MACHINES_SCHEMA = "barge-decky.config/2"
MACHINES_FILENAME = "machines.json"
KEYS_DIRNAME = "keys"

LEGACY_SCHEMA = "barge-decky.config/1"
LEGACY_CONFIG_FILENAME = "machine.json"
LEGACY_KEY_FILENAME = "lan.key"

_MACHINE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
_ALLOWED_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)


def is_valid_machine_id(machine_id: str) -> bool:
    """Machine ids are file-name safe: lowercase letters, digits and '-', up to 48 characters."""
    return isinstance(machine_id, str) and _MACHINE_ID_RE.fullmatch(machine_id) is not None


def machine_id_from_name(name: str, taken: Iterable[str]) -> str:
    """Derive a unique, file-name safe machine id from a display name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:40].strip("-")
    base = slug or "machine"
    used = set(taken)
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


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
    """One configured target machine. Its LAN key is stored separately, under `id`."""

    id: str = ""
    name: str = ""
    host: str = ""
    daemon_port: int = protocol.DEFAULT_DAEMON_PORT
    mac: str = ""
    broadcast: str = wol.DEFAULT_BROADCAST
    wol_port: int = wol.DEFAULT_WOL_PORT
    delay_s: int = protocol.DEFAULT_POWER_DELAY_S

    @property
    def is_configured(self) -> bool:
        """True when id, name, daemon host and MAC address are all set."""
        return bool(self.id and self.name and self.host and self.mac)

    @property
    def can_wake(self) -> bool:
        """True when a valid MAC address is configured for Wake-on-LAN."""
        return bool(self.mac)

    @property
    def can_poll(self) -> bool:
        """True when daemon host is configured."""
        return bool(self.host)

    @classmethod
    def validated(
        cls,
        *,
        machine_id: str,
        name: str,
        host: str,
        daemon_port: int = protocol.DEFAULT_DAEMON_PORT,
        mac: str,
        broadcast: str = wol.DEFAULT_BROADCAST,
        wol_port: int = wol.DEFAULT_WOL_PORT,
        delay_s: int = protocol.DEFAULT_POWER_DELAY_S,
    ) -> MachineConfig:
        """Build and validate a MachineConfig from user input."""
        if not is_valid_machine_id(machine_id):
            raise BargeError("usage", "machine id must be lowercase letters, digits or '-'")
        valid_name = validate_name(name)
        valid_host = validate_host(host)
        valid_daemon_port = validate_port(daemon_port, "daemon port")
        canonical_mac = wol.format_mac(wol.parse_mac(mac))
        valid_broadcast = wol.validate_broadcast(broadcast)
        valid_wol_port = validate_port(wol_port, "Wake-on-LAN port")
        valid_delay = protocol.validate_delay_s(delay_s)
        return cls(
            id=machine_id,
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
            "id": self.id,
            "name": self.name,
            "host": self.host,
            "daemon_port": self.daemon_port,
            "mac": self.mac,
            "broadcast": self.broadcast,
            "wol_port": self.wol_port,
            "delay_s": self.delay_s,
        }


@dataclass(frozen=True)
class MachinesDocument:
    """The ordered machine list and which machine the panel has selected."""

    machines: Tuple[MachineConfig, ...] = ()
    selected_id: str = ""

    @property
    def selected(self) -> Optional[MachineConfig]:
        return self.get(self.selected_id)

    def get(self, machine_id: str) -> Optional[MachineConfig]:
        for machine in self.machines:
            if machine.id == machine_id:
                return machine
        return None

    def ids(self) -> List[str]:
        return [machine.id for machine in self.machines]

    def upsert(self, config: MachineConfig, *, select: bool) -> MachinesDocument:
        """Replace the machine with the same id, or append it. Selection moves only when asked."""
        if self.get(config.id) is None:
            machines = self.machines + (config,)
        else:
            machines = tuple(config if m.id == config.id else m for m in self.machines)
        keep_selection = self.get(self.selected_id) is not None and not select
        selected = self.selected_id if keep_selection else config.id
        return MachinesDocument(machines=machines, selected_id=selected)

    def without(self, machine_id: str) -> MachinesDocument:
        """Drop a machine; if it was selected, the first remaining machine takes over."""
        remaining = tuple(m for m in self.machines if m.id != machine_id)
        if self.selected_id != machine_id and any(m.id == self.selected_id for m in remaining):
            selected = self.selected_id
        else:
            selected = remaining[0].id if remaining else ""
        return MachinesDocument(machines=remaining, selected_id=selected)

    def with_selected(self, machine_id: str) -> MachinesDocument:
        return MachinesDocument(machines=self.machines, selected_id=machine_id)


class ConfigStore:
    """Reads and writes the machine list and the owner-only per-machine LAN keys."""

    def __init__(self, settings_dir: Path) -> None:
        self._dir = settings_dir
        self._machines_path = settings_dir / MACHINES_FILENAME
        self._keys_dir = settings_dir / KEYS_DIRNAME
        self._legacy_config_path = settings_dir / LEGACY_CONFIG_FILENAME
        self._legacy_key_path = settings_dir / LEGACY_KEY_FILENAME

    def load(self) -> MachinesDocument:
        """Return the machine list, migrating v0.1.0 settings on first load. Empty if none exist."""
        try:
            raw = self._machines_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            if self._legacy_config_path.exists():
                return self._migrate_legacy()
            return MachinesDocument()
        except OSError as exc:
            raise BargeError("io", f"cannot read machine settings: {exc}") from exc
        return self._parse(raw)

    def save(self, document: MachinesDocument) -> None:
        """Atomically persist the machine list without any LAN key material."""
        payload = {
            "schema": MACHINES_SCHEMA,
            "selected": document.selected_id,
            "machines": [machine.to_public_dict() for machine in document.machines],
        }
        encoded = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
        atomic_write_owner_only(
            self._machines_path,
            encoded,
            temp_prefix=".machines-",
            label="machine settings",
        )

    def key_path(self, machine_id: str) -> Path:
        """Path of one machine's key file. Rejects ids that could escape the keys directory."""
        if not is_valid_machine_id(machine_id):
            raise BargeError("usage", "unknown machine")
        return self._keys_dir / f"{machine_id}.key"

    def is_paired(self, machine_id: str) -> bool:
        """Return True when a valid LAN key file exists for this machine."""
        try:
            return self.read_key(machine_id) is not None
        except BargeError:
            return False

    def read_key(self, machine_id: str) -> Optional[bytes]:
        """Read the stored 32-byte LAN key for a machine, or None if unpaired."""
        return auth.read_key(self.key_path(machine_id))

    def save_key(self, machine_id: str, key_hex: str) -> None:
        """Validate and store a 64-char hex LAN key with owner-only permissions."""
        self._ensure_keys_dir()
        auth.write_key_owner_only(self.key_path(machine_id), key_hex)

    def import_key_file(self, machine_id: str, file_path: str) -> None:
        """Import a LAN key from a file on disk and store it owner-only."""
        trimmed = file_path.strip()
        if not trimmed:
            raise BargeError("usage", "key file path must not be empty")
        source = Path(trimmed).expanduser()
        try:
            content = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise BargeError("io", f"cannot read key file {source}: {exc}") from exc
        self.save_key(machine_id, content)

    def unpair(self, machine_id: str) -> bool:
        """Remove one machine's stored LAN key."""
        return auth.remove_key(self.key_path(machine_id))

    def _ensure_keys_dir(self) -> None:
        try:
            self._keys_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise BargeError("io", f"cannot create key directory: {exc}") from exc

    def _parse(self, raw: str) -> MachinesDocument:
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BargeError("unknown_state", f"cannot parse machine settings: {exc}") from exc
        if not isinstance(doc, dict) or doc.get("schema") != MACHINES_SCHEMA:
            raise BargeError("unknown_state", "machine settings have an unknown schema")
        entries = doc.get("machines")
        selected = doc.get("selected", "")
        if not isinstance(entries, list) or not isinstance(selected, str):
            raise BargeError("unknown_state", "machine settings are malformed")

        machines: List[MachineConfig] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise BargeError("unknown_state", "machine settings are malformed")
            machine = self._machine_from_dict(entry)
            if any(existing.id == machine.id for existing in machines):
                raise BargeError("unknown_state", "machine settings list a machine twice")
            machines.append(machine)

        document = MachinesDocument(machines=tuple(machines), selected_id=selected)
        if document.selected is None:
            document = document.with_selected(machines[0].id if machines else "")
        return document

    def _machine_from_dict(self, entry: Dict[str, Any]) -> MachineConfig:
        try:
            return MachineConfig.validated(
                machine_id=str(entry.get("id", "")),
                name=str(entry.get("name", "")),
                host=str(entry.get("host", "")),
                daemon_port=int(entry.get("daemon_port", protocol.DEFAULT_DAEMON_PORT)),
                mac=str(entry.get("mac", "")),
                broadcast=str(entry.get("broadcast", wol.DEFAULT_BROADCAST)),
                wol_port=int(entry.get("wol_port", wol.DEFAULT_WOL_PORT)),
                delay_s=int(entry.get("delay_s", protocol.DEFAULT_POWER_DELAY_S)),
            )
        except (BargeError, TypeError, ValueError) as exc:
            message = exc.message if isinstance(exc, BargeError) else str(exc)
            raise BargeError("unknown_state", f"machine settings are malformed: {message}") from exc

    def _migrate_legacy(self) -> MachinesDocument:
        """Move v0.1.0 `machine.json` + `lan.key` into the v2 layout, keeping the key."""
        try:
            raw = self._legacy_config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BargeError("io", f"cannot read machine settings: {exc}") from exc
        try:
            legacy = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BargeError("unknown_state", f"cannot parse machine settings: {exc}") from exc
        if not isinstance(legacy, dict) or legacy.get("schema") != LEGACY_SCHEMA:
            raise BargeError("unknown_state", "machine settings have an unknown schema")
        machine_entry = legacy.get("machine")
        if not isinstance(machine_entry, dict):
            raise BargeError("unknown_state", "machine settings are malformed")

        machine_id = machine_id_from_name(str(machine_entry.get("name", "")), taken=())
        config = self._machine_from_dict({**machine_entry, "id": machine_id})

        legacy_key = self._read_legacy_key_text()
        if legacy_key is not None:
            self._ensure_keys_dir()
            atomic_write_owner_only(
                self.key_path(machine_id),
                legacy_key.encode("utf-8"),
                temp_prefix=".barge-key-",
                label="LAN key",
            )

        document = MachinesDocument(machines=(config,), selected_id=config.id)
        self.save(document)
        self._remove_legacy_files()
        return document

    def _read_legacy_key_text(self) -> Optional[str]:
        try:
            return self._legacy_key_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BargeError("io", f"cannot read LAN key: {exc}") from exc

    def _remove_legacy_files(self) -> None:
        # Best effort: the migrated copies already exist, so a leftover file is harmless.
        for path in (self._legacy_config_path, self._legacy_key_path):
            try:
                path.unlink()
            except OSError:
                pass
