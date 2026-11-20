"""mDNS (`_barged._tcp`) discovery without the system resolver.

`barged` advertises DNS-SD over mDNS on its LAN address; clients browse it
themselves because SteamOS does not resolve `.local` names. This module is a
port of the small browse-and-resolve subset in
`crates/barge-machine/src/discover.rs`: send a PTR query with the
unicast-response bit, then parse the PTR, SRV, TXT and A/AAAA answers (and
follow up for missing pieces) into resolved machines.

Only the wire subset DNS-SD needs is implemented, so the plugin stays
dependency-free.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from . import protocol
from .errors import BargeError

SERVICE_TYPE = "_barged._tcp.local."
MDNS_MULTICAST: Tuple[str, int] = ("224.0.0.251", 5353)
DEFAULT_BROWSE_TIMEOUT_S = 1.5

TYPE_A = 1
TYPE_PTR = 12
TYPE_TXT = 16
TYPE_AAAA = 28
TYPE_SRV = 33
CLASS_IN_QU = 0x8001

_POLL_SLICE_S = 0.2
_MAX_NAME_STEPS = 64
_MAX_PACKET_BYTES = 9000


@dataclass(frozen=True)
class DiscoveredMachine:
    """One `barged` instance found on the local network."""

    id: str
    name: str
    mode: str
    protocol_version: str
    address: str
    port: int
    hostname: str = ""
    mac: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "mode": self.mode,
            "protocol_version": self.protocol_version,
            "address": self.address,
            "port": self.port,
        }


class MdnsBrowser(Protocol):
    """Browse seam: tests substitute a fake, production browses the multicast group."""

    def browse(self, timeout_s: float = DEFAULT_BROWSE_TIMEOUT_S) -> List[DiscoveredMachine]:
        """Return every barged instance heard within `timeout_s`."""


class UdpMdnsBrowser:
    """Real mDNS browser sending unicast-response queries over UDP."""

    def __init__(self, target: Tuple[str, int] = MDNS_MULTICAST) -> None:
        self._target = target

    def browse(self, timeout_s: float = DEFAULT_BROWSE_TIMEOUT_S) -> List[DiscoveredMachine]:
        return browse_at(self._target, timeout_s)


@dataclass(frozen=True)
class DnsRecord:
    """One parsed resource record."""

    name: str
    kind: str
    target: str = ""
    port: int = 0
    txt: Tuple[Tuple[str, str], ...] = ()
    address: str = ""


def browse_at(
    target: Tuple[str, int],
    timeout_s: float,
    socket_factory: Optional[Callable[[], socket.socket]] = None,
) -> List[DiscoveredMachine]:
    """Browse `target`, returning the machines heard before `timeout_s` elapses."""
    if timeout_s <= 0:
        return []
    factory = socket_factory if socket_factory is not None else _default_socket
    udp = factory()
    try:
        udp.bind(("", 0))
        _send(udp, target, encode_query([(SERVICE_TYPE, TYPE_PTR)]))
        return _collect(udp, target, timeout_s)
    finally:
        try:
            udp.close()
        except OSError:
            pass


def _default_socket() -> socket.socket:
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def _send(udp: socket.socket, target: Tuple[str, int], packet: bytes) -> None:
    try:
        udp.sendto(packet, target)
    except OSError as exc:
        raise BargeError("io", f"cannot send mDNS query: {exc}") from exc


def _collect(
    udp: socket.socket,
    target: Tuple[str, int],
    timeout_s: float,
) -> List[DiscoveredMachine]:
    collector = Collector()
    queried_instances: set = set()
    queried_hosts: set = set()
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            udp.settimeout(min(remaining, _POLL_SLICE_S))
            packet, peer = udp.recvfrom(_MAX_PACKET_BYTES)
        except socket.timeout:
            continue
        except OSError as exc:
            raise BargeError("io", f"mDNS receive failed: {exc}") from exc
        collector.ingest(packet, str(peer[0]))
        follow_ups = collector.missing_queries(queried_instances, queried_hosts)
        if follow_ups:
            _send(udp, target, encode_query(follow_ups))
    return collector.finish()


def encode_query(questions: Sequence[Tuple[str, int]]) -> bytes:
    """Encode a DNS query with the unicast-response bit set on every question."""
    out = bytearray()
    out += (0).to_bytes(2, "big")  # transaction ID
    out += (0).to_bytes(2, "big")  # standard query flags
    out += len(questions).to_bytes(2, "big")
    out += (0).to_bytes(2, "big")  # ANCOUNT
    out += (0).to_bytes(2, "big")  # NSCOUNT
    out += (0).to_bytes(2, "big")  # ARCOUNT
    for name, qtype in questions:
        _encode_name(name, out)
        out += qtype.to_bytes(2, "big")
        out += CLASS_IN_QU.to_bytes(2, "big")
    return bytes(out)


def _encode_name(name: str, out: bytearray) -> None:
    for label in name.rstrip(".").split("."):
        if not label:
            continue
        encoded = label.encode("utf-8")[:63]
        out.append(len(encoded))
        out += encoded
    out.append(0)


def _read_name(packet: bytes, start: int) -> Tuple[str, int]:
    labels: List[str] = []
    pos = start
    end = start
    jumped = False
    steps = 0
    while True:
        if pos >= len(packet) or steps > _MAX_NAME_STEPS:
            raise ValueError("malformed DNS name")
        steps += 1
        length = packet[pos]
        if length == 0:
            if not jumped:
                end = pos + 1
            break
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(packet):
                raise ValueError("truncated DNS name pointer")
            pointer = ((length & 0x3F) << 8) | packet[pos + 1]
            if not jumped:
                end = pos + 2
                jumped = True
            pos = pointer
            continue
        if length & 0xC0 != 0:
            raise ValueError("unsupported DNS label type")
        label_end = pos + 1 + length
        if label_end > len(packet):
            raise ValueError("truncated DNS label")
        labels.append(packet[pos + 1 : label_end].decode("utf-8", errors="replace"))
        pos = label_end
        if not jumped:
            end = pos
    return ".".join(labels), end


def _parse_txt(rdata: bytes) -> Tuple[Tuple[str, str], ...]:
    pairs: List[Tuple[str, str]] = []
    pos = 0
    while pos < len(rdata):
        length = rdata[pos]
        pos += 1
        if pos + length > len(rdata):
            break
        entry = rdata[pos : pos + length].decode("utf-8", errors="replace")
        key, separator, value = entry.partition("=")
        if separator:
            pairs.append((key, value))
        pos += length
    return tuple(pairs)


def parse_records(packet: bytes) -> List[DnsRecord]:
    """Parse a DNS response packet's answer, authority and additional records."""
    if len(packet) < 12:
        raise ValueError("truncated DNS packet")
    flags = int.from_bytes(packet[2:4], "big")
    if flags & 0x8000 == 0:
        return []  # queries are not answers
    counts = [int.from_bytes(packet[offset : offset + 2], "big") for offset in (4, 6, 8, 10)]
    qdcount, ancount, nscount, arcount = counts

    pos = 12
    for _ in range(qdcount):
        _, pos = _read_name(packet, pos)
        pos += 4
        if pos > len(packet):
            raise ValueError("truncated DNS question")

    records: List[DnsRecord] = []
    for _ in range(ancount + nscount + arcount):
        if pos >= len(packet):
            break
        name, pos = _read_name(packet, pos)
        if pos + 10 > len(packet):
            raise ValueError("truncated DNS record header")
        rtype = int.from_bytes(packet[pos : pos + 2], "big")
        rdlength = int.from_bytes(packet[pos + 8 : pos + 10], "big")
        rdata_start = pos + 10
        rdata_end = rdata_start + rdlength
        if rdata_end > len(packet):
            raise ValueError("truncated DNS record data")
        records.append(_parse_rdata(packet, name, rtype, rdata_start, rdata_end))
        pos = rdata_end
    return records


