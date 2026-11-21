"""DNS packet builders for mDNS tests."""

from __future__ import annotations

from typing import Iterable, Sequence, Tuple

SERVICE = "_barged._tcp.local."


def encode_name(name: str) -> bytes:
    out = bytearray()
    for label in name.rstrip(".").split("."):
        if not label:
            continue
        encoded = label.encode("utf-8")
        out.append(len(encoded))
        out += encoded
    out.append(0)
    return bytes(out)


def _record(name: str, rtype: int, rdata: bytes, ttl: int = 120) -> bytes:
    return (
        encode_name(name)
        + rtype.to_bytes(2, "big")
        + (1).to_bytes(2, "big")
        + ttl.to_bytes(4, "big")
        + len(rdata).to_bytes(2, "big")
        + rdata
    )


def ptr_record(service: str, instance: str) -> bytes:
    return _record(service, 12, encode_name(instance))


def srv_record(instance: str, port: int, target: str) -> bytes:
    rdata = (0).to_bytes(2, "big") + (0).to_bytes(2, "big") + port.to_bytes(2, "big")
    return _record(instance, 33, rdata + encode_name(target))


def txt_record(instance: str, entries: Sequence[str]) -> bytes:
    rdata = bytearray()
    for entry in entries:
        encoded = entry.encode("utf-8")
        rdata.append(len(encoded))
        rdata += encoded
    return _record(instance, 16, bytes(rdata))


def a_record(name: str, address: str) -> bytes:
    return _record(name, 1, bytes(int(part) for part in address.split(".")))


def aaaa_record(name: str, packed: bytes) -> bytes:
    return _record(name, 28, packed)


def dns_response(answers: Iterable[bytes]) -> bytes:
    records = list(answers)
    header = (
        (0).to_bytes(2, "big")
        + (0x8400).to_bytes(2, "big")  # response, authoritative
        + (0).to_bytes(2, "big")
        + len(records).to_bytes(2, "big")
        + (0).to_bytes(2, "big")
        + (0).to_bytes(2, "big")
    )
    return header + b"".join(records)


def machine_response(
    machine_id: str,
    name: str,
    address: str,
    port: int,
    *,
    instance: str = "pc",
    host: str = "pc.local.",
) -> bytes:
    """A complete advertisement: PTR + SRV + TXT + A in one response."""
    instance_name = f"{instance}.{SERVICE}"
    return dns_response(
        [
            ptr_record(SERVICE, instance_name),
            srv_record(instance_name, port, host),
            txt_record(
                instance_name,
                [
                    f"id={machine_id}",
                    f"name={name}",
                    "mode=lan",
                    "version=1",
                ],
            ),
            a_record(host, address),
        ]
    )


def compression_packet() -> Tuple[bytes, int]:
    """A response whose second record's name is a pointer to the first name.

    Returns the packet and the offset the pointer targets.
    """
    first_name = encode_name("pc.local.")
    name_offset = 12
    pointer = bytes([0xC0 | (name_offset >> 8), name_offset & 0xFF])
    header = (
        (0).to_bytes(2, "big")
        + (0x8400).to_bytes(2, "big")
        + (0).to_bytes(2, "big")
        + (2).to_bytes(2, "big")
        + (0).to_bytes(2, "big")
        + (0).to_bytes(2, "big")
    )
    a_rdata = bytes([192, 168, 1, 50])
    answer = (
        (1).to_bytes(2, "big")
        + (1).to_bytes(2, "big")
        + (120).to_bytes(4, "big")
        + len(a_rdata).to_bytes(2, "big")
        + a_rdata
    )
    txt_rdata = bytearray()
    for entry in ("id=" + "cd" * 16, "name=PC"):
        encoded = entry.encode("utf-8")
        txt_rdata.append(len(encoded))
        txt_rdata += encoded
    txt = (
        pointer
        + (16).to_bytes(2, "big")
        + (1).to_bytes(2, "big")
        + (120).to_bytes(4, "big")
        + len(txt_rdata).to_bytes(2, "big")
        + bytes(txt_rdata)
    )
    return header + first_name + answer + txt, name_offset
