"""Matching discovery answers to configured machines, and display names."""

from __future__ import annotations

from barge_plugin import discovery, mdns
from barge_plugin.config import MachineConfig, MachinesDocument

MACHINE_A = "aa" * 16
MACHINE_B = "bb" * 16


def machine(
    machine_id: str,
    *,
    daemon_machine_id: str = "",
    host: str = "192.168.1.10",
    port: int = 9777,
) -> MachineConfig:
    return MachineConfig.validated(
        machine_id=machine_id,
        name=machine_id,
        host=host,
        daemon_port=port,
        mac="00:11:22:33:44:ff",
        daemon_machine_id=daemon_machine_id,
    )


def found(
    daemon_id: str,
    *,
    address: str = "192.168.1.10",
    port: int = 9777,
    name: str = "Bazzite",
) -> mdns.DiscoveredMachine:
    return mdns.DiscoveredMachine(
        id=daemon_id,
        name=name,
        mode="lan",
        protocol_version="1",
        address=address,
        port=port,
    )


def test_sync_moves_a_machine_by_its_daemon_id_and_adopts_one_by_address() -> None:
    doc = MachinesDocument(
        machines=(
            machine("bazzite", daemon_machine_id=MACHINE_A),
            machine("laptop", host="192.168.1.20", port=9778),
        ),
        selected_id="laptop",
    )

    updated, moved = discovery.sync_addresses(
        doc,
        [found(MACHINE_A, address="192.168.1.77"), found(MACHINE_B, address="192.168.1.20", port=9778)],
    )

    assert moved == ["bazzite", "laptop"]
    bazzite = updated.get("bazzite")
    assert bazzite is not None
    assert (bazzite.host, bazzite.daemon_port) == ("192.168.1.77", 9777)
    laptop = updated.get("laptop")
    assert laptop is not None
    assert laptop.daemon_machine_id == MACHINE_B
    assert updated.selected_id == "laptop"


def test_sync_leaves_an_unchanged_document_alone() -> None:
    doc = MachinesDocument(machines=(machine("bazzite", daemon_machine_id=MACHINE_A),))
    same, moved = discovery.sync_addresses(doc, [found(MACHINE_A)])
    assert moved == []
    assert same is doc


def test_match_machine_prefers_the_daemon_id_over_the_address() -> None:
    doc = MachinesDocument(
        machines=(
            machine("old", daemon_machine_id=MACHINE_A),
            machine("imposter", host="192.168.1.50"),
        )
    )
    matched = discovery.match_machine(doc, found(MACHINE_A, address="192.168.1.50"))
    assert matched is not None
    assert matched.id == "old"


def test_unique_display_name_makes_a_daemon_name_acceptable() -> None:
    doc = MachinesDocument(machines=(machine("bazzite"),))
    assert discovery.unique_display_name(doc, "Living room PC") == "living-room-pc"
    assert discovery.unique_display_name(doc, "bazzite") == "bazzite-2"
    assert discovery.unique_display_name(doc, "Bazzite") == "Bazzite-2"
