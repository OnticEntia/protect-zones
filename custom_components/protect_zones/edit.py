"""Changing a zone in Protect, from a dashboard. Administrators only.

  protect_zones/zone/update  {"mac", "kind": "zone" | "line" | "loiter", "zone_id",
                              "points"?: [[x, y], ...], "name"?, "color"?: "#rrggbb"}
  protect_zones/zone/undo    {"mac"}

Both answer with the camera's entry of the overlay payload (overlay.py) as it
is in Protect afterwards, and the overlay's subscribers hear of it at once.

THIS WRITES TO PROTECT; it is the only place in this integration that does.
How a change is made, so that nothing else is disturbed:

  1. one change at a time per camera (a lock);
  2. the camera is read fresh from Protect;
  3. zones.py (zone_change / zone_undo, covered by the tests) works out the
     new zone lists: only the fields given, only of that zone (and of the
     zones Protect shows as one with it), everything else exactly as read;
  4. the lists as they were are saved to disk (the last 10 changes per
     camera), so the change can be undone;
  5. only the lists that change are sent: PATCH cameras/<id>
     {"smartDetectZones": [...]};
  6. the camera is read again, and that is what we show and answer with.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .overlay import async_get_hub
from .stream import protect_api
from .zones import ZoneEditError, parse_cameras, remember, zone_change, zone_undo

_LOGGER = logging.getLogger(__name__)

_KEY = f"{DOMAIN}_editor"
STORAGE_KEY = f"{DOMAIN}.zone_history"
STORAGE_VERSION = 1


@callback
def async_get_editor(hass: HomeAssistant) -> ZoneEditor:
    if _KEY not in hass.data:
        hass.data[_KEY] = ZoneEditor(hass)
    return hass.data[_KEY]


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_zone_update)
    websocket_api.async_register_command(hass, ws_zone_undo)


class ZoneEditor:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._locks: dict[str, asyncio.Lock] = {}
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        # camera mac -> the changes made, oldest first: what zone_change calls a snapshot, plus "at"
        self._history: dict[str, list[dict[str, Any]]] | None = None

    async def update(self, mac: str, kind: str, zone_id: int, **fields: Any) -> dict[str, Any]:
        """Change a zone's points, name, color, object types and / or dwell times. Returns the camera's overlay entry."""
        coordinator, api, camera_id = self._camera(mac)
        async with self._locks.setdefault(mac, asyncio.Lock()):
            change = zone_change(await self._read(api, camera_id, mac), kind, zone_id, **fields)
            if change is not None:
                body, snapshot = change
                history = await self._load()
                history[mac] = remember(history.get(mac, []), {"at": dt_util.utcnow().isoformat(), **snapshot})
                await self._store.async_save({"cameras": history})
                await self._write(api, camera_id, body)
            return await self._show(coordinator, api, camera_id, mac, change[0] if change else None)

    async def undo(self, mac: str) -> dict[str, Any]:
        """Put back the zones the last change touched. Returns the camera's overlay entry."""
        coordinator, api, camera_id = self._camera(mac)
        async with self._locks.setdefault(mac, asyncio.Lock()):
            history = await self._load()
            if not history.get(mac):
                raise ZoneEditError("nothing_to_undo", "There is no change to undo for this camera")
            try:
                body = zone_undo(await self._read(api, camera_id, mac), history[mac][-1])
            except ZoneEditError as err:
                if err.code in ("zone_not_found", "bad_history"):  # it never will work: don't get stuck on it
                    history[mac].pop()
                    await self._store.async_save({"cameras": history})
                raise
            if body is not None:
                await self._write(api, camera_id, body)
            history[mac].pop()
            await self._store.async_save({"cameras": history})
            return await self._show(coordinator, api, camera_id, mac, body)

    # ------------------------------------------------------------ internals

    def _camera(self, mac: str) -> tuple[Any, Any, str]:
        entries = self.hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise ZoneEditError("not_loaded", "Protect Zones isn't set up")
        coordinator = entries[0].runtime_data
        camera = (coordinator.data or {}).get(mac)
        if not camera or not camera.get("id"):
            raise ZoneEditError("camera_not_found", "No such camera")
        api = protect_api(self.hass)
        if api is None:
            raise ZoneEditError("protect_not_loaded", "The UniFi Protect integration isn't loaded")
        return coordinator, api, camera["id"]

    async def _load(self) -> dict[str, list[dict[str, Any]]]:
        if self._history is None:
            stored = await self._store.async_load()
            cameras = stored.get("cameras") if isinstance(stored, dict) else None
            self._history = cameras if isinstance(cameras, dict) else {}
        return self._history

    async def _read(self, api: Any, camera_id: str, mac: str) -> dict[str, Any]:
        try:
            camera = await api.api_request(f"cameras/{camera_id}")
        except Exception as err:  # noqa: BLE001
            raise ZoneEditError("protect_error", f"Couldn't read the camera from Protect: {err}") from err
        if not isinstance(camera, dict) or camera.get("mac") != mac:
            raise ZoneEditError("unexpected_answer", "Protect gave an unexpected answer for the camera")
        return camera

    async def _write(self, api: Any, camera_id: str, body: dict[str, list[Any]]) -> None:
        try:
            await api.api_request(f"cameras/{camera_id}", method="patch", json=body)
        except Exception as err:  # noqa: BLE001
            raise ZoneEditError("protect_error", f"Protect didn't accept the change: {err}") from err

    async def _show(
        self, coordinator: Any, api: Any, camera_id: str, mac: str, sent: dict[str, list[Any]] | None
    ) -> dict[str, Any]:
        """Read the camera again, show that everywhere, and return its overlay entry."""
        camera = await self._read(api, camera_id, mac)
        for key, zones in (sent or {}).items():
            if camera.get(key) != zones:
                _LOGGER.warning(
                    "Protect accepted a zone change for %s but its %s are not as sent; showing what Protect has",
                    camera.get("name") or mac,
                    key,
                )
        try:
            fresh = parse_cameras([camera])
        except ValueError:
            fresh = {}
        if mac in fresh:
            # entities follow a new name, the overlay's subscribers hear of it
            coordinator.async_set_updated_data({**(coordinator.data or {}), **fresh})
        entry = async_get_hub(self.hass).payload()["cameras"].get(str(camera_id))
        if entry is None:
            raise ZoneEditError("unexpected_answer", "The camera has no zones any more")
        return entry


