"""Tests for Tailscale fallback routing, route memory, and tailnet field learning."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from barge_plugin import auth, mdns
from barge_plugin.config import MACHINES_FILENAME, ConfigStore, MachineConfig
from barge_plugin.errors import BargeError
from barge_plugin.link import MachineLink
from barge_plugin.protocol import (
    CANCEL_RESULT_SCHEMA,
    CLIENTS_SCHEMA,
    POWER_RESULT_SCHEMA,
    STATE_SCHEMA,
)
from barge_plugin.service import BargeService
from barge_plugin.transport import (
    DEFAULT_TIMEOUT_S,
    LAN_CONNECT_TIMEOUT_S,
    DaemonClient,
    HttpResponse,
    StdlibHttpTransport,
)

from fake_daemon import FakeBarged
from support import FakeBrowser, NullTransport

KEY_HEX = "c3" * 32
KEY_BYTES = bytes.fromhex(KEY_HEX)


def _closed_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _state_bytes(
    *,
    machine_id: str = "ab" * 16,
    mac: Optional[str] = "00:11:22:33:44:ff",
    broadcast: Optional[str] = "192.0.2.255",
    tailscale_name: Optional[str] = None,
    tailscale_ip: Optional[str] = None,
) -> bytes:
    doc: Dict[str, Any] = {
        "schema": STATE_SCHEMA,
        "power": "awake",
        "observed_at_unix_ms": 1_000,
        "machine_id": machine_id,
    }
    if mac is not None:
        doc["mac"] = mac
    if broadcast is not None:
        doc["broadcast"] = broadcast
    if tailscale_name is not None:
        doc["tailscale_name"] = tailscale_name
    if tailscale_ip is not None:
        doc["tailscale_ip"] = tailscale_ip
    return json.dumps(doc).encode("utf-8")


class RecordingHostTransport:
    """Per-host response queue that also records `timeout_s` for each call."""

    def __init__(self) -> None:
        self.queues: Dict[str, List[Any]] = {}
        self.calls: List[Dict[str, Any]] = []

    def queue(self, host: str, *items: Any) -> None:
        self.queues.setdefault(host, []).extend(items)

    def request(
        self,
        method: str,
        host: str,
        port: int,
        path: str,
        headers: Dict[str, str],
        body: bytes,
        timeout_s: float,
    ) -> HttpResponse:
        self.calls.append(
            {
                "method": method,
                "host": host,
                "port": port,
                "path": path,
                "headers": dict(headers),
                "body": body,
                "timeout_s": timeout_s,
            }
        )
        queue = self.queues.get(host, [])
        if not queue:
            raise BargeError("unreachable", f"no connection to {host}:{port}", not_sent=True)
        item = queue.pop(0)
        if isinstance(item, BargeError):
            raise item
        return item


class FakeSender:
    def __init__(self) -> None:
        self.packets: List[Tuple[bytes, str, int]] = []

    def send_to(self, data: bytes, host: str, port: int) -> int:
        self.packets.append((data, host, port))
        return len(data)


def _make_service(
    settings: Path,
    transport: Any,
    *,
    sender: Optional[FakeSender] = None,
) -> BargeService:
    async def build() -> BargeService:
        return BargeService(
            ConfigStore(settings),
            transport=transport,
            local_transport=NullTransport(),  # type: ignore[arg-type]
            sender=sender or FakeSender(),  # type: ignore[arg-type]
        )

    return asyncio.run(build())


def test_learns_updates_and_persists_tailnet_fields_from_state(tmp_path: Path) -> None:
    settings = tmp_path / "settings"
    transport = RecordingHostTransport()
    svc = _make_service(settings, transport)

    transport.queue(
        "pc.local",
        HttpResponse(
            200,
            _state_bytes(
                tailscale_name="pc.tail0123.ts.net",
                tailscale_ip="100.64.0.7",
            ),
        ),
    )
    saved = asyncio.run(
        svc.save_settings(
            name="pc",
            host="pc.local",
            daemon_port=9777,
            mac="00:11:22:33:44:ff",
            lan_key=KEY_HEX,
        )
    )
    assert saved["ok"] is True

    # Persisted on disk and exposed in snapshot/settings.
    loaded = ConfigStore(settings).load().get("pc")
    assert loaded is not None
    assert loaded.tailscale_name == "pc.tail0123.ts.net"
    assert loaded.tailscale_ip == "100.64.0.7"
    assert loaded.last_path == "lan"
    assert saved["state"]["route"] == "lan"
    assert saved["state"]["route_host"] == "pc.local"

    # A later poll that omits tailnet fields keeps the previously learned ones.
    transport.queue("pc.local", HttpResponse(200, _state_bytes()))
    asyncio.run(svc.poll_once())
    loaded = ConfigStore(settings).load().get("pc")
    assert loaded is not None
    assert loaded.tailscale_name == "pc.tail0123.ts.net"
    assert loaded.tailscale_ip == "100.64.0.7"

    # A later poll with changed tailnet name/IP updates them on disk.
    transport.queue(
        "pc.local",
        HttpResponse(
            200,
            _state_bytes(
                tailscale_name="pc-renamed.tail0123.ts.net",
                tailscale_ip="100.64.0.99",
            ),
        ),
    )
    asyncio.run(svc.poll_once())
    loaded = ConfigStore(settings).load().get("pc")
    assert loaded is not None
    assert loaded.tailscale_name == "pc-renamed.tail0123.ts.net"
    assert loaded.tailscale_ip == "100.64.0.99"


def test_pairing_also_learns_tailnet_fields(tmp_path: Path) -> None:
    with FakeBarged() as daemon:
        # Inject tailscale_name/ip into FakeBarged's state document.
        orig_state = daemon._state_document
        daemon._state_document = lambda: {  # type: ignore[method-assign]
            **orig_state(),
            "tailscale_name": "gaming-pc.tail999.ts.net",
            "tailscale_ip": "100.64.1.2",
        }

        async def run() -> None:
            browser = FakeBrowser()
            browser.machines = [
                mdns.DiscoveredMachine(
                    id=daemon.machine_id,
                    name=daemon.name,
                    mode="lan",
                    protocol_version="1",
                    address="127.0.0.1",
                    port=daemon.port,
                )
            ]
            store = ConfigStore(tmp_path / "settings")
            svc = BargeService(
                store,
                browser=browser,
                local_transport=NullTransport(),  # type: ignore[arg-type]
                sender=FakeSender(),  # type: ignore[arg-type]
            )
            await svc.discover()
            started = await svc.start_pairing(daemon.machine_id, "Steam Deck")
            assert started["ok"] is True
            daemon.decide(started["pairing"]["request_id"], "approved", client_id="deck-1")
            polled = await svc.pairing_status()
            assert polled["pairing"]["status"] == "approved"

            loaded = store.load().get(polled["pairing"]["machine_id"])
            assert loaded is not None
            assert loaded.tailscale_name == "gaming-pc.tail999.ts.net"
            assert loaded.tailscale_ip == "100.64.1.2"
            assert loaded.last_path == "lan"

        asyncio.run(run())


def test_older_v3_settings_without_tailnet_fields_still_load(tmp_path: Path) -> None:
    settings = tmp_path / "settings"
    settings.mkdir()
    doc = {
        "schema": "barge-decky.config/3",
        "selected": "pc",
        "machines": [
            {
                "id": "pc",
                "name": "pc",
                "host": "192.0.2.50",
                "daemon_port": 9777,
                "mac": "00:11:22:33:44:ff",
                "broadcast": "192.0.2.255",
                "wol_port": 9,
                "delay_s": 15,
                "daemon_machine_id": "ab" * 16,
                "client_id": "cid-1",
            }
        ],
    }
    (settings / MACHINES_FILENAME).write_text(json.dumps(doc), encoding="utf-8")

    loaded = ConfigStore(settings).load().get("pc")
    assert loaded is not None
    assert loaded.tailscale_name == ""
    assert loaded.tailscale_ip == ""
    assert loaded.last_path is None

    # Invalid last_path on disk is rejected as unknown_state.
    doc["machines"][0]["last_path"] = "wan"
    (settings / MACHINES_FILENAME).write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(BargeError) as exc_info:
        ConfigStore(settings).load()
    assert exc_info.value.kind == "unknown_state"


def test_route_order_and_connect_timeout_for_paired_machine() -> None:
    cfg = MachineConfig.validated(
        machine_id="pc",
        name="pc",
        host="192.0.2.50",
        daemon_port=9777,
        mac="00:11:22:33:44:ff",
        tailscale_name="pc.tail0123.ts.net",
        tailscale_ip="100.64.0.7",
        last_path=None,
    )
    default_link = MachineLink.for_machine(cfg, KEY_BYTES)
    assert default_link.paths() == [
        ("lan", "192.0.2.50"),
        ("tailscale", "pc.tail0123.ts.net"),
        ("tailscale", "100.64.0.7"),
    ]
    assert default_link._routes[0][1]._connect_timeout_s == LAN_CONNECT_TIMEOUT_S
    assert default_link._routes[1][1]._connect_timeout_s is None

    # With last_path='tailscale', tailnet name then tailnet IP move ahead of LAN.
    ts_preferred = MachineConfig.validated(
        machine_id="pc",
        name="pc",
        host="192.0.2.50",
        daemon_port=9777,
        mac="00:11:22:33:44:ff",
        tailscale_name="pc.tail0123.ts.net",
        tailscale_ip="100.64.0.7",
        last_path="tailscale",
    )
    ts_link = MachineLink.for_machine(ts_preferred, KEY_BYTES)
    assert ts_link.paths() == [
        ("tailscale", "pc.tail0123.ts.net"),
        ("tailscale", "100.64.0.7"),
        ("lan", "192.0.2.50"),
    ]


def test_falls_back_from_lan_to_tailnet_name_then_tailnet_ip_and_remembers_route(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "settings"
    transport = RecordingHostTransport()
    store = ConfigStore(settings)
    store.save_key("pc", KEY_HEX)
    cfg = MachineConfig.validated(
        machine_id="pc",
        name="pc",
        host="192.0.2.50",
        daemon_port=9777,
        mac="00:11:22:33:44:ff",
        client_id="cid-deck",
        tailscale_name="pc.tail0123.ts.net",
        tailscale_ip="100.64.0.7",
    )
    store.save(store.load().upsert(cfg, select=True))
    svc = _make_service(settings, transport)

    # 1. LAN fails to connect (not_sent=True), tailnet name fails DNS/connect, tailnet IP answers!
    transport.queue("192.0.2.50", BargeError("unreachable", "timeout", not_sent=True))
    transport.queue("pc.tail0123.ts.net", BargeError("unreachable", "dns", not_sent=True))
    transport.queue(
        "100.64.0.7",
        HttpResponse(
            200,
            _state_bytes(
                tailscale_name="pc.tail0123.ts.net",
                tailscale_ip="100.64.0.7",
            ),
        ),
    )

    snap = asyncio.run(svc.poll_once())
    assert snap["phase"] == "awake"
    assert snap["route"] == "tailscale"
    assert snap["route_host"] == "100.64.0.7"
    assert [c["host"] for c in transport.calls] == [
        "192.0.2.50",
        "pc.tail0123.ts.net",
        "100.64.0.7",
    ]
    # LAN attempt used the 1.5 s connect timeout; tailnet used default 5.0 s.
    assert transport.calls[0]["timeout_s"] == LAN_CONNECT_TIMEOUT_S
    assert transport.calls[1]["timeout_s"] == DEFAULT_TIMEOUT_S
    # Tailnet route carries the exact same client credentials and HMAC signature.
    tailnet_headers = transport.calls[2]["headers"]
    assert tailnet_headers[auth.CLIENT_ID_HEADER] == "cid-deck"
    assert auth.verify_request(
        KEY_BYTES,
        "GET",
        "/v1/state",
        tailnet_headers[auth.TIMESTAMP_HEADER],
        tailnet_headers[auth.NONCE_HEADER],
        b"",
        tailnet_headers[auth.SIGNATURE_HEADER],
    )

    # Persisted last_path is 'tailscale'.
    assert store.load().get("pc").last_path == "tailscale"

    # 2. Restart the service: tailnet is tried FIRST without touching LAN!
    transport.calls.clear()
    restarted = _make_service(settings, transport)
    assert restarted.snapshot()["route"] == "tailscale"
    assert restarted.snapshot()["route_host"] == "pc.tail0123.ts.net"

    transport.queue(
        "pc.tail0123.ts.net",
        HttpResponse(
            200,
            _state_bytes(
                tailscale_name="pc.tail0123.ts.net",
                tailscale_ip="100.64.0.7",
            ),
        ),
    )
    snap2 = asyncio.run(restarted.poll_once())
    assert snap2["route"] == "tailscale"
    assert snap2["route_host"] == "pc.tail0123.ts.net"
    assert [c["host"] for c in transport.calls] == ["pc.tail0123.ts.net"]

    # 3. Power and cancel also work over Tailscale when LAN cannot connect.
    transport.calls.clear()
    transport.queue(
        "pc.tail0123.ts.net",
        HttpResponse(
            202,
            json.dumps(
                {
                    "schema": POWER_RESULT_SCHEMA,
                    "action": "sleep",
                    "deadline_unix_ms": 16_000,
                }
            ).encode(),
        ),
    )
    power_res = asyncio.run(restarted.power("sleep"))
    assert power_res["ok"] is True
    assert power_res["state"]["phase"] == "pending"
    assert power_res["state"]["route"] == "tailscale"
    assert [c["host"] for c in transport.calls] == ["pc.tail0123.ts.net"]

    transport.calls.clear()
    transport.queue(
        "pc.tail0123.ts.net",
        HttpResponse(
            200,
            json.dumps({"schema": CANCEL_RESULT_SCHEMA, "cancelled": True}).encode(),
        ),
    )
    transport.queue(
        "pc.tail0123.ts.net",
        HttpResponse(
            200,
            _state_bytes(
                tailscale_name="pc.tail0123.ts.net",
                tailscale_ip="100.64.0.7",
            ),
        ),
    )
    cancel_res = asyncio.run(restarted.cancel())
    assert cancel_res["ok"] is True
    assert cancel_res["cancelled"] is True
    assert cancel_res["state"]["phase"] == "awake"
    assert [c["host"] for c in transport.calls] == ["pc.tail0123.ts.net", "pc.tail0123.ts.net"]

    # 4. Back home without Tailscale: tailnet fails to connect -> falls back to LAN and switches last_path!
    transport.calls.clear()
    transport.queue("pc.tail0123.ts.net", BargeError("unreachable", "no tailscale", not_sent=True))
    transport.queue("100.64.0.7", BargeError("unreachable", "no tailscale", not_sent=True))
    transport.queue("192.0.2.50", HttpResponse(200, _state_bytes()))
    home_snap = asyncio.run(restarted.poll_once())
    assert home_snap["route"] == "lan"
    assert home_snap["route_host"] == "192.0.2.50"
    assert store.load().get("pc").last_path == "lan"
    assert [c["host"] for c in transport.calls] == [
        "pc.tail0123.ts.net",
        "100.64.0.7",
        "192.0.2.50",
    ]


def test_real_stdlib_transport_falls_back_on_connect_refused_and_never_retries_after_connect() -> None:
    transport = StdlibHttpTransport()

    # Case 1: First route port is closed (connect refused -> not_sent=True), second route answers!
    with FakeBarged() as second_daemon:
        client_id = "cid-real"
        second_daemon.clients[client_id] = {
            "id": client_id,
            "name": "Deck",
            "paired_at_unix_ms": 1,
        }
        second_daemon.client_keys[client_id] = KEY_BYTES

        closed = _closed_loopback_port()
        link = MachineLink(
            [
                (
                    "lan",
                    DaemonClient.for_lan(
                        "127.0.0.1",
                        closed,
                        KEY_BYTES,
                        client_id=client_id,
                        transport=transport,
                        connect_timeout_s=0.3,
                    ),
                ),
                (
                    "tailscale",
                    DaemonClient.for_lan(
                        "127.0.0.1",
                        second_daemon.port,
                        KEY_BYTES,
                        client_id=client_id,
                        transport=transport,
                    ),
                ),
            ]
        )
        reached = link.run(lambda c: c.state())
        assert reached.path == "tailscale"
        assert reached.value.machine_id == second_daemon.machine_id

    # Case 2: First route ACCEPTS the TCP connection and drops without answering (not_sent=False).
    # The second route MUST NOT be contacted, so an action is never sent twice!
    drop_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    drop_listener.bind(("127.0.0.1", 0))
    drop_listener.listen(1)
    drop_port = int(drop_listener.getsockname()[1])

    def accept_and_drop() -> None:
        conn, _ = drop_listener.accept()
        try:
            conn.recv(4096)
        finally:
            conn.close()

    drop_thread = threading.Thread(target=accept_and_drop, daemon=True)
    drop_thread.start()

    second_connections = 0
    second_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    second_listener.bind(("127.0.0.1", 0))
    second_listener.listen(1)
    second_listener.settimeout(0.2)
    second_port = int(second_listener.getsockname()[1])

    def watch_second() -> None:
        nonlocal second_connections
        try:
            conn, _ = second_listener.accept()
            second_connections += 1
            conn.close()
        except socket.timeout:
            pass

    watch_thread = threading.Thread(target=watch_second, daemon=True)
    watch_thread.start()

    try:
        link_after_connect = MachineLink(
            [
                (
                    "lan",
                    DaemonClient.for_lan(
                        "127.0.0.1",
                        drop_port,
                        KEY_BYTES,
                        transport=transport,
                        timeout_s=1.0,
                        connect_timeout_s=0.5,
                    ),
                ),
                (
                    "tailscale",
                    DaemonClient.for_lan(
                        "127.0.0.1",
                        second_port,
                        KEY_BYTES,
                        transport=transport,
                    ),
                ),
            ]
        )
        with pytest.raises(BargeError) as exc_info:
            link_after_connect.run(lambda c: c.power("sleep", 15))
        assert exc_info.value.kind == "unreachable"
        assert exc_info.value.not_sent is False
        watch_thread.join(timeout=1.0)
        assert second_connections == 0
    finally:
        drop_thread.join(timeout=1.0)
        drop_listener.close()
        second_listener.close()


def test_real_stdlib_transport_stops_on_refusal_and_read_timeout() -> None:
    transport = StdlibHttpTransport()

    # Read timeout after TCP connect succeeded must set not_sent=False and never retry.
    hang_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    hang_listener.bind(("127.0.0.1", 0))
    hang_listener.listen(1)
    hang_port = int(hang_listener.getsockname()[1])
    release = threading.Event()

    def accept_and_hang() -> None:
        conn, _ = hang_listener.accept()
        try:
            conn.recv(4096)
            release.wait(timeout=2.0)
        finally:
            conn.close()

    hang_thread = threading.Thread(target=accept_and_hang, daemon=True)
    hang_thread.start()

    with FakeBarged() as backup:
        try:
            link = MachineLink(
                [
                    (
                        "lan",
                        DaemonClient.for_lan(
                            "127.0.0.1",
                            hang_port,
                            KEY_BYTES,
                            transport=transport,
                            timeout_s=0.15,
                            connect_timeout_s=0.5,
                        ),
                    ),
                    (
                        "tailscale",
                        DaemonClient.for_lan(
                            "127.0.0.1",
                            backup.port,
                            KEY_BYTES,
                            transport=transport,
                        ),
                    ),
                ]
            )
            with pytest.raises(BargeError) as exc_info:
                link.run(lambda c: c.power("shutdown", 15))
            assert exc_info.value.kind == "unreachable"
            assert exc_info.value.not_sent is False
            assert backup.requests == []
        finally:
            release.set()
            hang_thread.join(timeout=1.0)
            hang_listener.close()

    # Daemon refusal (401/403/409) on first route stops immediately without trying second.
    with FakeBarged() as first, FakeBarged() as second:
        # `first` has no client key registered -> answers 401 unauthorized.
        second.clients["cid"] = {"id": "cid", "name": "Deck", "paired_at_unix_ms": 1}
        second.client_keys["cid"] = KEY_BYTES

        link = MachineLink(
            [
                (
                    "lan",
                    DaemonClient.for_lan(
                        "127.0.0.1",
                        first.port,
                        KEY_BYTES,
                        client_id="cid",
                        transport=transport,
                    ),
                ),
                (
                    "tailscale",
                    DaemonClient.for_lan(
                        "127.0.0.1",
                        second.port,
                        KEY_BYTES,
                        client_id="cid",
                        transport=transport,
                    ),
                ),
            ]
        )
        with pytest.raises(BargeError) as exc_info:
            link.run(lambda c: c.state())
        assert exc_info.value.kind == "refused"
        assert len(first.requests) == 1
        assert second.requests == []


def test_client_management_and_wake_stay_lan_only_and_all_routes_unreachable_is_offline(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "settings"
    transport = RecordingHostTransport()
    sender = FakeSender()
    store = ConfigStore(settings)
    store.save_key("pc", KEY_HEX)
    cfg = MachineConfig.validated(
        machine_id="pc",
        name="pc",
        host="192.0.2.50",
        daemon_port=9777,
        mac="00:11:22:33:44:ff",
        broadcast="192.0.2.255",
        client_id="cid-deck",
        tailscale_name="pc.tail0123.ts.net",
        tailscale_ip="100.64.0.7",
        last_path="tailscale",
    )
    store.save(store.load().upsert(cfg, select=True))
    svc = _make_service(settings, transport, sender=sender)

    # Client management uses LAN host only, never tailnet even when last_path='tailscale'.
    transport.queue(
        "192.0.2.50",
        HttpResponse(
            200,
            json.dumps(
                {
                    "schema": CLIENTS_SCHEMA,
                    "clients": [{"id": "cid-deck", "name": "Deck", "paired_at_unix_ms": 1}],
                }
            ).encode(),
        ),
    )
    clients = asyncio.run(svc.get_clients("pc"))
    assert clients["ok"] is True
    assert [c["host"] for c in transport.calls] == ["192.0.2.50"]

    # When every route fails to connect, state is Offline offering Wake (never asleep).
    transport.calls.clear()
    transport.queue("pc.tail0123.ts.net", BargeError("unreachable", "no tailscale", not_sent=True))
    transport.queue("100.64.0.7", BargeError("unreachable", "no tailscale", not_sent=True))
    transport.queue("192.0.2.50", BargeError("unreachable", "no lan", not_sent=True))
    snap = asyncio.run(svc.poll_once())
    assert snap["phase"] == "offline"
    assert snap["offers"] == ["wake"]
    assert snap["attention"] is None

    # Wake stays LAN broadcast only.
    wake_res = asyncio.run(svc.wake("pc"))
    assert wake_res["ok"] is True
    assert len(sender.packets) == 1
    assert sender.packets[0][1:] == ("192.0.2.255", 9)


def test_real_transport_with_unresolvable_tailscale_name_behaves_without_tailscale() -> None:
    transport = StdlibHttpTransport()
    closed_lan = _closed_loopback_port()
    closed_ts_ip = _closed_loopback_port()

    link = MachineLink(
        [
            (
                "lan",
                DaemonClient.for_lan(
                    "127.0.0.1",
                    closed_lan,
                    KEY_BYTES,
                    transport=transport,
                    connect_timeout_s=0.2,
                ),
            ),
            (
                "tailscale",
                DaemonClient.for_lan(
                    "nonexistent.invalid",
                    9777,
                    KEY_BYTES,
                    transport=transport,
                    connect_timeout_s=0.2,
                ),
            ),
            (
                "tailscale",
                DaemonClient.for_lan(
                    "127.0.0.1",
                    closed_ts_ip,
                    KEY_BYTES,
                    transport=transport,
                    connect_timeout_s=0.2,
                ),
            ),
        ]
    )
    with pytest.raises(BargeError) as exc_info:
        link.run(lambda c: c.state())
    assert exc_info.value.kind == "unreachable"
    assert exc_info.value.not_sent is True
    assert str(closed_lan) in exc_info.value.message
    assert "nonexistent.invalid" in exc_info.value.message
    assert str(closed_ts_ip) in exc_info.value.message
