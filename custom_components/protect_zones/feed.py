"""Turns Protect's live updates into zone signals.

  - something is in a zone  (a smart detection whose zonesStatus / linesStatus /
                             loiterStatus, or "zone:<camera>:<id>" label, names it)
  - a zone is occupied or empty again
                            (zonesStatus says enter / moving, then leave, or the
                             detection ends)
  - the zone entities can (no longer) be trusted
                            (the live stream dropped or came back, or the zone
                             list hasn't been read for too long)

The event and binary sensor entities listen for these signals. What the
updates mean is worked out in zones.py (ZoneTracker), which needs no Home
Assistant and is covered by the tests.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN
from .stream import async_get_stream
from .zones import DETECTED, OBJECT_TYPES, ZoneTracker  # noqa: F401 - re-exported for the entities

if TYPE_CHECKING:
    from . import ZonesCoordinator

SWEEP_EVERY = timedelta(seconds=10)
# sent (no arguments) when the entities may have become available or unavailable
AVAILABILITY_SIGNAL = f"{DOMAIN}_availability"

_LOGGER = logging.getLogger(__name__)


def _brief(data: dict[str, Any]) -> dict[str, Any]:
    """The parts of an event update that say what is where (for the debug log)."""
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    brief = {k: data[k] for k in ("type", "camera", "smartDetectTypes", "start", "end", "labels") if k in data}
    brief.update({k: meta[k] for k in ("zonesStatus", "linesStatus", "loiterStatus") if k in meta})
    thumbs = meta.get("detectedThumbnails")
    if isinstance(thumbs, list):
        brief["thumbnails"] = [
            {k: t[k] for k in ("type", "labels", "attributes") if k in t} for t in thumbs if isinstance(t, dict)
        ]
    return brief


def zone_signal(mac: str, kind: str, zone_id: int) -> str:
    return f"{DOMAIN}_zone_{mac}_{kind}_{zone_id}"


def occupancy_signal(mac: str, kind: str, zone_id: int) -> str:
    return f"{DOMAIN}_occupancy_{mac}_{kind}_{zone_id}"


class ZoneFeed:
    def __init__(self, hass: HomeAssistant, coordinator: ZonesCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self._tracker = ZoneTracker(
            coordinator.camera_by_id, self._send_hit, self._send_occupancy, zone_types=coordinator.zone_types
        )
        self._was_live: bool | None = None
        # called (no arguments) when a zone's occupancy or "can we be trusted" changed
        self.on_change: list[Callable[[], None]] = []
        self._unsubs: list[Any] = []

    def start(self) -> None:
        self._unsubs = [
            async_get_stream(self.hass).subscribe(self._handle, self._disconnected, self._connected),
            async_track_time_interval(self.hass, self._sweep, SWEEP_EVERY),
        ]
        self._availability()

    @callback
    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    @callback
    def _handle(self, action: dict[str, Any], data: Any) -> None:
        if action.get("modelKey") == "event" and isinstance(data, dict):
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("event %s %s: %s", action.get("action"), action.get("id"), _brief(data))
            self._tracker.detection(action.get("id") or data.get("id"), data)

    @callback
    def _send_hit(self, mac: str, kind: str, zone_id: int, event_type: str, attrs: dict[str, Any]) -> None:
        async_dispatcher_send(self.hass, zone_signal(mac, kind, zone_id), event_type, attrs)

    @callback
    def _send_occupancy(self, mac: str, zone_id: int, occupied: bool, seen: list[str]) -> None:
        async_dispatcher_send(self.hass, occupancy_signal(mac, "zone", zone_id), occupied, seen)
        self._changed()

    @callback
    def occupancy(self, mac: str, zone_id: int) -> tuple[bool, list[str]]:
        """Is this detection zone occupied now, and what is in it."""
        return self._tracker.occupancy(mac, zone_id)

    @callback
    def _changed(self) -> None:
        for listener in list(self.on_change):
            listener()

    @callback
    def _sweep(self, _now=None) -> None:
        self._tracker.sweep()
        self._availability()

    @callback
    def _disconnected(self) -> None:
        """The stream dropped: we no longer hear who leaves."""
        self._tracker.clear_occupancy()
        self._availability()

    @callback
    def _connected(self) -> None:
        self._availability()

    @callback
    def _availability(self) -> None:
        """Tell the entities if "can we be trusted" changed."""
        live = self.coordinator.live
        if live != self._was_live:
            self._was_live = live
            async_dispatcher_send(self.hass, AVAILABILITY_SIGNAL)
            self._changed()
