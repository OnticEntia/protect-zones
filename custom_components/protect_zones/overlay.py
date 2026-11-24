"""The zones as shapes to draw over a camera's live picture, for dashboards.

Two websocket commands, for any logged-in user, read-only:
(changing a zone is in edit.py, for administrators)

  protect_zones/overlay             -> the payload below, once
  protect_zones/overlay/subscribe   -> the payload at once, and again whenever
                                       the zone list or a zone's occupancy changes

  {"editable": true | false,
   "cameras": {<Protect camera id>: {"mac", "name", "connected", "aligned", "zones": [
      {"id", "kind": "zone" | "line" | "loiter", "name", "color": "#rrggbb" | null,
       "types": ["person", ...] | null, "dwell": {"person": seconds, ...} | null,
       "points": [[x, y], ...], "occupied": true | false | null, "detected": str | null}]}}}

types: what the zone is set to detect, in Protect's words. dwell: for a
loiter zone, how many seconds each of them must stay before it counts.

connected: false for a camera that is offline (its zones are still listed,
with occupied null). A camera removed from Protect is no longer listed.

aligned: true / false for a camera that can move (is it at its home
position, which is where its zones are taken to have been drawn), null if
that isn't known or the camera can't move. While someone is subscribed it is
asked of Protect every 5 seconds.
points are 0..1 from the top left of the camera's picture. What the fields
mean exactly: zones.py, overlay_payload (which builds it, and is tested).
editable says whether the one asking may change zones (is an administrator).

The commands exist as soon as the integration's code is loaded. While
Protect Zones isn't set up (or is being reloaded) the payload is
{"cameras": {}}; subscriptions stay open across a reload.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
import time
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import DOMAIN
from .stream import protect_api
from .zones import overlay_payload, ptz_aligned

if TYPE_CHECKING:
    from . import ZonesCoordinator
    from .feed import ZoneFeed

_LOGGER = logging.getLogger(__name__)

_KEY = f"{DOMAIN}_overlay"
# Changes within this many seconds go out as one message (someone walking
# through three zones is one update, not six).
COALESCE = 0.25

# "aligned" (is a camera that can move at its home position): asked of
# Protect this often, and only while someone is watching the overlay.
ALIGNED_EVERY = timedelta(seconds=5)
HOME_EVERY = 300  # seconds; the home position itself rarely changes

type Subscriber = Callable[[dict[str, Any]], None]


@callback
def async_get_hub(hass: HomeAssistant) -> OverlayHub:
    if _KEY not in hass.data:
        hass.data[_KEY] = OverlayHub(hass)
    return hass.data[_KEY]


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_overlay)
    websocket_api.async_register_command(hass, ws_overlay_subscribe)


class OverlayHub:
    """Knows the current payload and who wants to hear when it changes."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._coordinator: ZonesCoordinator | None = None
        self._feed: ZoneFeed | None = None
        self._subscribers: list[Subscriber] = []
        self._last: dict[str, Any] | None = None  # what the subscribers were last sent
        self._pending: CALLBACK_TYPE | None = None
        self._aligned: dict[str, bool | None] = {}  # camera mac -> at its home position
        self._aligned_at = 0.0  # when that was read (time.monotonic)
        self._homes: dict[str, tuple[float, Any]] = {}  # camera mac -> (when read, home position)
        self._positions: dict[str, dict[str, Any]] = {}  # camera mac -> where it looks (pan, tilt, zoom steps)
        # called (no arguments) when a camera's position was read and differs
        self.on_position: list[Callable[[], None]] = []
        self._stop_aligned: CALLBACK_TYPE | None = None
        self._reading = asyncio.Lock()

    @callback
    def attach(self, coordinator: ZonesCoordinator, feed: ZoneFeed) -> CALLBACK_TYPE:
        """Protect Zones is set up: this is where the zones and their occupancy
        come from. Call the result when it is unloaded."""
        self._coordinator, self._feed = coordinator, feed
        feed.on_change.append(self.changed)
        stop_listening = coordinator.async_add_listener(self.changed)
        self.changed()

        @callback
        def detach() -> None:
            stop_listening()
            if self.changed in feed.on_change:
                feed.on_change.remove(self.changed)
            if self._coordinator is coordinator:
                self._coordinator = self._feed = None
                self._aligned = {}
            self.changed()

        return detach

    @callback
    def payload(self) -> dict[str, Any]:
        coordinator, feed = self._coordinator, self._feed
        if coordinator is None or feed is None:
            return overlay_payload(None, lambda mac, zone_id: (False, []), False)
        return overlay_payload(coordinator.data, feed.occupancy, coordinator.live, self._aligned.get)

    async def async_read_aligned(self, max_age: float = 0) -> None:
        """Ask Protect whether the cameras that can move (and have zones) are at
        home, unless that was done less than max_age seconds ago."""
        async with self._reading:
            if time.monotonic() - self._aligned_at < max_age:
                return
            coordinator, api = self._coordinator, protect_api(self.hass)
            aligned: dict[str, bool | None] = {}
            positions: dict[str, dict[str, Any]] = {}
            for mac, cam in ((coordinator.data or {}) if coordinator and api else {}).items():
                if not cam.get("ptz") or not cam.get("id") or not any(cam[kind] for kind in ("zone", "line", "loiter")):
                    continue
                if cam.get("connected") is False:
                    continue  # offline: nothing to ask, and not known
                try:
                    known = self._homes.get(mac)
                    if known is None or time.monotonic() - known[0] > HOME_EVERY:
                        known = self._homes[mac] = (time.monotonic(), await api.api_request(f"cameras/{cam['id']}/ptz/home"))
                    position = await api.api_request(f"cameras/{cam['id']}/ptz/position")
                    aligned[mac] = ptz_aligned(known[1], position)
                    steps = position.get("steps") if isinstance(position, dict) else None
                    if isinstance(steps, dict):
                        positions[mac] = {k: steps[k] for k in ("pan", "tilt", "zoom") if k in steps}
                except Exception:  # noqa: BLE001 - then we don't know
                    _LOGGER.debug("Couldn't read where %s is looking", cam.get("name"), exc_info=True)
                    aligned[mac] = None
            self._aligned_at = time.monotonic()
            for mac in [m for m in self._homes if m not in aligned]:
                del self._homes[mac]  # removed, or back later: read its home position afresh
            if aligned != self._aligned:
                self._aligned = aligned
                self.changed()
            if positions != self._positions:
                self._positions = positions
                for listener in list(self.on_position):
                    listener()

    @callback
    def aligned(self, mac: str) -> bool | None:
        """Is this camera at its home position, as far as was last read (None: not known)."""
        return self._aligned.get(mac)

    @callback
    def position(self, mac: str) -> dict[str, Any]:
        """Where this camera looks, in its own steps ({"pan", "tilt", "zoom"}), as far as was last read."""
        return self._positions.get(mac) or {}

    @callback
    def _read_aligned_soon(self, _now: Any = None) -> None:
        if not self._reading.locked():
            self.hass.async_create_background_task(self.async_read_aligned(), "protect zones camera position")

    @callback
    def subscribe(self, subscriber: Subscriber) -> CALLBACK_TYPE:
        """subscriber(payload) is called on every change (not right away)."""
        self._subscribers.append(subscriber)
        if self._stop_aligned is None:  # someone is watching: keep "aligned" up to date
            self._stop_aligned = async_track_time_interval(self.hass, self._read_aligned_soon, ALIGNED_EVERY)
            self._read_aligned_soon()

        @callback
        def unsubscribe() -> None:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)
            if not self._subscribers and self._pending:
                self._pending()
                self._pending = None
            if not self._subscribers and self._stop_aligned:
                self._stop_aligned()
                self._stop_aligned = None
                self._aligned = {}  # nobody keeps it up to date now
                self._aligned_at = 0.0

        return unsubscribe

    @callback
    def changed(self) -> None:
        """Something that may show in the payload changed: tell the subscribers, soon."""
        if self._subscribers and self._pending is None:
            self._pending = async_call_later(self.hass, COALESCE, self._send)

    @callback
    def _send(self, _now: Any = None) -> None:
        self._pending = None
        payload = self.payload()
        if payload == self._last:
            return  # e.g. the zone list was read again and is the same
        self._last = payload
        for subscriber in list(self._subscribers):
            try:
                subscriber(payload)
            except Exception:  # noqa: BLE001 - one closed connection shouldn't stop the others
                _LOGGER.debug("Couldn't send the zone overlay to a subscriber", exc_info=True)


def _for(connection: websocket_api.ActiveConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """The payload as this connection gets it."""
    return {"editable": bool(connection.user.is_admin), **payload}


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/overlay"})
@websocket_api.async_response
async def ws_overlay(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hub = async_get_hub(hass)
    await hub.async_read_aligned(max_age=ALIGNED_EVERY.total_seconds())
    connection.send_result(msg["id"], _for(connection, hub.payload()))


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/overlay/subscribe"})
@callback
def ws_overlay_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hub = async_get_hub(hass)

    @callback
    def send(payload: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], _for(connection, payload)))

    connection.subscriptions[msg["id"]] = hub.subscribe(send)
    connection.send_result(msg["id"])
    send(hub.payload())
