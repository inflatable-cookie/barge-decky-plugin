"""Wake-on-LAN MAC parsing, magic packet construction, and UDP broadcast."""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional, Protocol

from .errors import BargeError

DEFAULT_BROADCAST = "255.255.255.255"
DEFAULT_WOL_PORT = 9
MAGIC_PACKET_LEN = 102


def parse_mac(value: str) -> bytes:
    """Parse colon, hyphen, dot, or compact 12-digit hex MAC addresses."""
    trimmed = value.strip()
    if len(trimmed) == 12 and all(ch in "0123456789abcdefABCDEF" for ch in trimmed):
        compact = trimmed
    elif (
        len(trimmed) == 17
        and trimmed[2] in (":", "-")
        and all(trimmed[i] == trimmed[2] for i in (2, 5, 8, 11, 14))
        and all(
            ch in "0123456789abcdefABCDEF"
            for idx, ch in enumerate(trimmed)
            if idx not in (2, 5, 8, 11, 14)
        )
    ):
        compact = trimmed.replace(trimmed[2], "")
    elif (
        len(trimmed) == 14
        and trimmed[4] == "."
        and trimmed[9] == "."
        and all(
            ch in "0123456789abcdefABCDEF"
            for idx, ch in enumerate(trimmed)
            if idx not in (4, 9)
        )
    ):
        compact = trimmed.replace(".", "")
    else:
        raise BargeError("usage", f"invalid MAC address {value!r}")

    try:
        return bytes.fromhex(compact)
    except ValueError as exc:
        raise BargeError("usage", f"invalid MAC address {value!r}") from exc


def format_mac(mac: bytes) -> str:
    """Format 6 MAC bytes in canonical lowercase colon-separated form."""
    if len(mac) != 6:
        raise BargeError("usage", "MAC address must be 6 bytes")
    return ":".join(f"{b:02x}" for b in mac)


def magic_packet(mac: bytes) -> bytes:
    """Build the 102-byte Wake-on-LAN magic packet (6x 0xff + 16x MAC)."""
    if len(mac) != 6:
        raise BargeError("usage", "MAC address must be 6 bytes")
    return b"\xff" * 6 + mac * 16


class DatagramSender(Protocol):
    """Injectable UDP sender seam for Wake-on-LAN tests."""

    def send_to(self, data: bytes, host: str, port: int) -> int:
        """Send one UDP datagram and return the number of bytes sent."""


class UdpBroadcastSender:
    """Real UDP broadcast sender using an unprivileged IPv4 socket."""

    def send_to(self, data: bytes, host: str, port: int) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            return sock.sendto(data, (host, port))


def validate_broadcast(address: str) -> str:
    """Validate an IPv4 broadcast address string."""
    trimmed = address.strip()
    try:
        parsed = ipaddress.IPv4Address(trimmed)
    except ValueError as exc:
        raise BargeError("usage", f"broadcast address {address!r} must be IPv4") from exc
    return str(parsed)


def wake(
    mac: str,
    broadcast: str = DEFAULT_BROADCAST,
    port: int = DEFAULT_WOL_PORT,
    sender: Optional[DatagramSender] = None,
) -> tuple[str, int, int]:
    """Send a Wake-on-LAN magic packet and return (broadcast, port, bytes_sent)."""
    if not (1 <= port <= 65535):
        raise BargeError("usage", "Wake-on-LAN port must be 1-65535")
    dest_ip = validate_broadcast(broadcast)
    packet = magic_packet(parse_mac(mac))
    active_sender: DatagramSender = sender if sender is not None else UdpBroadcastSender()
    try:
        sent = active_sender.send_to(packet, dest_ip, port)
    except OSError as exc:
        raise BargeError(
            "io",
            f"cannot send Wake-on-LAN packet to {dest_ip}:{port}: {exc}",
        ) from exc
    if sent != MAGIC_PACKET_LEN:
        raise BargeError(
            "io",
            f"sent {sent} of {MAGIC_PACKET_LEN} Wake-on-LAN bytes to {dest_ip}:{port}",
        )
    return dest_ip, port, sent
