"""Matching discovered `barged` instances to configured machines.

Pure helpers: which configured machine a discovery answer belongs to, which
machines moved to a new address, and a display name the settings format accepts.
A paired machine is matched by its daemon machine ID, so a DHCP renewal updates
the stored address instead of creating a second machine.
"""

from __future__ import annotations

from dataclasses import replace
from typing import List, Optional, Sequence, Tuple

from . import mdns
from .config import MachinesDocument, MachineConfig, machine_id_from_name, validate_name
from .errors import BargeError


def match_machine(
    doc: MachinesDocument,
    found: mdns.DiscoveredMachine,
) -> Optional[MachineConfig]:
    """The configured machine a discovery answer belongs to, by ID then by address."""
    for machine in doc.machines:
        if machine.daemon_machine_id and machine.daemon_machine_id == found.id:
            return machine
    for machine in doc.machines:
        if machine.host == found.address and machine.daemon_port == found.port:
            return machine
    return None


def sync_addresses(
    doc: MachinesDocument,
    found_list: Sequence[mdns.DiscoveredMachine],
) -> Tuple[MachinesDocument, List[str]]:
    """Return the document with moved machines updated, and the ids that moved."""
    updated: List[MachineConfig] = []
    changed: List[str] = []
    for machine in doc.machines:
        found = _found_for(machine, found_list)
        if found is None or (
            machine.daemon_machine_id == found.id
            and machine.host == found.address
            and machine.daemon_port == found.port
        ):
            updated.append(machine)
            continue
        updated.append(
            replace(
                machine,
                host=found.address,
                daemon_port=found.port,
                daemon_machine_id=found.id,
            )
        )
        changed.append(machine.id)
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


def _found_for(
    machine: MachineConfig,
    found_list: Sequence[mdns.DiscoveredMachine],
) -> Optional[mdns.DiscoveredMachine]:
    if machine.daemon_machine_id:
        for found in found_list:
            if found.id == machine.daemon_machine_id:
                return found
        return None
    for found in found_list:
        if found.address == machine.host and found.port == machine.daemon_port:
            return found
    return None
