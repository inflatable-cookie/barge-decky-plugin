"""Tests for the machine list: v0.1.0 migration, per-machine keys, selection, and the error log."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from barge_plugin import auth
from barge_plugin.config import (
    LEGACY_CONFIG_FILENAME,
    LEGACY_KEY_FILENAME,
    MACHINES_FILENAME,
    ConfigStore,
)
from barge_plugin.error_log import ERROR_LOG_CAPACITY, ErrorEntry, ErrorLog
from barge_plugin.errors import BargeError
from barge_plugin.protocol import STATE_SCHEMA
from barge_plugin.service import BargeService
from barge_plugin.transport import HttpResponse

from support import NullTransport

KEY_A = "a1" * 32
KEY_B = "b2" * 32
AWAKE = HttpResponse(
    200,
    json.dumps({"schema": STATE_SCHEMA, "power": "awake", "observed_at_unix_ms": 1000}).encode(),
)
UNAUTHORIZED = HttpResponse(
    401,
    b'{"schema":"barge.daemon-error/1","error":"unauthorized","message":"bad signature"}',
)
BUSY = HttpResponse(
    409,
    b'{"schema":"barge.daemon-error/1","error":"pending","message":"busy"}',
)


def unreachable(host: str) -> BargeError:
    return BargeError("unreachable", f"barged at http://{host}:9777 is unreachable: [Errno 113] No route to host")


class HostTransport:
    """Answers each host from its own queue, so concurrent polls never race for one response."""

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
        self.calls.append({"host": host, "path": path, "headers": dict(headers)})
        queue = self.queues.get(host, [])
        if not queue:
            raise BargeError("unreachable", f"no response queued for {host}")
        item = queue.pop(0)
        if isinstance(item, BargeError):
            raise item
        return item


class FakeSender:
    def __init__(self) -> None:
        self.packets: List[bytes] = []

    def send_to(self, data: bytes, host: str, port: int) -> int:
        self.packets.append(data)
        return len(data)


def make_service(
    settings: Path,
    transport: HostTransport,
    error_log: Optional[ErrorLog] = None,
) -> BargeService:
    """Build a service inside a loop: Python 3.9's asyncio primitives need one at construction."""

    async def build() -> BargeService:
        return BargeService(
            ConfigStore(settings),
            transport=transport,  # type: ignore[arg-type]
            local_transport=NullTransport(),  # type: ignore[arg-type]
            sender=FakeSender(),  # type: ignore[arg-type]
            error_log=error_log,
        )

    return asyncio.run(build())


def write_legacy(settings: Path, *, name: str = "pc", key: Optional[str] = KEY_A) -> None:
    """Write the exact v0.1.0 on-disk layout: machine.json (schema /1) and lan.key."""
    settings.mkdir(parents=True, exist_ok=True)
    document = {
        "schema": "barge-decky.config/1",
        "machine": {
            "name": name,
            "host": "pc.local",
            "daemon_port": 9777,
            "mac": "00:11:22:33:44:ff",
            "broadcast": "192.0.2.255",
            "wol_port": 9,
            "delay_s": 15,
        },
    }
    (settings / LEGACY_CONFIG_FILENAME).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    if key is not None:
        key_file = settings / LEGACY_KEY_FILENAME
        key_file.write_text(f"{key}\n", encoding="utf-8")
        os.chmod(key_file, 0o600)


def add_machine(svc: BargeService, name: str, host: str, key: str) -> Dict[str, Any]:
    return asyncio.run(
        svc.save_settings(
            name=name,
            host=host,
            daemon_port=9777,
            mac="00:11:22:33:44:ff",
            lan_key=key,
        )
    )


def test_v010_settings_migrate_in_place_and_keep_the_key(tmp_path: Path) -> None:
    settings = tmp_path / "settings"
    write_legacy(settings, key=KEY_A)
    store = ConfigStore(settings)

    doc = store.load()

    assert doc.ids() == ["pc"]
    assert doc.selected_id == "pc"
    machine = doc.selected
    assert machine is not None
    assert (machine.name, machine.host, machine.mac) == ("pc", "pc.local", "00:11:22:33:44:ff")
    assert store.read_key("pc") == bytes.fromhex(KEY_A)
    assert stat.S_IMODE((settings / "keys" / "pc.key").stat().st_mode) == 0o600
    assert (settings / MACHINES_FILENAME).exists()
    assert not (settings / LEGACY_CONFIG_FILENAME).exists()
    assert not (settings / LEGACY_KEY_FILENAME).exists()
    assert KEY_A not in (settings / MACHINES_FILENAME).read_text(encoding="utf-8")