_UPDATE = {
    vol.Required("type"): f"{DOMAIN}/zone/update",
    vol.Required("mac"): str,
    vol.Required("kind"): vol.In(["zone", "line", "loiter"]),
    # not "id": that is the websocket message's own number
    vol.Required("zone_id"): int,
    vol.Optional("points"): list,
    vol.Optional("name"): str,
    vol.Optional("color"): str,
    # what a detection zone detects, ["person", "vehicle", ...]
    vol.Optional("types"): [str],
    # what a loiter zone detects and after how many seconds, {"person": 5, ...}
    vol.Optional("dwell"): {str: vol.Any(int, float)},
}


@websocket_api.require_admin
@websocket_api.websocket_command(_UPDATE)
@websocket_api.async_response
async def ws_zone_update(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    fields = {key: msg[key] for key in ("points", "name", "color", "types", "dwell") if key in msg}
    try:
        entry = await async_get_editor(hass).update(msg["mac"], msg["kind"], msg["zone_id"], **fields)
    except ZoneEditError as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    connection.send_result(msg["id"], entry)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/zone/undo", vol.Required("mac"): str})
@websocket_api.async_response
async def ws_zone_undo(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    try:
        entry = await async_get_editor(hass).undo(msg["mac"])
    except ZoneEditError as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    connection.send_result(msg["id"], entry)
