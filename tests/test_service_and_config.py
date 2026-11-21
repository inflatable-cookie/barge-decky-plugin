"""Tests for ConfigStore secret isolation and BargeService async flows."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

from barge_plugin.config import ConfigStore, MachineConfig, MachinesDocument
from barge_plugin.errors import BargeError
from barge_plugin.protocol import STATE_SCHEMA
from barge_plugin.service import BargeService
from barge_plugin.transport import HttpResponse

from support import NullTransport


class RecordingLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, msg: str) -> None:
        self.messages.append(msg)


class FakeTransport:
    def __init__(self) -> None:
        self.responses: list[HttpResponse | BargeError] = []
        self.calls: list[tuple[str, str, int, str, dict[str, str], bytes]] = []

    def request(
        self,
        method: str,
        host: str,
        port: int,
        path: str,
        headers: dict[str, str],
        body: bytes,
        timeout_s: float,
    ) -> HttpResponse:
        self.calls.append((method, host, port, path, headers, body))
        item = self.responses.pop(0)
        if isinstance(item, BargeError):
            raise item
        return item


class FakeWolSender:
    def __init__(self, error: OSError | None = None) -> None:
        self.packets: list[tuple[bytes, str, int]] = []
        self.error = error

    def send_to(self, data: bytes, host: str, port: int) -> int:
        if self.error is not None:
            raise self.error
        self.packets.append((data, host, port))
        return len(data)


def test_config_store_keeps_key_separate_and_owner_only(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "settings")
    cfg = MachineConfig.validated(
        machine_id="pc",
        name="pc",
        host="pc.local",
        daemon_port=9777,
        mac="00-11-22-33-44-FF",
        broadcast="192.0.2.255",
        wol_port=9,
        delay_s=15,
    )
    store.save(MachinesDocument(machines=(cfg,), selected_id=cfg.id))

    key_hex = "ab" * 32
    source_file = tmp_path / "pair.key"
    source_file.write_text(f"{key_hex}\n", encoding="utf-8")
    store.import_key_file(cfg.id, str(source_file))

    loaded = store.load().selected
    assert loaded is not None
    assert loaded.mac == "00:11:22:33:44:ff"
    assert store.is_paired(cfg.id)
    assert store.read_key(cfg.id) == bytes.fromhex(key_hex)

    # Machine list JSON never contains the secret key.
    config_text = (tmp_path / "settings" / "machines.json").read_text(encoding="utf-8")
    assert key_hex not in config_text
    assert stat.S_IMODE(store.key_path(cfg.id).stat().st_mode) == 0o600

    assert store.unpair(cfg.id) is True
    assert not store.is_paired(cfg.id)


def test_service_never_logs_or_exposes_key_and_handles_optimistic_actions(tmp_path: Path) -> None:
    async def run() -> None:
        key_hex = "ef" * 32
        store = ConfigStore(tmp_path / "settings")
        transport = FakeTransport()
        sender = FakeWolSender()
        logger = RecordingLogger()
        emitted: list[dict[str, Any]] = []

        async def emit(event: str, payload: Any) -> None:
            assert event == "barge_state"
            emitted.append(payload)

        svc = BargeService(
            store,
            transport=transport,
            local_transport=NullTransport(),  # type: ignore[arg-type]
            sender=sender,
            emit_fn=emit,
            logger=logger,
        )

        # Configure and pair; poll returns awake.
        transport.responses.append(
            HttpResponse(
                200,
                json.dumps(
                    {
                        "schema": STATE_SCHEMA,
                        "power": "awake",
                        "observed_at_unix_ms": 1_000,
                    }
                ).encode("utf-8"),
            )
        )
        saved = await svc.save_settings(
            name="pc",
            host="192.0.2.50",
            daemon_port=9777,
            mac="00:11:22:33:44:ff",
            lan_key=key_hex,
        )
        assert saved["ok"] is True
        assert saved["state"]["phase"] == "awake"
        assert saved["state"]["paired"] is True
        assert key_hex not in json.dumps(saved)
        assert key_hex not in json.dumps(svc.get_settings())

        # Refused power rolls back optimistic pending state.
        transport.responses.append(
            HttpResponse(
                409,
                b'{"schema":"barge.daemon-error/1","error":"pending","message":"busy"}',
            )
        )
        refused = await svc.power("sleep")
        assert refused["ok"] is False
        assert "busy" in refused["error"]
        assert refused["state"]["phase"] == "awake"
        # Optimistic pending was emitted before the network error rolled it back.
        assert any(item["phase"] == "pending" for item in emitted)

        # Accepted sleep enters pending; failed cancel never reports success.
        transport.responses.append(
            HttpResponse(
                202,
                b'{"schema":"barge.power-result/1","action":"sleep","deadline_unix_ms":16000}',
            )
        )
        ok_sleep = await svc.power("sleep")
        assert ok_sleep["ok"] is True
        assert ok_sleep["state"]["phase"] == "pending"

        transport.responses.append(BargeError("unreachable", "barged is unreachable"))
        failed_cancel = await svc.cancel()
        assert failed_cancel["ok"] is False
        assert failed_cancel["state"]["phase"] == "pending"

        # Wake works while unpaired/offline via WoL sender.
        transport.responses.append(BargeError("unreachable", "down"))
        await svc.poll_once()
        wake_res = await svc.wake()
        assert wake_res["ok"] is True
        assert wake_res["state"]["phase"] == "waking"
        assert len(sender.packets) == 1

        # Secret key never appeared in logs.
        for line in logger.messages:
            assert key_hex not in line

    asyncio.run(run())


def test_wake_failure_rolls_back_to_offline(tmp_path: Path) -> None:
    async def run() -> None:
        store = ConfigStore(tmp_path / "settings")
        store.save(
            MachinesDocument(
                machines=(
                    MachineConfig.validated(
                        machine_id="pc",
                        name="pc",
                        host="192.0.2.50",
                        mac="00:11:22:33:44:ff",
                    ),
                ),
                selected_id="pc",
            )
        )
        svc = BargeService(
            store,
            local_transport=NullTransport(),  # type: ignore[arg-type]
            sender=FakeWolSender(error=OSError("no route")),
        )
        await svc.poll_once()
        res = await svc.wake()
        assert res["ok"] is False
        assert res["state"]["phase"] == "offline"
        assert "no route" in res["error"]

    asyncio.run(run())
