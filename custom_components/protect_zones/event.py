"""One event per camera zone, line and loiter zone: fires when Protect
detects something in it. Pushed live (feed.py)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ProtectZonesConfigEntry
from .entity import ZoneEntity, zone_syncer
from .feed import DETECTED, OBJECT_TYPES, zone_signal

KIND_ICON = {"zone": "mdi:vector-square", "line": "mdi:vector-line", "loiter": "mdi:timer-sand"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ProtectZonesConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    sync = zone_syncer(hass, entry, async_add_entities, ZoneDetected, ("zone", "line", "loiter"))
    sync()
    entry.async_on_unload(entry.runtime_data.async_add_listener(sync))


class ZoneDetected(ZoneEntity, EventEntity):
    """The event type is what Protect saw (person, vehicle, ...)."""

    _attr_event_types = [*OBJECT_TYPES.values(), DETECTED]

    def __init__(self, coordinator, mac: str, camera: str, kind: str, zone_id: int, zone_name: str) -> None:
        super().__init__(coordinator, mac, camera, kind, zone_id, zone_name)
        self._attr_icon = KIND_ICON[kind]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, zone_signal(self._mac, self._kind, self._zone_id), self._detected)
        )

    @callback
    def _detected(self, event_type: str, attrs: dict[str, Any]) -> None:
        self._trigger_event(event_type, {"zone": self._zone_name, "kind": self._kind, **attrs})
        self.async_write_ha_state()
