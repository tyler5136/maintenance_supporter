"""Fork (tyler5136): native Vikunja sync (helpers/vikunja_sync).

Pins: a due task creates exactly one Vikunja task in the configured project
(title, 09:00 due, priority, description marker); completing in HA marks it
done (kept as catalog, not deleted); done in Vikunja completes the task here;
a hand-deleted Vikunja task is recreated; nothing is sent without a token;
the token is write-only (never in the settings echo / status). Plus the HTTP
client's read-modify-write update.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.maintenance_supporter.const import (
    CONF_TASKS,
    CONF_VIKUNJA_PROJECT_ID,
    CONF_VIKUNJA_URL,
    DOMAIN,
)
from custom_components.maintenance_supporter.helpers import vikunja_sync as vs
from custom_components.maintenance_supporter.helpers.vikunja_sync import (
    PRIORITY_URGENT,
    VIKUNJA_STATE_KEY,
    VIKUNJA_SYNC_KEY,
    VikunjaClient,
    api_base,
)

from .conftest import build_task_data, make_global_entry, make_object_entry, setup_integration

URL = "https://tasks.example"
PROJECT = 23
SECRET = "tk_test_not_a_real_token"


class FakeVikunja:
    """In-memory stand-in for VikunjaClient."""

    def __init__(self) -> None:
        self.tasks: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self._n = 100

    async def get_project(self, project_id: int) -> dict[str, Any] | None:
        return {"id": project_id, "title": "Home Maintenance"} if project_id == PROJECT else None

    async def create_task(self, project_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        self._n += 1
        task = {"id": self._n, "project_id": project_id, "done": False, **fields}
        self.tasks[self._n] = task
        self.calls.append(("create", project_id))
        return task

    async def get_task(self, task_id: int) -> dict[str, Any] | None:
        return self.tasks.get(task_id)

    async def update_task(self, task_id: int, changes: dict[str, Any]) -> bool:
        if task_id not in self.tasks:
            return False
        self.tasks[task_id].update(changes)
        self.calls.append(("update", task_id))
        return True


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeVikunja:
    f = FakeVikunja()
    monkeypatch.setattr(vs.VikunjaSync, "_client", lambda self: f)
    return f


def _task(days_ago: int = 60) -> dict[str, Any]:
    last = (dt_util.now().date() - timedelta(days=days_ago)).isoformat()
    return build_task_data(task_id="t1", name="Filter", interval_days=30, warning_days=3, last_performed=last)


async def _setup(hass: HomeAssistant, *, token: bool = True):
    g = make_global_entry(hass, extra_data={CONF_VIKUNJA_URL: URL, CONF_VIKUNJA_PROJECT_ID: PROJECT})
    obj = make_object_entry(hass, tasks={"t1": _task()}, uid="fork_vik_sync")
    await setup_integration(hass, g, obj)
    sync = hass.data[DOMAIN][VIKUNJA_SYNC_KEY]
    if token:
        await sync.async_set_token(SECRET)
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(obj.entry_id)
    return entry, sync


def _record(entry) -> dict | None:
    return entry.runtime_data.store.get_task_state("t1").get(VIKUNJA_STATE_KEY)


async def _refresh(entry, hass: HomeAssistant) -> None:
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


def test_api_base_normalises() -> None:
    assert api_base("https://h/") == "https://h/api/v1"
    assert api_base("https://h/api/v1/") == "https://h/api/v1"


async def test_due_task_creates_one_vikunja_task(hass: HomeAssistant, fake: FakeVikunja) -> None:
    entry, sync = await _setup(hass)
    await _refresh(entry, hass)
    assert len(fake.tasks) == 1
    [vt] = fake.tasks.values()
    assert vt["project_id"] == PROJECT
    assert vt["title"] == "Test Object: Filter"
    assert vt["priority"] == PRIORITY_URGENT  # 60 days on a 30-day interval = overdue
    due = date.fromisoformat(entry.runtime_data.coordinator.data[CONF_TASKS]["t1"].next_due)
    assert vt["due_date"].startswith(due.isoformat() + "T09:00")
    assert f"ms:{entry.entry_id}/t1" in vt["description"]
    assert _record(entry)["id"] == vt["id"]
    # Further refreshes change nothing.
    n = len(fake.calls)
    await _refresh(entry, hass)
    assert len(fake.calls) == n


async def test_complete_in_ha_marks_done_and_keeps_it(hass: HomeAssistant, fake: FakeVikunja) -> None:
    entry, sync = await _setup(hass)
    await _refresh(entry, hass)
    vid = _record(entry)["id"]
    await entry.runtime_data.coordinator.complete_maintenance("t1", notes="done", source="panel")
    await hass.async_block_till_done()
    assert fake.tasks[vid]["done"] is True
    assert _record(entry) is None
    assert len(fake.tasks) == 1  # catalog keeps it


async def test_done_in_vikunja_completes_here(hass: HomeAssistant, fake: FakeVikunja) -> None:
    entry, sync = await _setup(hass)
    await _refresh(entry, hass)
    vid = _record(entry)["id"]
    fake.tasks[vid]["done"] = True
    await sync.async_poll()
    await hass.async_block_till_done()
    merged = entry.runtime_data.coordinator._get_merged_tasks_data()["t1"]
    assert [h for h in merged["history"] if h["type"] == "completed"]
    assert _record(entry) is None
    assert len(fake.tasks) == 1  # not recreated: the task is OK again


async def test_hand_deleted_task_is_recreated(hass: HomeAssistant, fake: FakeVikunja) -> None:
    entry, sync = await _setup(hass)
    await _refresh(entry, hass)
    old = _record(entry)["id"]
    fake.tasks.pop(old)
    await sync.async_poll()
    await hass.async_block_till_done()
    assert _record(entry)["id"] != old and len(fake.tasks) == 1


async def test_no_token_sends_nothing(hass: HomeAssistant, fake: FakeVikunja) -> None:
    entry, sync = await _setup(hass, token=False)
    await _refresh(entry, hass)
    assert not sync.enabled and fake.calls == [] and _record(entry) is None


async def test_token_is_write_only(hass: HomeAssistant, hass_ws_client, fake: FakeVikunja) -> None:
    entry, sync = await _setup(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/vikunja/status"})
    status = await client.receive_json()
    assert status["success"] and status["result"]["token_set"] is True
    assert status["result"]["test"] == {"ok": True, "project_title": "Home Maintenance"}
    await client.send_json({"id": 2, "type": f"{DOMAIN}/settings"})
    settings = await client.receive_json()
    assert SECRET not in str(status) and SECRET not in str(settings)
    assert SECRET not in str(entry.data) and SECRET not in str(hass.config_entries.async_entries(DOMAIN)[0].options)


async def test_client_update_is_read_modify_write(hass: HomeAssistant, aioclient_mock) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.get(f"{URL}/api/v1/tasks/7", json={"id": 7, "title": "t", "description": "keep", "done": False})
    aioclient_mock.post(f"{URL}/api/v1/tasks/7", json={"id": 7})
    client = VikunjaClient(async_get_clientsession(hass), URL, SECRET)
    assert await client.update_task(7, {"done": True})
    method, url, body, headers = aioclient_mock.mock_calls[-1]
    assert method == "POST" and body == {"id": 7, "title": "t", "description": "keep", "done": True}
    assert headers["Authorization"] == f"Bearer {SECRET}"
