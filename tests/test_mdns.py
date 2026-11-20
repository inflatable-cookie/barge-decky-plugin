"""mDNS browsing and answer parsing, matching barge-machine's discover module."""

from __future__ import annotations

import socket
import threading
from typing import List

import mdns_packets
import pytest

from barge_plugin import mdns

MACHINE_ID = "ab" * 16
INSTANCE = "bazzite._barged._tcp.local"


def test_encode_query_sets_the_unicast_response_bit() -> None:
    expected = (
        b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        b"\x07_barged\x04_tcp\x05local\x00"
        b"\x00\x0c"
        b"\x80\x01"
    )
    assert mdns.encode_query([(mdns.SERVICE_TYPE, mdns.TYPE_PTR)]) == expected


def test_parse_records_reads_ptr_srv_txt_and_a() -> None:
    packet = mdns_packets.machine_response(MACHINE_ID, "Bazzite", "192.168.1.50", 9777)

    records = mdns.parse_records(packet)
    kinds = [record.kind for record in records]

    assert kinds == ["ptr", "srv", "txt", "a"]
    assert records[0].target.startswith("bazzite._barged._tcp.local")
    assert records[1].port == 9777
    assert records[1].target.startswith("bazzite.local")
    assert dict(records[2].txt)["id"] == MACHINE_ID
    assert records[3].address == "192.168.1.50"


def test_parse_records_follows_compression_pointers() -> None:
    packet, _offset = mdns_packets.compression_packet()

    records = mdns.parse_records(packet)

    assert [record.name for record in records] == [
        "bazzite.local",
        "bazzite.local",
    ]
    assert records[1].kind == "txt"
    assert dict(records[1].txt)["name"] == "Bazzite"


def test_parse_records_declines_a_query_or_truncated_packet() -> None:
    with pytest.raises(ValueError):
        mdns.parse_records(b"\x00\x00\x00\x00\x00\x01")

    query = (
        b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        + mdns_packets.encode_name(mdns.SERVICE_TYPE)
        + b"\x00\x0c\x80\x01"
    )
    assert mdns.parse_records(query) == []

    # A response that promises a record it does not carry yields no records,
    # matching barge-machine's parser, which stops at the end of the packet.
    assert mdns.parse_records(b"\x00\x00\x84\x00\x00\x00\x00\x01\x00\x00\x00\x00") == []


def test_collector_resolves_a_machine_and_prefers_ipv4() -> None:
    instance = INSTANCE
    packet = mdns_packets.dns_response(
        [
            mdns_packets.ptr_record(mdns.SERVICE_TYPE, instance),
            mdns_packets.srv_record(instance, 9777, "bazzite.local."),
            mdns_packets.txt_record(
                instance,
                [f"id={MACHINE_ID}", "name=Bazzite PC", "mode=lan", "version=1"],
            ),
            mdns_packets.aaaa_record("bazzite.local.", b"\xfe\x80" + b"\x00" * 14),
            mdns_packets.a_record("bazzite.local.", "192.168.1.50"),
        ]
    )

    collector = mdns.Collector()
    collector.ingest(packet, "192.168.1.50")
    found = collector.finish()

    assert found == [
        mdns.DiscoveredMachine(
            id=MACHINE_ID,
            name="Bazzite PC",
            mode="lan",
            protocol_version="1",
            address="192.168.1.50",
            port=9777,
        )
    ]


def test_collector_falls_back_to_the_source_address_and_needs_a_valid_id() -> None:
    instance = INSTANCE
    no_address = mdns_packets.dns_response(
        [
            mdns_packets.ptr_record(mdns.SERVICE_TYPE, instance),
            mdns_packets.srv_record(instance, 9777, "bazzite.local."),
            mdns_packets.txt_record(instance, [f"id={MACHINE_ID}", "name=Bazzite"]),
        ]
    )
    collector = mdns.Collector()
    collector.ingest(no_address, "10.0.0.7")
    assert collector.finish()[0].address == "10.0.0.7"

    bad_id = mdns_packets.dns_response(
        [
            mdns_packets.ptr_record(mdns.SERVICE_TYPE, instance),
            mdns_packets.srv_record(instance, 9777, "bazzite.local."),
            mdns_packets.txt_record(instance, ["id=NOT-HEX", "name=Bazzite"]),
            mdns_packets.a_record("bazzite.local.", "10.0.0.7"),
        ]
    )
    empty = mdns.Collector()
    empty.ingest(bad_id, "10.0.0.7")
    assert empty.finish() == []


def test_collector_ignores_malformed_packets_and_other_services() -> None:
    collector = mdns.Collector()
    collector.ingest(b"\x00\x01", "10.0.0.7")
    collector.ingest(
        mdns_packets.dns_response(
            [mdns_packets.ptr_record("_other._tcp.local.", "x._other._tcp.local.")]
        ),
        "10.0.0.7",
    )
    assert collector.finish() == []


def test_collector_asks_for_missing_srv_txt_and_address() -> None:
    instance = INSTANCE
    collector = mdns.Collector()
    collector.ingest(
        mdns_packets.dns_response(
            [mdns_packets.ptr_record(mdns.SERVICE_TYPE, instance)]
        ),
        "10.0.0.7",
    )

    queried_instances: set = set()
    queried_hosts: set = set()
    first = collector.missing_queries(queried_instances, queried_hosts)
    assert (INSTANCE, mdns.TYPE_SRV) in first
    assert (INSTANCE, mdns.TYPE_TXT) in first

    collector.ingest(
        mdns_packets.dns_response(
            [mdns_packets.srv_record(instance, 9777, "bazzite.local.")]
        ),
        "10.0.0.7",
    )
    second = collector.missing_queries(queried_instances, queried_hosts)
    assert ("bazzite.local", mdns.TYPE_A) in second
    assert collector.missing_queries(queried_instances, queried_hosts) == []


def test_browse_at_reads_answers_over_loopback() -> None:
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.bind(("127.0.0.1", 0))
    responder.settimeout(2.0)
    target = responder.getsockname()
    packet = mdns_packets.machine_response(MACHINE_ID, "Bazzite", "192.168.1.50", 9777)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                _data, source = responder.recvfrom(4096)
            except (socket.timeout, OSError):
                return
            try:
                responder.sendto(packet, source)
            except OSError:
                return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        found: List[mdns.DiscoveredMachine] = mdns.browse_at(target, 1.0)
    finally:
        stop.set()
        responder.close()
        thread.join(timeout=3)

    assert [machine.id for machine in found] == [MACHINE_ID]
    assert found[0].address == "192.168.1.50"
    assert found[0].port == 9777


def test_browse_at_returns_nothing_for_a_zero_timeout() -> None:
    assert mdns.browse_at(("127.0.0.1", 9), 0) == []
