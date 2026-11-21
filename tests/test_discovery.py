"""Matching discovery answers to configured machines, and display names."""

from __future__ import annotations

import pytest

from barge_plugin import discovery, mdns
from barge_plugin.config import MachineConfig, MachinesDocument

MACHINE_A = "aa" * 16
MACHINE_B = "bb" * 16
MATCHING_MAC = "00:11:22:33:44:ff"


def machine(
    machine_id: str,
    *,
    daemon_machine_id: str = "",
    host: str = "192.0.2.10",
    port: int = 9777,
    mac: str = "",
) -> MachineConfig:
    return MachineConfig.validated(
        machine_id=machine_id,
        name=machine_id,
        host=host,
        daemon_port=port,
        mac=mac,
        daemon_machine_id=daemon_machine_id,
    )


def found(
    daemon_id: str,
    *,
    address: str = "192.0.2.10",
    port: int = 9777,
    hostname: str = "",
    mac: str = "",
) -> mdns.DiscoveredMachine:
    return mdns.DiscoveredMachine(
        id=daemon_id,
        name="Living Room PC",
        mode="lan",
        protocol_version="1",
        address=address,
        port=port,
        hostname=hostname,
        mac=mac,
    )


@pytest.mark.parametrize("strongest", ["hostname", "address", "mac", "id"])
def test_matching_and_sync_use_the_same_priority(strongest: str) -> None:
    candidates = {
        "hostname": machine("hostname", host="PC.Local."),
        "address": machine("address", host="192.0.2.50"),
        "mac": machine("mac", host="192.0.2.51", mac=MATCHING_MAC),
        "id": machine("id", daemon_machine_id=MACHINE_A),
    }
    machines = tuple(candidates.values())[: list(candidates).index(strongest) + 1]
    doc = MachinesDocument(machines=machines, selected_id="hostname")
    advertisement = found(
        MACHINE_A, address="192.0.2.50", port=9788, hostname="pc.local", mac=MATCHING_MAC,
    )

    assert discovery.match_machine(doc, advertisement) == candidates[strongest]
    updated, moved = discovery.sync_addresses(doc, [advertisement])

    assert moved == [strongest]
    matched = updated.get(strongest)
    assert matched is not None
    assert (matched.host, matched.daemon_port) == (advertisement.address, advertisement.port)
    assert matched.daemon_machine_id == candidates[strongest].daemon_machine_id
    assert tuple(m for m in updated.machines if m.id != strongest) == machines[:-1]
    assert updated.selected_id == "hostname"


def test_sync_leaves_an_unchanged_document_alone() -> None:
    doc = MachinesDocument(machines=(machine("pc", daemon_machine_id=MACHINE_A),))
    same, moved = discovery.sync_addresses(doc, [found(MACHINE_A)])
    assert moved == []
    assert same is doc


def test_conflicting_id_rules_out_weaker_matches() -> None:
    saved = machine("pc", daemon_machine_id=MACHINE_A, mac=MATCHING_MAC)
    doc = MachinesDocument(machines=(saved,))
    advertisement = found(MACHINE_B, mac=MATCHING_MAC, port=9788)

    assert discovery.match_machine(doc, advertisement) is None
    assert discovery.sync_addresses(doc, [advertisement]) == (doc, [])


def test_sync_uses_strongest_advertisement_and_leaves_ambiguous_matches_alone() -> None:
    doc = MachinesDocument(machines=(machine("pc", mac=MATCHING_MAC),))
    by_address = found(MACHINE_A, port=9788)
    by_mac = found(MACHINE_B, address="192.0.2.50", mac=MATCHING_MAC)

    for advertisements in ([by_address, by_mac], [by_mac, by_address]):
        updated, moved = discovery.sync_addresses(doc, advertisements)
        assert moved == ["pc"]
        assert updated.machines[0].host == by_mac.address

    other_mac_match = found(MACHINE_A, address="192.0.2.51", mac=MATCHING_MAC)
    assert discovery.sync_addresses(doc, [by_mac, other_mac_match]) == (doc, [])


def test_unique_display_name_makes_a_daemon_name_acceptable() -> None:
    doc = MachinesDocument(machines=(machine("pc"),))
    assert discovery.unique_display_name(doc, "Living room PC") == "living-room-pc"
    assert discovery.unique_display_name(doc, "pc") == "pc-2"
    assert discovery.unique_display_name(doc, "PC") == "PC-2"
