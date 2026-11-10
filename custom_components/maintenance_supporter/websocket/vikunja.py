"""Fork (tyler5136): admin websocket commands for the native Vikunja sync.

The token goes in through ``vikunja/set_token`` and never comes back out —
``vikunja/status`` reports only whether one is set.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from ..const import DOMAIN
from ..helpers.vikunja_sync import VIKUNJA_SYNC_KEY, VikunjaSync


def _sync(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> VikunjaSync | None:
    sync = hass.data.get(DOMAIN, {}).get(VIKUNJA_SYNC_KEY)
    if sync is None:
        connection.send_error(msg["id"], "not_loaded", "Vikunja sync is not set up")
    return sync


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/vikunja/set_token", vol.Required("token"): vol.All(str, vol.Length(max=512))}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_vikunja_set_token(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Store (or clear, with "") the Vikunja API token; replies with the test result."""
    sync = _sync(hass, connection, msg)
    if sync is None:
        return
    await sync.async_set_token(msg["token"])
    connection.send_result(msg["id"], {"token_set": sync.token_set, **await sync.async_test()})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/vikunja/status"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_vikunja_status(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    sync = _sync(hass, connection, msg)
    if sync is None:
        return
    connection.send_result(msg["id"], {**sync.status(), "test": await sync.async_test()})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/vikunja/sync_now"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_vikunja_sync_now(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Run the poll + reconcile pass now (instead of waiting for the timer)."""
    sync = _sync(hass, connection, msg)
    if sync is None:
        return
    await sync.async_poll()
    connection.send_result(msg["id"], sync.status())
