"""Fork (tyler5136): native Vikunja sync — the household task catalog.

While a task is due (due_soon / overdue / triggered; with ``warning_percent``
= 20 that is the last 20 % of its interval) one Vikunja task is kept in the
configured project, talking to the Vikunja REST API directly (no extra HA
integration). It carries the object, area, interval and notes, a due date at
09:00 local and a priority that rises once the task is overdue.

* Task completed / skipped / reset in HA (it leaves the due window) → the
  Vikunja task is marked **done**, never deleted, so the project is the
  catalog of everything that has circulated.
* Vikunja task marked done → the poll (every 5 min, and on demand) completes
  the task here through ``complete_maintenance``; the next cycle creates a
  fresh Vikunja task when it comes due again.
* Vikunja task deleted by hand while still due → recreated on the next pass.
* Only our own tasks: every Vikunja id we create is remembered per task in
  the object's Store (``vikunja`` = ``{"id", "title", "due", "priority"}``).

The API token is a credential: it lives in its own Store file
(``maintenance_supporter.vikunja_credentials``), is written through an
admin-only websocket command and is never echoed back, logged or included in
diagnostics. URL and project id are ordinary global settings.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    COMPLETION_PROVENANCE_NOTES,
    CONF_TASKS,
    CONF_VIKUNJA_PROJECT_ID,
    CONF_VIKUNJA_URL,
    DOMAIN,
    GLOBAL_UNIQUE_ID,
    NOTIFIABLE_STATUSES,
    URGENT_STATUSES,
)
from .coordinator_data import task_results
from .global_options import global_option
from .todo_mirror import DUE_DATETIME_HOUR, mirror_summary, task_due_date

if TYPE_CHECKING:
    from ..coordinator import MaintenanceCoordinator
    from ..models.maintenance_object import MaintenanceObject
    from ..models.task_result import TaskResult
    from ..storage import MaintenanceStore

_LOGGER = logging.getLogger(__name__)

VIKUNJA_SYNC_KEY = "vikunja_sync"
VIKUNJA_STATE_KEY = "vikunja"
CREDENTIALS_STORE_KEY = f"{DOMAIN}.vikunja_credentials"
POLL_INTERVAL = timedelta(minutes=5)
REQUEST_TIMEOUT = 15
PRIORITY_DUE_SOON = 2
PRIORITY_URGENT = 4


class VikunjaError(Exception):
    """A Vikunja API call failed (network, auth, server)."""


def api_base(url: str) -> str:
    """``https://host[/api/v1][/]`` → ``https://host/api/v1``."""
    base = (url or "").strip().rstrip("/")
    if base.endswith("/api/v1"):
        base = base[: -len("/api/v1")]
    return f"{base}/api/v1"


def due_iso(due: date | None) -> str | None:
    """The due date at 09:00 local, RFC 3339 (Vikunja's ``due_date``)."""
    if due is None:
        return None
    return datetime.combine(due, time(DUE_DATETIME_HOUR, 0), tzinfo=dt_util.get_default_time_zone()).isoformat()


def task_priority(status: str) -> int:
    return PRIORITY_URGENT if status in URGENT_STATUSES else PRIORITY_DUE_SOON


def task_description(task: Mapping[str, Any], object_name: str, area_name: str | None, entry_id: str, task_id: str) -> str:
    """HTML body (Vikunja renders descriptions as HTML)."""
    lines = [f"<b>Object:</b> {html.escape(object_name)}"]
    if area_name:
        lines.append(f"<b>Area:</b> {html.escape(area_name)}")
    interval = task.get("interval_days")
    if interval:
        lines.append(f"<b>Every:</b> {int(interval)} days")
    notes = task.get("notes")
    if isinstance(notes, str) and notes.strip():
        lines.append(f"<b>Notes:</b> {html.escape(notes.strip())}")
    lines.append("<i>Managed by Maintenance Supporter — mark this task done to complete it in Home Assistant.</i>")
    lines.append(f"<small>ms:{entry_id}/{task_id}</small>")
    return "".join(f"<p>{line}</p>" for line in lines)


class VikunjaClient:
    """Minimal async Vikunja REST client (bearer token)."""

    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self._session = session
        self._base = api_base(url)
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> tuple[int, Any]:
        try:
            async with self._session.request(
                method,
                f"{self._base}{path}",
                json=payload,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    body = None
        except (TimeoutError, aiohttp.ClientError) as err:
            raise VikunjaError(f"{method} {path}: {type(err).__name__}") from err
        if status == 404:
            return status, None
        if status >= 400:
            # Never log the body verbatim beyond Vikunja's own message field.
            message = body.get("message") if isinstance(body, dict) else None
            raise VikunjaError(f"{method} {path}: HTTP {status}{f' ({message})' if message else ''}")
        return status, body

    async def get_project(self, project_id: int) -> dict[str, Any] | None:
        _, body = await self._request("GET", f"/projects/{int(project_id)}")
        return body if isinstance(body, dict) else None

    async def create_task(self, project_id: int, fields: Mapping[str, Any]) -> dict[str, Any]:
        _, body = await self._request("PUT", f"/projects/{int(project_id)}/tasks", fields)
        if not isinstance(body, dict) or not body.get("id"):
            raise VikunjaError("create task: no id in response")
        return body

    async def get_task(self, task_id: int) -> dict[str, Any] | None:
        _, body = await self._request("GET", f"/tasks/{int(task_id)}")
        return body if isinstance(body, dict) else None

    async def update_task(self, task_id: int, changes: Mapping[str, Any]) -> bool:
        """Read-modify-write: Vikunja's POST /tasks/{id} replaces the whole
        task, a partial body wipes the omitted fields. False when gone."""
        current = await self.get_task(task_id)
        if current is None:
            return False
        await self._request("POST", f"/tasks/{int(task_id)}", {**current, **changes})
        return True


@callback
def settings_changed(hass: HomeAssistant) -> None:
    sync = hass.data.get(DOMAIN, {}).get(VIKUNJA_SYNC_KEY)
    if sync is not None:
        sync.schedule_full_pass()


class VikunjaSync:
    """Owns the Vikunja tasks Maintenance Supporter keeps for due tasks."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, 1, CREDENTIALS_STORE_KEY, private=True)
        self._token: str = ""
        # Serialises reconcile/poll; NEVER held across complete_maintenance
        # (its refresh re-enters async_sync_entry).
        self._lock = asyncio.Lock()
        self._unsub_poll: Any = None
        self._polling = False
        self.last_error: str | None = None
        self.last_poll: str | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        data = await self._store.async_load() or {}
        token = data.get("token")
        self._token = token if isinstance(token, str) else ""
        self._unsub_poll = async_track_time_interval(
            self._hass, self._on_poll_timer, POLL_INTERVAL, name=f"{DOMAIN}_vikunja_poll"
        )

    @callback
    def async_teardown(self) -> None:
        if self._unsub_poll is not None:
            self._unsub_poll()
            self._unsub_poll = None

    # ── configuration ─────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        value = global_option(self._hass, CONF_VIKUNJA_URL)
        return value.strip() if isinstance(value, str) else ""

    @property
    def project_id(self) -> int:
        try:
            return int(global_option(self._hass, CONF_VIKUNJA_PROJECT_ID) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def token_set(self) -> bool:
        return bool(self._token)

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.project_id > 0 and self._token)

    def _client(self) -> VikunjaClient:
        return VikunjaClient(async_get_clientsession(self._hass), self.url, self._token)

    async def async_set_token(self, token: str) -> None:
        self._token = token.strip()
        await self._store.async_save({"token": self._token} if self._token else {})
        self.last_error = None
        self.schedule_full_pass()

    async def async_test(self) -> dict[str, Any]:
        """Check URL + token + project; returns {ok, project_title|error}."""
        if not self.enabled:
            return {"ok": False, "error": "not_configured"}
        try:
            project = await self._client().get_project(self.project_id)
        except VikunjaError as err:
            return {"ok": False, "error": str(err)}
        if project is None:
            return {"ok": False, "error": "project_not_found"}
        return {"ok": True, "project_title": project.get("title")}

    def status(self) -> dict[str, Any]:
        tracked = 0
        for ce in self._object_entries():
            store = getattr(getattr(ce, "runtime_data", None), "store", None)
            if store is not None:
                tracked += sum(1 for tid in (ce.data.get(CONF_TASKS) or {}) if store.get_task_state(tid).get(VIKUNJA_STATE_KEY))
        return {
            "enabled": self.enabled,
            "url": self.url,
            "project_id": self.project_id,
            "token_set": self.token_set,
            "tracked_tasks": tracked,
            "last_error": self.last_error,
            "last_poll": self.last_poll,
        }

    def _object_entries(self) -> list[ConfigEntry]:
        return [ce for ce in self._hass.config_entries.async_entries(DOMAIN) if ce.unique_id != GLOBAL_UNIQUE_ID]

    def _area_name(self, obj: MaintenanceObject | None) -> str | None:
        area_id = getattr(obj, "area_id", None)
        if not area_id:
            return None
        area = ar.async_get(self._hass).async_get_area(area_id)
        return area.name if area is not None else None

    # ── reconcile (called by the coordinator after each refresh) ──────────

    async def async_sync_entry(
        self,
        entry_id: str,
        results: dict[str, TaskResult],
        *,
        store: MaintenanceStore,
        obj: MaintenanceObject,
    ) -> None:
        if not self.enabled:
            return
        async with self._lock:
            try:
                await self._sync_entry_locked(entry_id, results, store, obj)
            except Exception:  # never fail the coordinator refresh; retried next time
                _LOGGER.exception("Vikunja sync for entry %s failed", entry_id)

    async def _sync_entry_locked(
        self, entry_id: str, results: dict[str, TaskResult], store: MaintenanceStore, obj: MaintenanceObject
    ) -> None:
        client = self._client()
        area_name = self._area_name(obj)
        dirty = False
        for task_id, tr in results.items():
            wanted = tr.status in NOTIFIABLE_STATUSES
            record: dict[str, Any] = dict(store.get_task_state(task_id).get(VIKUNJA_STATE_KEY) or {})
            if not wanted and not record:
                continue
            try:
                if not wanted:
                    # Left the due window (completed / skipped / paused …):
                    # keep it in the catalog, checked off.
                    await client.update_task(record["id"], {"done": True})
                    store.update_task_state(task_id, **{VIKUNJA_STATE_KEY: None})
                    dirty = True
                    continue
                title = mirror_summary(obj.name, tr.task, tr.next_event_titles)
                due = due_iso(task_due_date(tr))
                priority = task_priority(tr.status)
                if record:
                    changes: dict[str, Any] = {}
                    if record.get("title") != title:
                        changes["title"] = title
                    if record.get("due") != due:
                        changes["due_date"] = due
                    if record.get("priority") != priority:
                        changes["priority"] = priority
                    if not changes:
                        continue
                    if await client.update_task(record["id"], changes):
                        store.update_task_state(
                            task_id, **{VIKUNJA_STATE_KEY: {**record, "title": title, "due": due, "priority": priority}}
                        )
                        dirty = True
                        continue
                    # Deleted by hand in Vikunja while still due: recreate.
                created = await client.create_task(
                    self.project_id,
                    {
                        "title": title,
                        "description": task_description(tr.task, obj.name, area_name, entry_id, task_id),
                        "due_date": due,
                        "priority": priority,
                    },
                )
                store.update_task_state(
                    task_id,
                    **{VIKUNJA_STATE_KEY: {"id": int(created["id"]), "title": title, "due": due, "priority": priority}},
                )
                dirty = True
                self.last_error = None
            except VikunjaError as err:
                self.last_error = str(err)
                _LOGGER.warning("Vikunja sync of task %s failed: %s (retried next refresh)", task_id, err)
        if dirty:
            store.async_delay_save()

    async def async_forget_task(self, store: MaintenanceStore, task_id: str) -> None:
        """A task is being deleted: check its Vikunja task off (catalog keeps it)."""
        record = store.get_task_state(task_id).get(VIKUNJA_STATE_KEY)
        if not record or not self.enabled:
            return
        async with self._lock:
            try:
                await self._client().update_task(record["id"], {"done": True})
            except VikunjaError as err:
                _LOGGER.warning("Could not close Vikunja task of deleted task %s: %s", task_id, err)
            store.update_task_state(task_id, **{VIKUNJA_STATE_KEY: None})

    # ── poll: done in Vikunja → complete here ─────────────────────────────

    @callback
    def _on_poll_timer(self, _now: Any) -> None:
        self._hass.async_create_background_task(self.async_poll(), name=f"{DOMAIN}_vikunja_poll")

    @callback
    def schedule_full_pass(self) -> None:
        self._hass.async_create_background_task(self.async_poll(), name=f"{DOMAIN}_vikunja_full_pass")

    async def async_poll(self) -> None:
        if not self.enabled or self._polling:
            return
        self._polling = True
        try:
            await self._poll()
            self.last_poll = dt_util.utcnow().isoformat()
        except Exception:  # never crash the timer; next tick retries
            _LOGGER.exception("Vikunja poll failed")
        finally:
            self._polling = False

    async def _poll(self) -> None:
        client = self._client()
        for ce in self._object_entries():
            rd = getattr(ce, "runtime_data", None)
            store: MaintenanceStore | None = getattr(rd, "store", None)
            coordinator: MaintenanceCoordinator | None = getattr(rd, "coordinator", None)
            if store is None or coordinator is None:
                continue
            for task_id in list(ce.data.get(CONF_TASKS) or {}):
                record = store.get_task_state(task_id).get(VIKUNJA_STATE_KEY)
                if not record:
                    continue
                try:
                    remote = await client.get_task(record["id"])
                except VikunjaError as err:
                    self.last_error = str(err)
                    _LOGGER.warning("Vikunja poll: %s", err)
                    return  # server down — try again next tick
                if remote is None:
                    store.update_task_state(task_id, **{VIKUNJA_STATE_KEY: None})
                    store.async_delay_save()
                elif remote.get("done"):
                    # Drop the record FIRST: the completion's refresh re-enters
                    # async_sync_entry and must not close it again.
                    store.update_task_state(task_id, **{VIKUNJA_STATE_KEY: None})
                    store.async_delay_save()
                    await self._complete(ce, coordinator, task_id)
            # Converge (recreate hand-deleted tasks, pick up a new config).
            results = task_results(coordinator)
            if results:
                await self.async_sync_entry(ce.entry_id, results, store=store, obj=coordinator.maintenance_object)

    async def _complete(self, ce: ConfigEntry, coordinator: MaintenanceCoordinator, task_id: str) -> None:
        task_name = (ce.data.get(CONF_TASKS) or {}).get(task_id, {}).get("name", task_id)
        try:
            await coordinator.complete_maintenance(task_id=task_id, notes=COMPLETION_PROVENANCE_NOTES["todo_mirror"], unattended=True, source="todo_mirror")
        except Exception as err:  # noqa: BLE001 — a refused completion must not stall the poll
            _LOGGER.warning(
                "Vikunja: %r was marked done but did not complete here (%s); a new Vikunja task will be created",
                task_name,
                err,
            )
