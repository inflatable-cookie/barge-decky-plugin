"""Shared test doubles for the Barge plugin suite."""

from __future__ import annotations

from typing import Dict, List, Optional

from barge_plugin import mdns
from barge_plugin.errors import BargeError
from barge_plugin.transport import StdlibHttpTransport


class FakeBrowser:
    """Discovery seam returning whatever the test says is on the LAN."""

    def __init__(self) -> None:
        self.machines: List[mdns.DiscoveredMachine] = []
        self.calls = 0
        self.error: Optional[BargeError] = None

    def browse(self, timeout_s: float = mdns.DEFAULT_BROWSE_TIMEOUT_S) -> List[mdns.DiscoveredMachine]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.machines)


class NullTransport:
    """Refuses every request, so a service under test never probes a real daemon."""

    def request(
        self,
        method: str,
        host: str,
        port: int,
        path: str,
        headers: Dict[str, str],
        body: bytes,
        timeout_s: float,
    ) -> object:
        raise BargeError("unreachable", f"no daemon at {host}:{port} in this test")


class ShortTimeoutTransport:
    """Real loopback HTTP with a short cap, so an unreachable host fails a test fast."""

    def __init__(self, cap_s: float = 0.5) -> None:
        self._inner = StdlibHttpTransport()
        self._cap_s = cap_s

    def request(
        self,
        method: str,
        host: str,
        port: int,
        path: str,
        headers: Dict[str, str],
        body: bytes,
        timeout_s: float,
    ) -> object:
        return self._inner.request(
            method, host, port, path, headers, body, min(timeout_s, self._cap_s)
        )
