"""Discovery pairing against a loopback fake barged: approve, deny, expire, address moves."""

from __future__ import annotations

import asyncio
import json
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from barge_plugin import mdns
from barge_plugin.config import ConfigStore, MachineConfig, MachinesDocument
from barge_plugin.errors import BargeError
from barge_plugin.pair_flow import default_client_name
from barge_plugin.service import BargeService
from fake_daemon import FakeBarged
from support import FakeBrowser, NullTransport, ShortTimeoutTransport


def discovered(fake: FakeBarged, address: str = "127.0.0.1") -> mdns.DiscoveredMachine:
    return mdns.DiscoveredMachine(
        id=fake.machine_id,
        name=fake.name,
        mode="lan",
        protocol_version="1",
        address=address,
        port=fake.port,
    )


def build_service(settings: Path, browser: FakeBrowser) -> BargeService:
    return BargeService(
        ConfigStore(settings),
        transport=ShortTimeoutTransport(),  # type: ignore[arg-type]
        local_transport=NullTransport(),  # type: ignore[arg-type]
        browser=browser,
        browse_timeout_s=0.05,
    )


def test_pairing_approval_saves_the_key_and_learned_details(tmp_path: Path) -> None:
    async def scenario() -> None:
        with FakeBarged() as fake:
            browser = FakeBrowser()
            browser.machines = [discovered(fake)]
            svc = build_service(tmp_path / "settings", browser)

            found = await svc.discover()
            assert found["ok"] is True
            assert found["state"]["discovered"][0]["id"] == fake.machine_id

            started = await svc.start_pairing(fake.machine_id, "Steam Deck")
            assert started["ok"] is True
            request_id = started["pairing"]["request_id"]
            assert started["pairing"]["status"] == "waiting"
            # The code the Deck shows is the code barged derived for the same exchange.
            assert started["pairing"]["code"] == fake.pending_codes()[request_id]

            still_waiting = await svc.pairing_status()
            assert still_waiting["pairing"]["status"] == "waiting"

            fake.decide(request_id, "approved")
            approved = await svc.pairing_status()
            assert approved["ok"] is True
            assert approved["pairing"]["status"] == "approved"
            assert approved["pairing"]["machine_id"]
            assert approved["state"]["discovered"] == []

            local_id = approved["pairing"]["machine_id"]
            client_id = fake.pending[request_id]["client_id"]
            store = ConfigStore(tmp_path / "settings")
            machine = store.load().get(local_id)
            assert machine is not None
            assert machine.daemon_machine_id == fake.machine_id
            assert machine.client_id == client_id
            assert machine.mac == fake.mac
            assert machine.broadcast == fake.broadcast
            assert machine.host == "127.0.0.1"
            assert machine.daemon_port == fake.port

            stored_key = store.read_key(local_id)
            assert stored_key is not None
            assert stored_key.hex() == fake.client_keys[client_id].hex()
            key_path = store.key_path(local_id)
            assert stat.S_IMODE(key_path.stat().st_mode) == 0o600

            polls = await svc.poll_once()
            assert polls["machines"][0]["reachable"] is True
            assert polls["phase"] == "awake"
            signed_calls = [
                headers
                for method, path, headers in fake.requests
                if path == "/v1/state" and method == "GET"
            ]
            assert signed_calls[-1].get("x-barge-client-id") == client_id

            snapshot = json.dumps(polls)
            settings = json.dumps(svc.get_settings())
            assert stored_key.hex() not in snapshot
            assert stored_key.hex() not in settings
            assert "lan_key" not in snapshot

    asyncio.run(scenario())


def test_the_deck_names_itself_on_the_pc(monkeypatch: Any) -> None:
    monkeypatch.setattr("barge_plugin.pair_flow.socket.gethostname", lambda: "steamdeck")
    assert default_client_name() == "Steam Deck"

    monkeypatch.setattr("barge_plugin.pair_flow.socket.gethostname", lambda: "gaming-pc")
    assert default_client_name() == "gaming-pc"

    monkeypatch.setattr("barge_plugin.pair_flow.socket.gethostname", lambda: "localhost")
    assert default_client_name() == "Steam Deck"


def test_start_browses_once_and_stops_cleanly(tmp_path: Path) -> None:
    async def scenario() -> None:
        with FakeBarged() as fake:
            browser = FakeBrowser()
            browser.machines = [discovered(fake)]
            svc = build_service(tmp_path / "settings", browser)

            svc.start()
            discovery = svc._discover_task
            assert discovery is not None
            await discovery
            assert browser.calls == 1
            assert [machine["id"] for machine in svc.snapshot()["discovered"]] == [
                fake.machine_id
            ]

            await svc.stop()
            assert svc._poll_task is None
            assert svc._discover_task is None

    asyncio.run(scenario())


