"""Client-side pairing: the X25519 exchange, the approval poll, and saving the machine.

`PairFlow` holds one in-flight request for the whole two-minute window. Starting
it sends `POST /v1/pair` and derives the six-digit code; polling follows
`GET /v1/pair/<id>` and, once approved, stores this Deck's own key
(`keys/<machine-id>.key`, owner-only) and the machine's learned details before
handing the saved machine back to the service.

The derived key and the ephemeral secret live only in this object and are never
rendered by `repr` or returned to the frontend.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from typing import Any, Callable, Dict, Literal, Optional

from . import discovery, mdns, pairing, protocol, wol
from .config import ConfigStore, MachineConfig, machine_id_from_name, safe_optional_host
from .errors import BargeError
from .transport import DaemonClient, HttpTransport

PairingPhase = Literal["waiting", "approved", "denied", "expired", "failed"]

PairedCallback = Callable[[MachineConfig, str], None]


def default_client_name() -> str:
    """Name this plugin shows on the machine it pairs with ("Steam Deck wants to pair")."""
    try:
        hostname = socket.gethostname().strip()
    except OSError:
        hostname = ""
    lowered = hostname.lower()
    if not hostname or lowered in ("localhost", "localhost.localdomain"):
        return "Steam Deck"
    if lowered.startswith("steamdeck") or lowered == "deck":
        return "Steam Deck"
    return hostname


@dataclass
class PairingSession:
    """One in-flight pairing request. The derived key never leaves this object unwrapped."""

    discovered: mdns.DiscoveredMachine
    derived: pairing.DerivedPairing
    request_id: str
    expires_at_unix_ms: int
    status: PairingPhase = "waiting"
    error: str = ""
    machine_id: str = ""
    client_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Frontend view: the code is shown, the key and secret are not."""
        return {
            "status": self.status,
            "code": self.derived.code,
            "name": self.discovered.name,
            "address": self.discovered.address,
            "port": self.discovered.port,
            "daemon_machine_id": self.discovered.id,
            "request_id": self.request_id,
            "expires_at_unix_ms": self.expires_at_unix_ms,
            "machine_id": self.machine_id,
            "error": self.error,
        }


@dataclass(frozen=True)
class PairingUpdate:
    """The result of one approval poll."""

    status: PairingPhase
    error: str = ""


