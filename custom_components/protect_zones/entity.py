"""Shared by the per-zone entities (event.py, binary_sensor.py).

Zone entities follow Protect: a zone or camera renamed there is renamed here
(the entity id stays, so automations keep working), and a zone or camera
deleted there is removed here.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ProtectZonesConfigEntry, ZonesCoordinator
from .const import DOMAIN
from .feed import AVAILABILITY_SIGNAL
from .zones import zone_key

KIND_SUFFIX = {"zone": "", "line": " line", "loiter": " loiter zone"}

type ZoneKey = tuple[str, str, int]  # camera mac, kind (zone / line / loiter), zone id


class ZoneEntity(Entity):
    """One zone, line or loiter zone of a camera."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _tail = ""  # unique id ending, to tell a zone's entities apart
    _name_tail = ""

    def __init__(
        self, coordinator: ZonesCoordinator, mac: str, camera: str, kind: str, zone_id: int, zone_name: str
    ) -> None:
        self._coordinator = coordinator
        self._mac = mac
        self._kind = kind
        self._zone_id = zone_id
        self._camera = camera
        self._zone_name = zone_name
        self._attr_unique_id = f"zone_{mac}_{kind}_{zone_id}{self._tail}"
        self._attr_name = self._label(zone_name)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"zones_{mac}")},
            name=f"Protect Zones {camera}",
            manufacturer="Ubiquiti",
            model="UniFi Protect detection zones",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Not while the live stream is down or the zone list is stale: then
        we can't tell what happens in the zone."""
        return self._coordinator.live

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, AVAILABILITY_SIGNAL, self._availability_changed))

    @callback
    def _availability_changed(self) -> None:
        self.async_write_ha_state()

    def _label(self, zone_name: str) -> str:
        return f"{zone_name}{KIND_SUFFIX[self._kind]}{self._name_tail}"

    @callback
    def follow(self, camera: str, zone_name: str) -> None:
        """Take over a new zone or camera name from Protect."""
        if self.hass is None:
            return  # still being added
        if zone_name != self._zone_name:
            self._zone_name = zone_name
            self._attr_name = self._label(zone_name)
            er.async_get(self.hass).async_update_entity(self.entity_id, original_name=self._attr_name)
            self.async_write_ha_state()
        if camera != self._camera:
            self._camera = camera
            devices = dr.async_get(self.hass)
            if device := devices.async_get_device(identifiers={(DOMAIN, f"zones_{self._mac}")}):
                devices.async_update_device(device.id, name=f"Protect Zones {camera}")


def zone_syncer(
    hass: HomeAssistant,
    entry: ProtectZonesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    entity_class: type[ZoneEntity],
    kinds: tuple[str, ...],
) -> Callable[[], None]:
    """Returns a callback (run it on every coordinator update) that keeps one
    `entity_class` entity per zone of the given kinds."""
    coordinator = entry.runtime_data
    entities: dict[ZoneKey, ZoneEntity] = {}
    # zones Protect no longer lists -> how many reads in a row they were missing
    missing: dict[ZoneKey, int] = {}
    last_read = [0]
    tail = entity_class._tail  # noqa: SLF001

    @callback
    def sync() -> None:
        cameras = coordinator.data
        if cameras is None or not coordinator.last_update_success:
            return  # nothing read yet, or Protect didn't answer
        current = {
            (mac, kind, zone["id"]): (cam["name"], zone["name"])
            for mac, cam in cameras.items()
            for kind in kinds
            for zone in cam[kind]
        }
        new = []
        for key, (camera, zone_name) in current.items():
            if (ent := entities.get(key)) is None:
                ent = entities[key] = entity_class(coordinator, key[0], camera, key[1], key[2], zone_name)
                new.append(ent)
            else:
                ent.follow(camera, zone_name)
        if new:
            async_add_entities(new)

        # Deleted in Protect (the zone, or its camera): remove it here too, once
        # two reads in a row agree, so one odd answer can't wipe entities. A
        # camera whose answer left this kind of zone out altogether (rather
        # than listing none) says nothing either way: keep its entities.
        if coordinator.reads == last_read[0]:
            return
        last_read[0] = coordinator.reads
        registry = er.async_get(hass)
        registered = {}
        for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
            # (an "…_occupied" id is not taken for the plain one, or the other way round)
            if (key := zone_key(ent.unique_id, tail)) is not None:
                registered[key] = ent.entity_id
        for key in [k for k in missing if k in current or k not in registered]:
            missing.pop(key)
        for key, entity_id in registered.items():
            if key in current:
                continue
            if key[0] in cameras and key[1] in cameras[key[0]]["missing"]:
                continue
            missing[key] = missing.get(key, 0) + 1
            if missing[key] < 2:
                continue
            missing.pop(key)
            entities.pop(key, None)
            registry.async_remove(entity_id)
            devices = dr.async_get(hass)
            device = devices.async_get_device(identifiers={(DOMAIN, f"zones_{key[0]}")})
            if device and not er.async_entries_for_device(registry, device.id, include_disabled_entities=True):
                devices.async_remove_device(device.id)

    return sync