def test_v020_list_is_read_and_rewritten_with_the_daemon_fields(tmp_path: Path) -> None:
    settings = tmp_path / "settings"
    settings.mkdir()
    document = {
        "schema": "barge-decky.config/2",
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
            }
        ],
    }
    (settings / MACHINES_FILENAME).write_text(json.dumps(document), encoding="utf-8")
    store = ConfigStore(settings)

    loaded = store.load()
    machine = loaded.get("pc")

    assert machine is not None
    assert machine.daemon_machine_id == ""
    assert machine.client_id == ""
    store.save(loaded)
    saved = json.loads((settings / MACHINES_FILENAME).read_text(encoding="utf-8"))
    assert saved["schema"] == "barge-decky.config/3"
    assert saved["machines"][0]["client_id"] == ""
    assert saved["machines"][0]["daemon_machine_id"] == ""


def test_migration_runs_once_and_later_loads_read_the_new_list(tmp_path: Path) -> None:
    settings = tmp_path / "settings"
    write_legacy(settings, key=KEY_A)
    store = ConfigStore(settings)

    first = store.load()
    second = store.load()

    assert second == first
    assert store.read_key("pc") == bytes.fromhex(KEY_A)


def test_migration_without_a_legacy_key_leaves_the_machine_unpaired(tmp_path: Path) -> None:
    settings = tmp_path / "settings"
    write_legacy(settings, key=None)
    store = ConfigStore(settings)

    doc = store.load()

    assert doc.selected is not None and doc.selected.name == "pc"
    assert not store.is_paired("pc")


def test_migration_turns_the_display_name_into_a_file_safe_id(tmp_path: Path) -> None:
    settings = tmp_path / "settings"
    write_legacy(settings, name="Living_Room.PC", key=KEY_A)

    doc = ConfigStore(settings).load()

    assert doc.selected is not None
    assert doc.selected.id == "living-room-pc"
    assert doc.selected.name == "Living_Room.PC"
    assert ConfigStore(settings).read_key("living-room-pc") == bytes.fromhex(KEY_A)


def test_unknown_legacy_schema_is_refused_and_the_key_is_left_alone(tmp_path: Path) -> None:
    settings = tmp_path / "settings"
    settings.mkdir()
    (settings / LEGACY_CONFIG_FILENAME).write_text(
        json.dumps({"schema": "something/9", "machine": {}}), encoding="utf-8"
    )
    (settings / LEGACY_KEY_FILENAME).write_text(KEY_A, encoding="utf-8")

    with pytest.raises(BargeError) as info:
        ConfigStore(settings).load()

    assert info.value.kind == "unknown_state"
    assert (settings / LEGACY_KEY_FILENAME).read_text(encoding="utf-8") == KEY_A
    assert not (settings / MACHINES_FILENAME).exists()
    assert not (settings / "keys").exists()


def test_each_machine_has_its_own_key_and_requests_are_signed_with_it(tmp_path: Path) -> None:
    transport = HostTransport()
    svc = make_service(tmp_path / "settings", transport)
    transport.queue("den.local", *[AWAKE] * 4)
    transport.queue("attic.local", *[AWAKE] * 4)

    add_machine(svc, "Den", "den.local", KEY_A)
    add_machine(svc, "Attic", "attic.local", KEY_B)
    asyncio.run(svc.poll_once())

    store = ConfigStore(tmp_path / "settings")
    assert store.read_key("den") == bytes.fromhex(KEY_A)
    assert store.read_key("attic") == bytes.fromhex(KEY_B)
    assert (tmp_path / "settings" / "keys" / "den.key").exists()
    assert (tmp_path / "settings" / "keys" / "attic.key").exists()

    keys = {"den.local": bytes.fromhex(KEY_A), "attic.local": bytes.fromhex(KEY_B)}
    assert transport.calls, "expected signed polls"
    for call in transport.calls:
        headers = call["headers"]
        args = (
            "GET",
            call["path"],
            headers[auth.TIMESTAMP_HEADER],
            headers[auth.NONCE_HEADER],
            b"",
            headers[auth.SIGNATURE_HEADER],
        )
        assert auth.verify_request(keys[call["host"]], *args)
        other = keys["attic.local" if call["host"] == "den.local" else "den.local"]
        assert not auth.verify_request(other, *args)

    snap = svc.snapshot()
    assert [m["phase"] for m in snap["machines"]] == ["awake", "awake"]
    assert all(m["paired"] for m in snap["machines"])
    assert KEY_A not in json.dumps(snap) and KEY_B not in json.dumps(snap)
    assert KEY_A not in json.dumps(svc.get_settings())