class PairFlow:
    """Runs one pairing request at a time against a discovered machine."""

    def __init__(
        self,
        store: ConfigStore,
        transport: Optional[HttpTransport],
        *,
        on_paired: PairedCallback,
    ) -> None:
        self._store = store
        self._transport = transport
        self._on_paired = on_paired
        self._session: Optional[PairingSession] = None

    @property
    def session(self) -> Optional[PairingSession]:
        return self._session

    def view(self) -> Optional[Dict[str, Any]]:
        return self._session.to_dict() if self._session is not None else None

    def is_waiting(self) -> bool:
        return self._session is not None and self._session.status == "waiting"

    def cancel(self) -> None:
        """Forget the request; barged lets it expire after two minutes."""
        self._session = None

    async def start(
        self,
        found: mdns.DiscoveredMachine,
        client_name: str = "",
    ) -> Dict[str, Any]:
        """Open a pairing request and return its frontend view, including the code."""
        if self.is_waiting():
            raise BargeError("usage", "a pairing request is already waiting")
        doc = self._store.load()
        existing = discovery.match_machine(doc, found)
        if existing is not None and self._store.is_paired(existing.id):
            raise BargeError("usage", f"{existing.name} is already paired")
        name = client_name.strip() or default_client_name()
        keys = pairing.PairingKeyPair.generate()
        client = DaemonClient.for_lan_unauthenticated(
            found.address, found.port, transport=self._transport
        )
        started = await asyncio.to_thread(client.start_pair, name, keys.public_key_hex())
        derived = keys.derive(pairing.parse_public_key(started.public_key), "client")
        self._session = PairingSession(
            discovered=found,
            derived=derived,
            request_id=started.request_id,
            expires_at_unix_ms=started.expires_at_unix_ms,
        )
        return self._session.to_dict()

    async def poll(self) -> PairingUpdate:
        """Check the request; an approval saves the machine before returning."""
        session = self._session
        if session is None:
            return PairingUpdate("failed", "no pairing request is open")
        if session.status != "waiting":
            return PairingUpdate(session.status, session.error)

        client = DaemonClient.for_lan_unauthenticated(
            session.discovered.address,
            session.discovered.port,
            transport=self._transport,
        )
        try:
            status = await asyncio.to_thread(client.pair_status, session.request_id)
        except BargeError as exc:
            # A single failed poll keeps the request open; barged still has it.
            session.error = exc.message
            return PairingUpdate("waiting", exc.message)

        session.error = ""
        if status.status == "approved" and status.client_id is not None:
            await self._complete(session, status.client_id)
        elif status.status == "approved":
            session.status = "failed"
            session.error = "barged approved the request without a client id"
        elif status.status in ("denied", "expired"):
            session.status = status.status
        return PairingUpdate(session.status, session.error)

    async def _complete(self, session: PairingSession, client_id: str) -> None:
        """Save the approved machine: key first, then the list; learn details from state."""
        key = session.derived.key
        state = await self._state_after_approval(session, client_id, key)
        try:
            doc = self._store.load()
        except BargeError as exc:
            session.status = "failed"
            session.error = exc.message
            return
        existing = discovery.match_machine(doc, session.discovered)
        local_id = (
            existing.id
            if existing is not None
            else machine_id_from_name(session.discovered.name, taken=doc.ids())
        )
        display_name = (
            existing.name
            if existing is not None
            else discovery.unique_display_name(doc, session.discovered.name)
        )
        try:
            tailscale_name = safe_optional_host(
                state.tailscale_name if state is not None else None,
                "tailscale name",
                existing.tailscale_name if existing is not None else "",
            )
            tailscale_ip = safe_optional_host(
                state.tailscale_ip if state is not None else None,
                "tailscale ip",
                existing.tailscale_ip if existing is not None else "",
            )
            last_path = (
                "lan"
                if state is not None
                else (existing.last_path if existing is not None else None)
            )
            config = MachineConfig.validated(
                machine_id=local_id,
                name=display_name,
                host=session.discovered.address,
                daemon_port=session.discovered.port,
                mac=state.mac if state is not None and state.mac else "",
                broadcast=(
                    state.broadcast
                    if state is not None and state.broadcast
                    else (existing.broadcast if existing is not None else wol.DEFAULT_BROADCAST)
                ),
                wol_port=existing.wol_port if existing is not None else wol.DEFAULT_WOL_PORT,
                delay_s=existing.delay_s if existing is not None else protocol.DEFAULT_POWER_DELAY_S,
                daemon_machine_id=(
                    state.machine_id
                    if state is not None and state.machine_id
                    else session.discovered.id
                ),
                client_id=client_id,
                tailscale_name=tailscale_name,
                tailscale_ip=tailscale_ip,
                last_path=last_path,
            )
            self._store.save_key(local_id, key.hex())
            try:
                self._store.save(doc.upsert(config, select=True))
            except BargeError:
                self._store.unpair(local_id)
                raise
        except BargeError as exc:
            # The daemon may still hold the approved client; revoke it there if
            # the local save cannot be completed, and show why pairing failed.
            session.status = "failed"
            session.error = exc.message
            return

        session.status = "approved"
        session.machine_id = local_id
        session.client_id = client_id
        self._on_paired(config, client_id)

    async def _state_after_approval(
        self,
        session: PairingSession,
        client_id: str,
        key: bytes,
    ) -> Optional[protocol.DaemonState]:
        """Fetch the daemon's state with the new credentials to learn MAC and IDs."""
        client = DaemonClient.for_lan(
            session.discovered.address,
            session.discovered.port,
            key,
            client_id=client_id,
            transport=self._transport,
        )
        try:
            return await asyncio.to_thread(client.state)
        except BargeError:
            # The pairing still stands; a later poll fills in the missing details.
            return None
