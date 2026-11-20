"""Approving other clients on this PC: the `barged` answering on localhost.

barged trusts an unsigned caller on loopback for `GET /v1/pair/pending` and the
approve/deny routes, so the plugin on the PC can answer a request without being
paired as a client itself. Candidate ports come from discovery and settings
first (the PC advertises its own port), then the default.

Failures to find a local daemon are normal on the Deck, so they are silent; a
request that cannot be answered is reported to the caller.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from . import mdns, protocol
from .config import ConfigStore, MachinesDocument
from .errors import BargeError
from .state import now_ms
from .transport import DaemonClient, HttpTransport

LOOPBACK_HOST = "127.0.0.1"
LOCAL_PROBE_INTERVAL_MS = 10_000
LOCAL_PROBE_TIMEOUT_S = 1.0


@dataclass(frozen=True)
class LocalDaemon:
    """The `barged` answering on localhost, treated as this PC."""

    port: int
    pending: Sequence[protocol.PendingPairRequest]


class LocalApprovals:
    """Finds this PC's barged and tracks its pending pair requests."""

    def __init__(
        self,
        store: ConfigStore,
        transport: Optional[HttpTransport],
        *,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self._store = store
        self._transport = transport
        self._clock = clock
        self._local: Optional[LocalDaemon] = None
        self._found: Sequence[mdns.DiscoveredMachine] = ()
        self._next_probe_ms = 0

    @property
    def local(self) -> Optional[LocalDaemon]:
        return self._local

    def has_pending(self) -> bool:
        return self._local is not None and bool(self._local.pending)

    def invalidate(self) -> None:
        """Probe again on the next poll (new discovery answers, or an explicit refresh)."""
        self._next_probe_ms = 0

    def view(self) -> Dict[str, object]:
        """Frontend view of this PC and its requests."""
        local = self._local
        if local is None:
            return {"available": False, "port": 0, "pending": []}
        return {
            "available": True,
            "port": local.port,
            "pending": [
                {
                    "request_id": request.request_id,
                    "client_name": request.client_name,
                    "code": request.code,
                    "created_at_unix_ms": request.created_at_unix_ms,
                    "expires_at_unix_ms": request.expires_at_unix_ms,
                }
                for request in local.pending
            ],
        }

    async def poll(
        self,
        doc: MachinesDocument,
        found_list: Sequence[mdns.DiscoveredMachine],
    ) -> None:
        """Keep this PC's pending requests current, probing localhost when needed."""
        self._found = tuple(found_list)
        if self._local is None:
            if self._clock() < self._next_probe_ms:
                return
            self._local = await self._probe(self._candidate_ports(doc, found_list))
            if self._local is None:
                self._next_probe_ms = self._clock() + LOCAL_PROBE_INTERVAL_MS
                return
        await self._refresh()

    async def decide(self, request_id: str, approve: bool) -> str:
        """Approve or deny one request; returns the requester's name when approving."""
        protocol.validate_path_segment(request_id, "request_id")
        local = self._local if self._local is not None else await self._ensure()
        if local is None:
            raise BargeError(
                "unreachable",
                "no barged answered on localhost; approvals need this PC's daemon",
            )
        client = self._client(local.port)
        if approve:
            approved = await asyncio.to_thread(client.approve_pair, request_id)
            client_name = approved.client.name
        else:
            await asyncio.to_thread(client.deny_pair, request_id)
            client_name = ""
        await self._refresh()
        return client_name

    async def _ensure(self) -> Optional[LocalDaemon]:
        doc = self._store.load()
        return await self._probe(self._candidate_ports(doc, self._found))

    async def _probe(self, ports: Sequence[int]) -> Optional[LocalDaemon]:
        for port in ports:
            try:
                result = await asyncio.to_thread(self._client(port).pending_pairs)
            except BargeError:
                continue
            return LocalDaemon(port=port, pending=tuple(result.requests))
        return None

    async def _refresh(self) -> None:
        local = self._local
        if local is None:
            return
        try:
            result = await asyncio.to_thread(self._client(local.port).pending_pairs)
        except BargeError:
            # The daemon stopped answering on loopback; rediscover on a later poll.
            self._local = None
            self._next_probe_ms = self._clock() + LOCAL_PROBE_INTERVAL_MS
            return
        self._local = LocalDaemon(port=local.port, pending=tuple(result.requests))

    def _client(self, port: int) -> DaemonClient:
        return DaemonClient.for_lan_unauthenticated(
            LOOPBACK_HOST,
            port,
            transport=self._transport,
            timeout_s=LOCAL_PROBE_TIMEOUT_S,
        )

    @staticmethod
    def _candidate_ports(
        doc: MachinesDocument,
        found_list: Sequence[mdns.DiscoveredMachine],
    ) -> List[int]:
        """What discovery and settings know first, then barged's default port."""
        ports: List[int] = []
        for found in found_list:
            if found.port not in ports:
                ports.append(found.port)
        for machine in doc.machines:
            if machine.daemon_port not in ports:
                ports.append(machine.daemon_port)
        if protocol.DEFAULT_DAEMON_PORT not in ports:
            ports.append(protocol.DEFAULT_DAEMON_PORT)
        return ports