def _parse_rdata(packet: bytes, name: str, rtype: int, start: int, end: int) -> DnsRecord:
    if rtype == TYPE_PTR:
        target, _ = _read_name(packet, start)
        return DnsRecord(name=name, kind="ptr", target=target)
    if rtype == TYPE_SRV and end - start >= 6:
        port = int.from_bytes(packet[start + 4 : start + 6], "big")
        target, _ = _read_name(packet, start + 6)
        return DnsRecord(name=name, kind="srv", target=target, port=port)
    if rtype == TYPE_TXT:
        return DnsRecord(name=name, kind="txt", txt=_parse_txt(packet[start:end]))
    if rtype == TYPE_A and end - start == 4:
        return DnsRecord(name=name, kind="a", address=".".join(str(b) for b in packet[start:end]))
    if rtype == TYPE_AAAA and end - start == 16:
        return DnsRecord(name=name, kind="aaaa", address=_format_ipv6(packet[start:end]))
    return DnsRecord(name=name, kind="other")


def _format_ipv6(raw: bytes) -> str:
    return socket.inet_ntop(socket.AF_INET6, raw)


@dataclass
class _ServiceParts:
    port: Optional[int] = None
    target: Optional[str] = None
    txt: Dict[str, str] = field(default_factory=dict)
    source_ip: Optional[str] = None