def test_selection_is_remembered_across_restarts(tmp_path: Path) -> None:
    transport = HostTransport()
    settings = tmp_path / "settings"
    svc = make_service(settings, transport)
    add_machine(svc, "Den", "den.local", KEY_A)
    add_machine(svc, "Attic", "attic.local", KEY_B)
    assert svc.snapshot()["selected_id"] == "attic"

    asyncio.run(svc.select_machine("den"))
    restarted = make_service(settings, transport)

    assert restarted.snapshot()["selected_id"] == "den"
    assert restarted.snapshot()["machine"]["name"] == "Den"


def test_removing_a_machine_deletes_only_its_key_and_reselects(tmp_path: Path) -> None:
    transport = HostTransport()
    settings = tmp_path / "settings"
    svc = make_service(settings, transport)
    add_machine(svc, "Den", "den.local", KEY_A)
    add_machine(svc, "Attic", "attic.local", KEY_B)
    asyncio.run(svc.select_machine("attic"))

    removed = asyncio.run(svc.remove_machine("attic"))

    assert removed["ok"] is True
    assert not (settings / "keys" / "attic.key").exists()
    assert (settings / "keys" / "den.key").exists()
    assert removed["state"]["selected_id"] == "den"
    assert [m["id"] for m in svc.get_settings()["machines"]] == ["den"]
    unknown = asyncio.run(svc.remove_machine("attic"))
    assert unknown["ok"] is False


def test_editing_a_machine_keeps_its_id_and_key(tmp_path: Path) -> None:
    transport = HostTransport()
    settings = tmp_path / "settings"
    svc = make_service(settings, transport)
    add_machine(svc, "Den", "den.local", KEY_A)

    edited = asyncio.run(
        svc.save_settings(
            machine_id="den",
            name="Den",
            host="den-2.local",
            daemon_port=9777,
            mac="00:11:22:33:44:01",
        )
    )

    assert edited["ok"] is True
    assert edited["machine_id"] == "den"
    assert ConfigStore(settings).read_key("den") == bytes.fromhex(KEY_A)
    assert ConfigStore(settings).load().get("den").host == "den-2.local"


def test_machine_names_must_be_unique_and_nothing_is_written_on_clash(tmp_path: Path) -> None:
    transport = HostTransport()
    svc = make_service(tmp_path / "settings", transport)
    add_machine(svc, "Den", "den.local", KEY_A)

    clash = add_machine(svc, "den", "other.local", KEY_B)

    assert clash["ok"] is False
    assert "already named" in clash["error"]
    assert [m["id"] for m in svc.get_settings()["machines"]] == ["den"]
    assert not (tmp_path / "settings" / "keys" / "den-2.key").exists()


def test_key_paths_reject_ids_that_could_leave_the_keys_folder(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "settings")

    for bad in ("../lan", "Den", "", "a/b", "x" * 49):
        with pytest.raises(BargeError):
            store.key_path(bad)


def test_error_log_is_capped_and_newest_first() -> None:
    log = ErrorLog()
    assert ERROR_LOG_CAPACITY == 50
    for index in range(ERROR_LOG_CAPACITY + 10):
        log.add(
            ErrorEntry(
                at_unix_ms=index,
                machine_id="den",
                machine_name="Den",
                kind="unreachable",
                message=f"failure {index}",
            )
        )

    entries = log.entries()

    assert len(log) == ERROR_LOG_CAPACITY
    assert len(entries) == ERROR_LOG_CAPACITY
    assert entries[0]["message"] == f"failure {ERROR_LOG_CAPACITY + 9}"
    assert entries[-1]["message"] == "failure 10"


