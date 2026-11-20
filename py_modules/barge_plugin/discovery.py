"""Reconcile LAN advertisements with the configured machine list."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from ipaddress import ip_address
from typing import Dict, List, Optional, Sequence, Tuple

from . import mdns
from .config import ConfigStore, MachineConfig, MachinesDocument, machine_id_from_name, validate_name
from .errors import BargeError
from .transport import DaemonClient, HttpTransport

_DISCOVERY_STATE_TIMEOUT_S = 1.0


async def resolve_macs(
    doc: MachinesDocument,
    found_list: Sequence[mdns.DiscoveredMachine],
    store: ConfigStore,
    transport: Optional[HttpTransport],
) -> List[mdns.DiscoveredMachine]:
    """Get MACs from state when a saved machine still lacks its daemon ID."""
    credentials: List[Tuple[MachineConfig, bytes]] = []
    known_ids = {machine.daemon_machine_id for machine in doc.machines}
    for machine in doc.machines:
        if machine.daemon_machine_id or not machine.mac:
            continue
        try:
            key = store.read_key(machine.id)
        except BargeError:
            continue  # Normal polling reports settings and key errors.
        if key is not None:
            credentials.append((machine, key))
    if not credentials:
        return list(found_list)

    async def resolve(found: mdns.DiscoveredMachine) -> mdns.DiscoveredMachine:
        if found.id in known_ids or found.mac:
            return found
        return await asyncio.to_thread(_resolve_mac, found, credentials, transport)

    return await asyncio.gather(*(resolve(found) for found in found_list))


def _resolve_mac(
    found: mdns.DiscoveredMachine,
    credentials: Sequence[Tuple[MachineConfig, bytes]],
    transport: Optional[HttpTransport],
) -> mdns.DiscoveredMachine:
    # mDNS does not advertise a MAC. If the state request fails, matching can
    # still use the address and hostname.
    for machine, key in credentials:
        client = DaemonClient.for_lan(
            found.address,
            found.port,
            key,
            client_id=machine.client_id or None,
            transport=transport,
            timeout_s=_DISCOVERY_STATE_TIMEOUT_S,
        )
        try:
            state = client.state()
        except BargeError as exc:
            if exc.kind == "unreachable":
                break
            continue
        if state.machine_id == found.id and state.mac:
            return replace(found, mac=state.mac)
        break  # A different key cannot make this daemon report a different identity.
    return found


def match_machine(
    doc: MachinesDocument,
    found: mdns.DiscoveredMachine,
) -> Optional[MachineConfig]:
    """Find the configured machine by ID, MAC, IP address, then mDNS hostname."""
    return _best_match(doc, found)[0]


def _best_match(
    doc: MachinesDocument,
    found: mdns.DiscoveredMachine,
) -> Tuple[Optional[MachineConfig], int]:
    best = None
    highest = 0
    for machine in doc.machines:
        priority = _match_priority(machine, found)
        if priority > highest:
            best, highest = machine, priority
    return best, highest


def _match_priority(machine: MachineConfig, found: mdns.DiscoveredMachine) -> int:
    if machine.daemon_machine_id:
        # A different stable ID rules out matches on weaker identifiers.
        return 4 if machine.daemon_machine_id == found.id else 0
    if machine.mac and machine.mac == found.mac:
        return 3
    try:
        if ip_address(machine.host) == ip_address(found.address):
            return 2
    except ValueError:
        pass  # A manually configured host can be a DNS name.
    if found.hostname and _local_hostname(machine.host) == _local_hostname(found.hostname):
        return 1
    return 0


def _local_hostname(hostname: str) -> str:
    return hostname.rstrip(".").lower().removesuffix(".local")


def sync_addresses(
    doc: MachinesDocument,
    found_list: Sequence[mdns.DiscoveredMachine],
) -> Tuple[MachinesDocument, List[str]]:
    """Return the document with moved machines updated, and the ids that moved."""
    matches: Dict[str, List[Tuple[int, mdns.DiscoveredMachine]]] = {}
    for found in found_list:
        machine, priority = _best_match(doc, found)
        if machine is not None:
            matches.setdefault(machine.id, []).append((priority, found))

    updated: List[MachineConfig] = []
    changed: List[str] = []
    for machine in doc.machines:
        candidates = matches.get(machine.id, [])
        highest = max((priority for priority, _ in candidates), default=0)
        preferred = [found for priority, found in candidates if priority == highest]
        if len(preferred) == 1:
            found = preferred[0]
            if (machine.host, machine.daemon_port) != (found.address, found.port):
                machine = replace(machine, host=found.address, daemon_port=found.port)
                changed.append(machine.id)
        updated.append(machine)
    if not changed:
        return doc, changed
    return MachinesDocument(machines=tuple(updated), selected_id=doc.selected_id), changed


def unique_display_name(doc: MachinesDocument, display: str) -> str:
    """Make a daemon's display name acceptable to the settings format and unique."""
    try:
        base = validate_name(display)
    except BargeError:
        base = machine_id_from_name(display, taken=())
    taken = {machine.name.lower() for machine in doc.machines}
    candidate = base
    suffix = 2
    while candidate.lower() in taken:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate
