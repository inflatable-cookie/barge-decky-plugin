"""Approving pair requests on the PC itself, over localhost.

barged trusts an unsigned loopback caller for `GET /v1/pair/pending` and the
approve/deny routes, so the plugin on the PC can answer a request without being
paired as a client first.
"""

from __future__ import annotations

import asyncio
import http.client
import json
from pathlib import Path
from typing import Any, Dict, Tuple

from barge_plugin import mdns, pairing, protocol
from barge_plugin.config import ConfigStore
from barge_plugin.service import BargeService
from fake_daemon import FakeBarged
from support import FakeBrowser, NullTransport, ShortTimeoutTransport


def request_pairing(fake: FakeBarged, client_name: str) -> Tuple[pairing.PairingKeyPair, Dict[str, Any]]:
    """Ask the fake daemon to create a pending request, as a Deck would."""
    keys = pairing.PairingKeyPair.generate()
    conn = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=5)
    try:
        conn.request(
            "POST",
            "/v1/pair",
            body=protocol.encode_pair_request(client_name, keys.public_key_hex()),
            headers={"content-type": "application/json"},
        )
        response = conn.getresponse()
        return keys, json.loads(response.read())
    finally:
        conn.close()


def discovered(fake: FakeBarged) -> mdns.DiscoveredMachine:
    return mdns.DiscoveredMachine(
        id=fake.machine_id,
        name=fake.name,
        mode="lan",
        protocol_version="1",
        address="127.0.0.1",
        port=fake.port,
    )


def build_service(
    settings: Path,
    browser: FakeBrowser,
    *,
    with_local_daemon: bool = True,
) -> BargeService:
    local = ShortTimeoutTransport() if with_local_daemon else NullTransport()
    return BargeService(
        ConfigStore(settings),
        transport=NullTransport(),  # type: ignore[arg-type]
        local_transport=local,  # type: ignore[arg-type]
        browser=browser,
        browse_timeout_s=0.05,
    )


def test_pending_requests_appear_on_the_pc_without_being_paired(tmp_path: Path) -> None:
    async def scenario() -> None:
        with FakeBarged() as fake:
            _keys, started = request_pairing(fake, "Steam Deck")
            browser = FakeBrowser()
            browser.machines = [discovered(fake)]
            svc = build_service(tmp_path / "settings", browser)

            snap = await svc.discover()
            this_pc = snap["state"]["this_pc"]

            assert this_pc["available"] is True
            assert this_pc["port"] == fake.port
            assert this_pc["pending"] == [
                {
                    "request_id": started["request_id"],
                    "client_name": "Steam Deck",
                    "code": fake.pending_codes()[started["request_id"]],
                    "created_at_unix_ms": fake.pending[started["request_id"]][
                        "created_at_unix_ms"
                    ],
                    "expires_at_unix_ms": fake.pending[started["request_id"]][
                        "expires_at_unix_ms"
                    ],
                }
            ]
            # Approval needs no configured machine and no stored key.
            assert snap["state"]["machines"] == []

    asyncio.run(scenario())


def test_approve_on_the_pc_clears_the_request_and_unlocks_the_deck(tmp_path: Path) -> None:
    async def scenario() -> None:
        with FakeBarged() as fake:
            keys, started = request_pairing(fake, "Steam Deck")
            request_id = started["request_id"]
            browser = FakeBrowser()
            browser.machines = [discovered(fake)]
            svc = build_service(tmp_path / "settings", browser)
            await svc.discover()

            approved = await svc.approve_pair(request_id)

            assert approved["ok"] is True
            assert approved["approved"] is True
            assert approved["client_name"] == "Steam Deck"
            assert approved["state"]["this_pc"]["pending"] == []
            assert fake.pending[request_id]["status"] == "approved"
            # The Deck's own status poll now sees the approval and gets a key.
            deck = protocol.decode_pair_status(
                json.dumps(
                    {
                        "schema": protocol.PAIR_STATUS_SCHEMA,
                        "request_id": request_id,
                        "status": "approved",
                        "client_id": fake.pending[request_id]["client_id"],
                    }
                ).encode()
            )
            assert deck.status == "approved"
            assert keys.public_key_hex() == fake.pending[request_id]["client_public"].hex()
            approve_calls = [
                headers
                for method, path, headers in fake.requests
                if method == "POST" and path == f"/v1/pair/{request_id}/approve"
            ]
            assert approve_calls
            assert "x-barge-signature" not in approve_calls[-1]

    asyncio.run(scenario())


def test_deny_on_the_pc_is_reported_back(tmp_path: Path) -> None:
    async def scenario() -> None:
        with FakeBarged() as fake:
            _keys, started = request_pairing(fake, "Living room laptop")
            request_id = started["request_id"]
            browser = FakeBrowser()
            browser.machines = [discovered(fake)]
            svc = build_service(tmp_path / "settings", browser)
            await svc.discover()

            denied = await svc.deny_pair(request_id)

            assert denied["ok"] is True
            assert denied["approved"] is False
            assert denied["state"]["this_pc"]["pending"] == []
            assert fake.pending[request_id]["status"] == "denied"

            unknown = await svc.deny_pair("req-99")
            assert unknown["ok"] is False
            assert "unknown pairing request" in unknown["error"]

    asyncio.run(scenario())


def test_a_second_request_appears_on_the_next_poll(tmp_path: Path) -> None:
    async def scenario() -> None:
        with FakeBarged() as fake:
            browser = FakeBrowser()
            browser.machines = [discovered(fake)]
            svc = build_service(tmp_path / "settings", browser)
            await svc.discover()
            assert svc.snapshot()["this_pc"]["pending"] == []

            _keys, started = request_pairing(fake, "Steam Deck")
            snap = await svc.poll_once()

            assert [item["request_id"] for item in snap["this_pc"]["pending"]] == [
                started["request_id"]
            ]

    asyncio.run(scenario())


def test_without_a_local_daemon_approval_reports_unreachable(tmp_path: Path) -> None:
    async def scenario() -> None:
        with FakeBarged() as fake:
            _keys, started = request_pairing(fake, "Steam Deck")
            browser = FakeBrowser()
            browser.machines = [discovered(fake)]
            svc = build_service(tmp_path / "settings", browser, with_local_daemon=False)

            snap = await svc.discover()
            assert snap["state"]["this_pc"] == {"available": False, "port": 0, "pending": []}

            result = await svc.approve_pair(started["request_id"])
            assert result["ok"] is False
            assert "no barged answered on localhost" in result["error"]
            assert fake.pending[started["request_id"]]["status"] == "pending"

    asyncio.run(scenario())


def test_malformed_request_id_is_refused_before_any_call(tmp_path: Path) -> None:
    async def scenario() -> None:
        with FakeBarged() as fake:
            browser = FakeBrowser()
            browser.machines = [discovered(fake)]
            svc = build_service(tmp_path / "settings", browser)
            await svc.discover()

            result = await svc.approve_pair("../state")

            assert result["ok"] is False
            assert "request_id" in result["error"]

    asyncio.run(scenario())
