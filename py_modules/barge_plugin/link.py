"""Route selection across a paired machine's LAN and Tailscale addresses.

Mirrors `crates/barge-machine/src/link.rs`: a paired machine tries its LAN
host first with a short connect timeout (1.5 s), then its tailnet name, then
its tailnet IP. Only a failure to establish the TCP connection (`not_sent=True`)
moves on to the next route, so an action is never sent twice. The route that
last answered is remembered on the machine and tried first next time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, List, Optional, Sequence, Tuple, TypeVar

from .config import MachineConfig, RoutePath
from .errors import BargeError
from .transport import LAN_CONNECT_TIMEOUT_S, DaemonClient, HttpTransport

T = TypeVar("T")


@dataclass(frozen=True)
class Reached(Generic[T]):
    """A daemon response and the route that delivered it."""

    value: T
    path: RoutePath
    host: str


class MachineLink:
    """Ordered candidate routes to one paired machine's `barged`."""

    def __init__(
        self,
        routes: Sequence[Tuple[RoutePath, DaemonClient]],
        preferred: Optional[RoutePath] = None,
    ) -> None:
        ordered = list(routes)
        if preferred is not None:
            # Keep relative order among candidates of the same route (e.g.
            # tailscale_name before tailscale_ip) while putting `preferred` first.
            front = [item for item in ordered if item[0] == preferred]
            rest = [item for item in ordered if item[0] != preferred]
            ordered = front + rest
        self._routes: List[Tuple[RoutePath, DaemonClient]] = ordered

    @classmethod
    def for_machine(
        cls,
        config: MachineConfig,
        key: bytes,
        *,
        transport: Optional[HttpTransport] = None,
    ) -> MachineLink:
        """Build the candidate routes for a paired machine.

        Order before preference: LAN host (1.5 s connect timeout), tailnet name,
        then tailnet IP. All routes sign with the same client credentials.
        """
        client_id = config.client_id or None
        routes: List[Tuple[RoutePath, DaemonClient]] = []
        if config.host:
            routes.append(
                (
                    "lan",
                    DaemonClient.for_lan(
                        host=config.host,
                        port=config.daemon_port,
                        key=key,
                        client_id=client_id,
                        transport=transport,
                        connect_timeout_s=LAN_CONNECT_TIMEOUT_S,
                    ),
                )
            )
        seen_tailnet: set[str] = set()
        for candidate in (config.tailscale_name, config.tailscale_ip):
            if candidate and candidate not in seen_tailnet:
                seen_tailnet.add(candidate)
                routes.append(
                    (
                        "tailscale",
                        DaemonClient.for_lan(
                            host=candidate,
                            port=config.daemon_port,
                            key=key,
                            client_id=client_id,
                            transport=transport,
                        ),
                    )
                )
        return cls(routes, preferred=config.last_path)

    def paths(self) -> List[Tuple[RoutePath, str]]:
        """Candidate `(path, host)` pairs in the order they will be tried."""
        return [(path, client.host) for path, client in self._routes]

    def run(self, operation: Callable[[DaemonClient], T]) -> Reached[T]:
        """Run `operation` on the first route that connects.

        Any response from the daemon (even a refusal) or any failure after a
        connection was established stops immediately without trying later routes.
        """
        unsent: List[str] = []
        for path, client in self._routes:
            try:
                value = operation(client)
                return Reached(value=value, path=path, host=client.host)
            except BargeError as exc:
                if exc.not_sent:
                    unsent.append(exc.message)
                    continue
                raise
        message = "; ".join(unsent) if unsent else "no route to the machine"
        raise BargeError("unreachable", message, not_sent=True)
