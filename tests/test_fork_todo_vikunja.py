"""Fork (tyler5136): to-do mirror for a Vikunja-style list.

Pins: a list that only takes a due *datetime* gets the task's due date at
09:00 local (and follows a moved date); with ``mirror_todo_keep_completed``
a finished task's row is checked off instead of deleted, both when it is
completed elsewhere and when it is checked off on the list.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.components.todo import (
    DOMAIN as TODO_DOMAIN,
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import setup_test_component_platform

from custom_components.maintenance_supporter.const import (
    CONF_MIRROR_TODO_DEFAULT_ENTITIES,
    CONF_MIRROR_TODO_KEEP_COMPLETED,
    CONF_TASKS,
    DOMAIN,
)
from custom_components.maintenance_supporter.helpers.todo_mirror import (
    DUE_DATETIME_HOUR,
    MIRROR_STATE_KEY,
    TODO_MIRROR_KEY,
    due_fields,
)

from .conftest import build_task_data, make_global_entry, make_object_entry, setup_integration

VIK = "todo.vikunja"
ROW = "Test Object: Filter"


class DatetimeList(TodoListEntity):
    """Like the Vikunja integration: due *datetime* only, no date-only field."""

    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
    )

    def __init__(self) -> None:
        self._attr_name = "Vikunja"
        self._attr_unique_id = "vikunja"
        self._attr_todo_items: list[TodoItem] = []
        self._n = 0

    def rows(self) -> list[TodoItem]:
        return list(self._attr_todo_items)

    def check_off(self, uid: str) -> None:
        self._attr_todo_items = [
            TodoItem(summary=i.summary, uid=i.uid, status=TodoItemStatus.COMPLETED, due=i.due) if i.uid == uid else i
            for i in self._attr_todo_items
        ]
        self.async_write_ha_state()

    async def async_create_todo_item(self, item: TodoItem) -> None:
        self._n += 1
        self._attr_todo_items.append(TodoItem(summary=item.summary, uid=f"v-{self._n}", status=item.status, due=item.due))
        self.async_write_ha_state()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        self._attr_todo_items = [i for i in self._attr_todo_items if i.uid not in uids]
        self.async_write_ha_state()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        self._attr_todo_items = [item if i.uid == item.uid else i for i in self._attr_todo_items]
        self.async_write_ha_state()


def _task(days_ago: int = 60) -> dict[str, Any]:
    last = (dt_util.now().date() - timedelta(days=days_ago)).isoformat()
    return build_task_data(task_id="t1", name="Filter", interval_days=30, warning_days=3, last_performed=last)


async def _setup(hass: HomeAssistant, *, keep: bool):
    vik = DatetimeList()
    setup_test_component_platform(hass, TODO_DOMAIN, [vik])
    assert await async_setup_component(hass, TODO_DOMAIN, {TODO_DOMAIN: {"platform": "test"}})
    await hass.async_block_till_done()
    g = make_global_entry(hass, extra_data={CONF_MIRROR_TODO_DEFAULT_ENTITIES: [VIK], CONF_MIRROR_TODO_KEEP_COMPLETED: keep})
    obj = make_object_entry(hass, tasks={"t1": _task()}, uid="fork_vikunja")
    await setup_integration(hass, g, obj)
    entry = hass.config_entries.async_get_entry(obj.entry_id)
    return vik, entry, hass.data[DOMAIN][TODO_MIRROR_KEY]


def _record(entry) -> dict | None:
    return entry.runtime_data.store.get_task_state("t1").get(MIRROR_STATE_KEY)


def test_due_fields_shapes() -> None:
    d = date(2026, 12, 1)
    assert due_fields(None, "date") == {} and due_fields(d, None) == {}
    assert due_fields(d, "date") == {"due_date": d}
    dt = due_fields(d, "datetime")["due_datetime"]
    assert dt.date() == d and dt.time() == time(DUE_DATETIME_HOUR, 0) and dt.tzinfo is not None


async def test_datetime_only_list_gets_the_due_date_at_nine(hass: HomeAssistant) -> None:
    vik, entry, mirror = await _setup(hass, keep=False)
    due = date.fromisoformat(entry.runtime_data.coordinator.data[CONF_TASKS]["t1"].next_due)
    [row] = vik.rows()
    assert row.summary == ROW
    assert isinstance(row.due, datetime)
    assert dt_util.as_local(row.due).date() == due and dt_util.as_local(row.due).hour == DUE_DATETIME_HOUR
    assert _record(entry)[VIK]["due"] == due.isoformat()


async def test_upstream_default_deletes_the_finished_row(hass: HomeAssistant) -> None:
    vik, entry, mirror = await _setup(hass, keep=False)
    await entry.runtime_data.coordinator.complete_maintenance("t1", notes="done", source="panel")
    await hass.async_block_till_done()
    assert vik.rows() == []


async def test_keep_completed_checks_off_instead_of_deleting(hass: HomeAssistant) -> None:
    vik, entry, mirror = await _setup(hass, keep=True)
    await entry.runtime_data.coordinator.complete_maintenance("t1", notes="done", source="panel")
    await hass.async_block_till_done()
    [row] = vik.rows()
    assert row.summary == ROW and row.status == TodoItemStatus.COMPLETED
    assert _record(entry) is None


async def test_keep_completed_leaves_a_checked_off_row_as_history(hass: HomeAssistant) -> None:
    vik, entry, mirror = await _setup(hass, keep=True)
    uid = _record(entry)[VIK]["uid"]
    vik.check_off(uid)
    await mirror.async_check_lists()
    await hass.async_block_till_done()

    merged = entry.runtime_data.coordinator._get_merged_tasks_data()["t1"]
    assert [h for h in merged["history"] if h["type"] == "completed"]
    [row] = vik.rows()
    assert row.uid == uid and row.status == TodoItemStatus.COMPLETED
    assert _record(entry) is None
    # Next pass adds nothing: the task is OK again.
    await mirror.async_check_lists()
    await hass.async_block_till_done()
    assert len(vik.rows()) == 1