def test_denied_pairing_writes_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        with FakeBarged() as fake:
            browser = FakeBrowser()
            browser.machines = [discovered(fake)]
            svc = build_service(tmp_path / "settings", browser)
            await svc.discover()
            started = await svc.start_pairing(fake.machine_id)

            fake.decide(started["pairing"]["request_id"], "denied")
            result = await svc.pairing_status()

            assert result["pairing"]["status"] == "denied"
            assert ConfigStore(tmp_path / "settings").load().machines == ()

    asyncio.run(scenario())


def test_expired_pairing_is_reported_and_writes_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        with FakeBarged() as fake:
            browser = FakeBrowser()
            browser.machines = [discovered(fake)]
            svc = build_service(tmp_path / "settings", browser)
            await svc.discover()
            started = await svc.start_pairing(fake.machine_id)

            fake.expire(started["pairing"]["request_id"])
            result = await svc.pairing_status()

            assert result["pairing"]["status"] == "expired"
            assert ConfigStore(tmp_path / "settings").load().machines == ()

    asyncio.run(scenario())


def test_pairing_an_unreachable_or_already_paired_machine_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        with FakeBarged() as fake:
            browser = FakeBrowser()
            browser.machines = [discovered(fake)]
            svc = build_service(tmp_path / "settings", browser)
            await svc.discover()
            started = await svc.start_pairing(fake.machine_id)
            fake.decide(started["pairing"]["request_id"], "approved")
            await svc.pairing_status()

            again = await svc.start_pairing(fake.machine_id)
            assert again["ok"] is False
            assert "already paired" in again["error"]

        browser = FakeBrowser()
        browser.machines = [discovered(fake, address="127.0.0.2")]
        svc = build_service(tmp_path / "settings-2", browser)
        await svc.discover()
        with FakeBarged() as other:
            browser.machines = [
                mdns.DiscoveredMachine(
                    id=other.machine_id,
                    name=other.name,
                    mode="lan",
                    protocol_version="1",
                    address="127.0.0.1",
                    port=1,  # nothing listens on the discard port
                )
            ]
            await svc.discover()
            failed = await svc.start_pairing(other.machine_id)
            assert failed["ok"] is False
            assert "unreachable" in failed["error"]

    asyncio.run(scenario())


def test_rediscovery_moves_a_paired_machine_by_its_daemon_id(tmp_path: Path) -> None:
    async def scenario() -> None:
        with FakeBarged() as fake:
            browser = FakeBrowser()
            browser.machines = [discovered(fake)]
            svc = build_service(tmp_path / "settings", browser)
            await svc.discover()
            started = await svc.start_pairing(fake.machine_id)
            fake.decide(started["pairing"]["request_id"], "approved")
            approved = await svc.pairing_status()
            local_id = approved["pairing"]["machine_id"]
            store = ConfigStore(tmp_path / "settings")
            key_before = store.read_key(local_id)

            browser.machines = [discovered(fake, address="127.0.0.2")]
            moved = await svc.discover()

            machine = store.load().get(local_id)
            assert machine is not None
            assert machine.host == "127.0.0.2"
            assert machine.daemon_machine_id == fake.machine_id
            assert store.read_key(local_id) == key_before
            assert moved["state"]["discovered"] == []

    asyncio.run(scenario())


@pytest.mark.parametrize("host", ["192.0.2.55", "PC.Local."])
def test_legacy_machine_is_hidden_by_address_or_hostname(tmp_path: Path, host: str) -> None:
    async def scenario() -> None:
        store = ConfigStore(tmp_path / "settings")
        saved = MachineConfig.validated(
            machine_id="pc", name="pc", host=host, mac="",
        )
        store.save(MachinesDocument(machines=(saved,), selected_id=saved.id))
        browser = FakeBrowser()
        browser.machines = [
            mdns.DiscoveredMachine(
                id="ab" * 16,
                name="Living Room PC",
                mode="lan",
                protocol_version="1",
                address="192.0.2.55",
                port=9788,
                hostname="pc.local",
            )
        ]
        svc = build_service(tmp_path / "settings", browser)

        result = await svc.discover()

        assert result["state"]["discovered"] == []
        assert store.load().machines == (replace(saved, host="192.0.2.55", daemon_port=9788),)

    asyncio.run(scenario())


