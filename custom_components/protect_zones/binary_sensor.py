"""One "occupied" sensor per camera detection zone: on while Protect reports
something in the zone, off once it has left or the detection has ended.
Pushed live from Protect (feed.py).

And one "at home position" sensor per camera that can move and has zones: on
while it looks where it looks by itself, which is where its zones are taken
to have been drawn (overlay.py asks Protect every few seconds). Off means it
has turned away, to follow someone or by hand, and its zones don't match the
picture then.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from . import ProtectZonesConfigEntry
from .const import DOMAIN
from .entity import ZoneEntity, zone_syncer
from .feed import occupancy_signal
from .overlay import async_get_hub


async def async_setup_entry(
    hass: HomeAssistant, entry: ProtectZonesConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    sync = zone_syncer(hass, entry, async_add_entities, ZoneOccupied, ("zone",))
    sync()
    entry.async_on_unload(entry.runtime_data.async_add_listener(sync))

    coordinator = entry.runtime_data
    at_home: set[str] = set()  # the cameras that have their sensor

    @callback
    def sync_at_home() -> None:
        new = []
        for mac, cam in (coordinator.data or {}).items():
            if mac in at_home or not cam.get("ptz") or not cam.get("id"):
                continue
            if any(cam[kind] for kind in ("zone", "line", "loiter")):
                at_home.add(mac)
                new.append(CameraAtHome(mac, cam["name"]))
        if new:
            async_add_entities(new)

    sync_at_home()
    entry.async_on_unload(coordinator.async_add_listener(sync_at_home))


class ZoneOccupied(ZoneEntity, BinarySensorEntity):
    _tail = "_occupied"
    _name_tail = " occupied"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_is_on = False

    def __init__(self, coordinator, mac: str, camera: str, kind: str, zone_id: int, zone_name: str) -> None:
        super().__init__(coordinator, mac, camera, kind, zone_id, zone_name)
        self._detected: list[str] = []

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, occupancy_signal(self._mac, self._kind, self._zone_id), self._changed)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # what the detections in the zone have seen (a detection can see more
        # than one kind of thing, so this can list more than is in the zone)
        return {"zone": self._zone_name, "camera": self._camera, "detected": self._detected}

    @callback
    def _changed(self, occupied: bool, detected: list[str]) -> None:
        self._attr_is_on = occupied
        self._detected = detected
        self.async_write_ha_state()


class CameraAtHome(BinarySensorEntity):
    """Is a camera that can move looking where its zones were drawn."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "At home position"
    _attr_icon = "mdi:crosshairs-gps"

    def __init__(self, mac: str, camera: str) -> None:
        self._mac = mac
        self._attr_unique_id = f"camera_{mac}_at_home"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"zones_{mac}")},
            name=f"Protect Zones {camera}",
            manufacturer="Ubiquiti",
            model="UniFi Protect detection zones",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        hub = async_get_hub(self.hass)
        # being subscribed is what keeps the position being read
        self.async_on_remove(hub.subscribe(self._changed))
        hub.on_position.append(self._moved)
        self.async_on_remove(lambda: hub.on_position.remove(self._moved))

    @property
    def is_on(self) -> bool | None:
        """None (unknown) while Protect doesn't say where the camera looks."""
        return async_get_hub(self.hass).aligned(self._mac)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # where it looks, in the camera's own steps (about 100 to a degree):
        # tells following someone from sitting still away from home
        return dict(async_get_hub(self.hass).position(self._mac))

    @callback
    def _changed(self, _payload: dict[str, Any]) -> None:
        self.async_write_ha_state()

    @callback
    def _moved(self) -> None:
        self.async_write_ha_state()