class Collector:
    """Accumulates the records heard for `_barged._tcp` into resolved machines."""

    def __init__(self) -> None:
        self._instances: Dict[str, _ServiceParts] = {}
        self._hosts: Dict[str, List[str]] = {}

    def ingest(self, packet: bytes, peer_ip: str) -> None:
        try:
            records = parse_records(packet)
        except (ValueError, OSError):
            return
        for record in records:
            lower = record.name.lower()
            if record.kind == "ptr" and lower == SERVICE_TYPE.rstrip(".").lower():
                entry = self._instance(record.target)
                entry.source_ip = entry.source_ip or peer_ip
            elif record.kind == "srv" and _is_barged_instance(lower):
                entry = self._instance(record.name)
                entry.port = record.port
                entry.target = record.target
                entry.source_ip = entry.source_ip or peer_ip
            elif record.kind == "txt" and _is_barged_instance(lower):
                entry = self._instance(record.name)
                for key, value in record.txt:
                    entry.txt[key] = value
                entry.source_ip = entry.source_ip or peer_ip
            elif record.kind in ("a", "aaaa") and record.address:
                self._hosts.setdefault(lower, []).append(record.address)

    def missing_queries(self, queried_instances: set, queried_hosts: set) -> List[Tuple[str, int]]:
        """Follow-up questions for instances or hosts whose records have not arrived."""
        questions: List[Tuple[str, int]] = []
        for instance, parts in self._instances.items():
            if (parts.port is None or not parts.txt) and instance not in queried_instances:
                queried_instances.add(instance)
                questions.append((instance, TYPE_SRV))
                questions.append((instance, TYPE_TXT))
            if parts.target:
                target_lower = parts.target.lower()
                if target_lower not in self._hosts and target_lower not in queried_hosts:
                    queried_hosts.add(target_lower)
                    questions.append((parts.target, TYPE_A))
        return questions

    def finish(self) -> List[DiscoveredMachine]:
        by_id: Dict[str, DiscoveredMachine] = {}
        for instance, parts in self._instances.items():
            if parts.port is None:
                continue
            machine_id = parts.txt.get("id", "")
            if not protocol.is_machine_id(machine_id):
                continue
            address = self._resolve(parts)
            if address is None:
                continue
            name = parts.txt.get("name", "").strip() or instance.split(".")[0] or "barged"
            by_id[machine_id] = DiscoveredMachine(
                id=machine_id,
                name=name,
                mode=parts.txt.get("mode", "lan"),
                protocol_version=parts.txt.get("version", "1"),
                address=address,
                port=parts.port,
                hostname=parts.target or "",
            )
        return sorted(by_id.values(), key=lambda machine: (machine.name.lower(), machine.id))

    def _instance(self, name: str) -> _ServiceParts:
        return self._instances.setdefault(name, _ServiceParts())

    def _resolve(self, parts: _ServiceParts) -> Optional[str]:
        if parts.target:
            addresses = self._hosts.get(parts.target.lower(), [])
            for address in addresses:
                if ":" not in address:
                    return address
            if addresses:
                return addresses[0]
        return parts.source_ip


def _is_barged_instance(name_lower: str) -> bool:
    return name_lower.endswith("._barged._tcp.local")