def test_unreachable_poll_logs_once_until_the_machine_answers(tmp_path: Path) -> None:
    transport = HostTransport()
    log = ErrorLog()
    svc = make_service(tmp_path / "settings", transport, error_log=log)
    # The save's own poll is the first queued answer.
    transport.queue(
        "den.local",
        unreachable("den.local"),
        unreachable("den.local"),
        unreachable("den.local"),
        AWAKE,
        unreachable("den.local"),
    )
    add_machine(svc, "Den", "den.local", KEY_A)
    assert len(log) == 1

    asyncio.run(svc.poll_once())
    asyncio.run(svc.poll_once())
    assert len(log) == 1
    assert svc.snapshot()["attention"] is None
    assert svc.snapshot()["phase"] == "offline"

    asyncio.run(svc.poll_once())
    assert len(log) == 1

    asyncio.run(svc.poll_once())
    assert len(log) == 2
    assert svc.get_error_log()["entries"][0]["kind"] == "unreachable"


def test_refused_poll_needs_attention_on_the_panel_and_in_the_log(tmp_path: Path) -> None:
    transport = HostTransport()
    svc = make_service(tmp_path / "settings", transport)
    transport.queue("den.local", AWAKE, UNAUTHORIZED)
    add_machine(svc, "Den", "den.local", KEY_A)

    snap = asyncio.run(svc.poll_once())

    assert snap["attention"] is not None
    assert "HTTP 401" in snap["attention"]["message"]
    entries = svc.get_error_log()["entries"]
    assert len(entries) == 1
    assert entries[0]["kind"] == "refused"
    assert entries[0]["machine_name"] == "Den"


def test_missing_key_shows_a_short_attention_line(tmp_path: Path) -> None:
    transport = HostTransport()
    svc = make_service(tmp_path / "settings", transport)

    result = asyncio.run(
        svc.save_settings(
            name="Den",
            host="den.local",
            daemon_port=9777,
            mac="00:11:22:33:44:ff",
        )
    )

    assert result["ok"] is True
    assert result["state"]["attention"]["message"].startswith("Not paired")


def test_refused_action_is_logged_each_time_and_shown_on_the_panel(tmp_path: Path) -> None:
    transport = HostTransport()
    log = ErrorLog()
    svc = make_service(tmp_path / "settings", transport, error_log=log)
    transport.queue("den.local", AWAKE, BUSY, BUSY)
    add_machine(svc, "Den", "den.local", KEY_A)

    first = asyncio.run(svc.power("sleep"))
    second = asyncio.run(svc.power("sleep"))

    assert first["ok"] is False and second["ok"] is False
    assert "busy" in first["state"]["attention"]["message"]
    assert len(log) == 2
    assert {entry["kind"] for entry in svc.get_error_log()["entries"]} == {"refused"}


def test_unreachable_action_is_logged_but_not_shown_on_the_panel(tmp_path: Path) -> None:
    transport = HostTransport()
    svc = make_service(tmp_path / "settings", transport)
    transport.queue("den.local", AWAKE, unreachable("den.local"))
    add_machine(svc, "Den", "den.local", KEY_A)

    result = asyncio.run(svc.power("sleep"))

    assert result["ok"] is False
    assert result["state"]["attention"] is None
    assert svc.get_error_log()["entries"][0]["kind"] == "unreachable"


def test_unreadable_settings_file_is_reported_through_the_log_and_attention(tmp_path: Path) -> None:
    settings = tmp_path / "settings"
    settings.mkdir()
    (settings / MACHINES_FILENAME).write_text("not json", encoding="utf-8")
    svc = make_service(settings, HostTransport())

    asyncio.run(svc.poll_once())
    snap = svc.snapshot()

    assert snap["configured"] is False
    assert snap["machines"] == []
    assert snap["attention"] is not None
    entries = svc.get_error_log()["entries"]
    assert len(entries) == 1
    assert entries[0]["machine_id"] == ""
