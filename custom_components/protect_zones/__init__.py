"""Protect Zones: a companion to the UniFi Protect integration.

For every detection zone, line and loiter zone of a camera:
  event          — fires when something is detected in it (person, vehicle, ...)
and for every detection zone also:
  binary sensor  — on while something is in it
and for dashboards, the zones as shapes to draw over a camera's picture
(overlay.py).

Both are pushed live from Protect's update stream, so automations can use
them as triggers. It only reads, with one exception: an administrator can
change a zone's outline, name and colour from a dashboard (edit.py).

The connection to that stream (stream.py) is shared with the other Protect
companions (Protect Rules, Protect Detections).

It reuses the logged-in client of HA's own UniFi Protect integration, so it
holds no credentials of its own. It uses Protect's internal API (the one the
Protect app uses), which is undocumented: a Protect update could change it.
"""

from __future__ import annotations

from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .feed import ZoneFeed
from . import edit, overlay
from .overlay import async_get_hub
from .stream import async_get_stream, protect_api
from .zones import parse_cameras

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.EVENT, Platform.BINARY_SENSOR]
SCAN_INTERVAL = timedelta(minutes=5)
# The zone list counts as stale (and the entities as unavailable) when it
# couldn't be read for this long: three reads in a row, so one failed read
# doesn't take the entities down.
STALE_AFTER = 3 * SCAN_INTERVAL.total_seconds() + 30

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type ProtectZonesConfigEntry = ConfigEntry[ZonesCoordinator]


class ZonesCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Reads each camera's zones every few minutes.

    data is {camera mac: {"id", "name", "ptz", "zone": [...], "line": [...], "loiter": [...], "missing": (...)}}
    where each zone is {"id", "name", "color", "points"} and "missing" names the kinds Protect
    said nothing about for that camera (zones.py, parse_cameras).
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL, config_entry=entry)
        self.reads = 0  # successful reads so far
        self.read_at: float | None = None  # when the last one was (time.monotonic)

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        api = protect_api(self.hass)
        if api is None:
            raise UpdateFailed("UniFi Protect integration isn't loaded")
        try:
            cameras = await api.api_request("cameras")
        except Exception as err:
            raise UpdateFailed(f"Couldn't read the cameras' zones: {err}") from err
        try:
            parsed = parse_cameras(cameras)
        except ValueError as err:
            raise UpdateFailed(str(err)) from err
        self.reads += 1
        self.read_at = time.monotonic()
        return parsed

    @property
    def live(self) -> bool:
        """Can the zone entities be trusted: the live stream is connected and
        the zone list was read not long ago."""
        fresh = self.read_at is not None and time.monotonic() - self.read_at < STALE_AFTER
        return fresh and async_get_stream(self.hass).connected

    def camera_by_id(self, camera_id: str | None) -> tuple[str, str] | None:
        """(mac, name) of the camera with this Protect id."""
        for mac, cam in (self.data or {}).items():
            if cam["id"] == camera_id:
                return mac, cam["name"]
        return None

    def zone_types(self, mac: str, kind: str, zone_id: int) -> tuple[str, ...] | None:
        """The object types this zone is set to detect, None if that isn't known."""
        for zone in ((self.data or {}).get(mac) or {}).get(kind) or []:
            if zone["id"] == zone_id:
                return zone.get("types")
        return None


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    overlay.async_register_commands(hass)  # the zones as shapes, for dashboards
    edit.async_register_commands(hass)  # changing a zone from a dashboard (administrators)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ProtectZonesConfigEntry) -> bool:
    if protect_api(hass) is None:
        raise ConfigEntryNotReady("Waiting for the UniFi Protect integration")
    coordinator = ZonesCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    feed = ZoneFeed(hass, coordinator)
    feed.start()
    entry.async_on_unload(feed.stop)
    entry.async_on_unload(async_get_hub(hass).attach(coordinator, feed))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ProtectZonesConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