@pytest.mark.parametrize("same_advertised_id", [True, False])
def test_discovery_uses_reported_mac_after_a_legacy_machine_moves(
    tmp_path: Path, same_advertised_id: bool,
) -> None:
    async def scenario() -> None:
        with FakeBarged() as fake:
            store = ConfigStore(tmp_path / "settings")
            saved = MachineConfig.validated(
                machine_id="pc", name="pc", host="127.0.0.2",
                daemon_port=fake.port, mac=fake.mac,
            )
            store.save(MachinesDocument(machines=(saved,), selected_id=saved.id))
            key = bytes.fromhex("22" * 32)
            fake.client_keys["legacy"] = key
            store.save_key(saved.id, key.hex())
            browser = FakeBrowser()
            advertisement = discovered(fake)
            if not same_advertised_id:
                advertisement = replace(advertisement, id="cd" * 16)
            browser.machines = [advertisement]
            svc = build_service(tmp_path / "settings", browser)

            result = await svc.discover()

            assert store.read_key(saved.id) == key
            if same_advertised_id:
                assert result["state"]["discovered"] == []
                expected = replace(
                    saved,
                    host="127.0.0.1",
                    daemon_machine_id=fake.machine_id,
                    broadcast=fake.broadcast,
                )
                assert store.load().machines == (expected,)
            else:
                assert result["state"]["discovered"] == [advertisement.to_dict()]
                assert store.load().machines == (saved,)

    asyncio.run(scenario())


def test_state_fills_missing_details_and_preserves_configured_ones(tmp_path: Path) -> None:
    async def scenario() -> None:
        with FakeBarged() as fake:
            store = ConfigStore(tmp_path / "settings")
            saved = MachineConfig.validated(
                machine_id="pc", name="pc", host="127.0.0.1",
                daemon_port=fake.port, mac="",
            )
            store.save(MachinesDocument(machines=(saved,), selected_id=saved.id))
            key = bytes.fromhex("22" * 32)
            fake.client_keys["legacy"] = key
            store.save_key(saved.id, key.hex())
            svc = build_service(tmp_path / "settings", FakeBrowser())

            learned = await svc.poll_once()
            expected = replace(
                saved, daemon_machine_id=fake.machine_id, mac=fake.mac, broadcast=fake.broadcast,
            )
            assert learned["reachable"] is True
            assert store.load().machines == (expected,)

            fake.mac = "00:11:22:33:44:ee"
            fake.broadcast = "192.0.2.255"
            await svc.poll_once()
            assert store.load().machines == (expected,)

            fake.machine_id = "cd" * 16
            refused = await svc.poll_once()
            assert refused["error"] == "barged reported a different machine ID"
            assert store.load().machines == (expected,)

    asyncio.run(scenario())


def test_discovery_failure_is_reported_without_losing_the_machine_list(tmp_path: Path) -> None:
    async def scenario() -> None:
        browser = FakeBrowser()
        browser.error = BargeError("io", "cannot bind mDNS socket: address in use")
        svc = build_service(tmp_path / "settings", browser)

        result = await svc.discover()

        assert result["ok"] is False
        assert "cannot bind" in result["error"]
        assert result["state"]["discover_error"] == browser.error.message
        assert result["state"]["discovered"] == []
        assert svc.get_error_log()["entries"] == []

    asyncio.run(scenario())


def test_clients_are_listed_and_revoked_with_the_machines_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        with FakeBarged() as fake:
            browser = FakeBrowser()
            browser.machines = [discovered(fake)]
            svc = build_service(tmp_path / "settings", browser)
            await svc.discover()
            started = await svc.start_pairing(fake.machine_id)
            fake.decide(started["pairing"]["request_id"], "approved")
            approved = await svc.pairing_status()
            local_id = approved["pairing"]["machine_id"]
            client_id = fake.pending[started["pairing"]["request_id"]]["client_id"]

            listed = await svc.get_clients(local_id)
            assert listed["ok"] is True
            assert [(item["id"], item["this_deck"]) for item in listed["clients"]] == [
                (client_id, True)
            ]

            revoked = await svc.revoke_client(client_id, local_id)
            assert revoked["ok"] is True
            assert revoked["revoked_is_this_deck"] is True
            assert revoked["clients"] == []
            assert fake.clients == {}

            unpaired = await svc.get_clients("missing-machine")
            assert unpaired["ok"] is False

    asyncio.run(scenario())


def test_pairing_state_never_exposes_the_derived_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        with FakeBarged() as fake:
            browser = FakeBrowser()
            browser.machines = [discovered(fake)]
            svc = build_service(tmp_path / "settings", browser)
            await svc.discover()
            started = await svc.start_pairing(fake.machine_id)
            assert started["ok"] is True

            payload = json.dumps(started)
            session = svc._pair_flow.session
            assert session is not None
            assert session.derived.key.hex() not in payload
            assert session.derived.key not in payload.encode()

    asyncio.run(scenario())


def test_start_pairing_rejects_an_unknown_discovery_id(tmp_path: Path) -> None:
    async def scenario() -> None:
        browser = FakeBrowser()
        svc = build_service(tmp_path / "settings", browser)
        await svc.discover()

        result = await svc.start_pairing("not-a-machine-id")

        assert result["ok"] is False
        assert "valid discovery id" in result["error"]

    asyncio.run(scenario())
