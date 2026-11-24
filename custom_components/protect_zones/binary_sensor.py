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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from . import ProtectZonesConfigEntry, ZonesCoordinator
from .const import DOMAIN
from .entity import ZoneEntity, remove_entity, zone_syncer
from .feed import AVAILABILITY_SIGNAL, occupancy_signal
from .overlay import async_get_hub
from .zones import gone

AT_HOME = ("camera_", "_at_home")  # the at-home sensor's unique id is the mac between these


async def async_setup_entry(
    hass: HomeAssistant, entry: ProtectZonesConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    sync = zone_syncer(hass, entry, async_add_entities, ZoneOccupied, ("zone",))
    sync()
    entry.async_on_unload(entry.runtime_data.async_add_listener(sync))

    coordinator = entry.runtime_data
    at_home: set[str] = set()  # the cameras that have their sensor
    missing: dict[tuple[str], int] = {}  # sensors with no camera for them -> for how many reads
    last_read = [0]

    @callback
    def sync_at_home() -> None:
        cameras = coordinator.data
        if cameras is None or not coordinator.last_update_success:
            return
        wanted = {
            (mac,): cam["name"]
            for mac, cam in cameras.items()
            if cam.get("ptz") and cam.get("id") and any(cam[kind] for kind in ("zone", "line", "loiter"))
        }
        new = [CameraAtHome(coordinator, mac, name) for (mac,), name in wanted.items() if mac not in at_home]
        at_home.update(mac for (mac,) in wanted)
        if new:
            async_add_entities(new)

        # The camera was removed from Protect, or has no zones left: remove
        # the sensor too, once two reads in a row agree (as for the zones).
        if coordinator.reads == last_read[0]:
            return
        last_read[0] = coordinator.reads
        registered = {}
        for ent in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
            uid = ent.unique_id
            if uid.startswith(AT_HOME[0]) and uid.endswith(AT_HOME[1]):
                registered[(uid[len(AT_HOME[0]) : -len(AT_HOME[1])],)] = ent.entity_id
        for key in gone(registered, wanted, cameras, missing):
            at_home.discard(key[0])
            remove_entity(hass, registered[key], key[0])

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

    def __init__(self, coordinator: ZonesCoordinator, mac: str, camera: str) -> None:
        self._coordinator = coordinator
        self._mac = mac
        self._attr_unique_id = f"{AT_HOME[0]}{mac}{AT_HOME[1]}"
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
        self.async_on_remove(async_dispatcher_connect(self.hass, AVAILABILITY_SIGNAL, self._moved))

    @property
    def available(self) -> bool:
        """Not while the camera is offline or Protect can't be heard."""
        return self._coordinator.camera_live(self._mac)

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
