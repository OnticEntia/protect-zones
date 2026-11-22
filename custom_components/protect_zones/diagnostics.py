"""Download diagnostics (Settings > Devices & services > Protect Zones > ...):
what this integration knows, and the cameras' zones exactly as Protect sends
them, to check our reading of them against. No credentials are included.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.core import HomeAssistant

from . import ProtectZonesConfigEntry
from .overlay import async_get_hub
from .stream import async_get_stream, protect_api
from .zones import ZONE_KINDS

# camera fields that could tell where a camera that can move is looking
_POSITION = re.compile("ptz|preset|patrol|home|position|pan|tilt|zoom", re.IGNORECASE)
# Read-only (GET) requests. ptz/home is the home position (preset slot -1,
# which the preset list leaves out); zones.py compares it with ptz/position.
_PTZ_READS = ("ptz/preset", "ptz/position", "ptz/home")


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ProtectZonesConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    result: dict[str, Any] = {
        "stream_connected": async_get_stream(hass).connected,
        "live": coordinator.live,
        "zone_list_reads": coordinator.reads,
        "catalogue": coordinator.data,
        "overlay": async_get_hub(hass).payload(),
    }
    api = protect_api(hass)
    if api is None:
        result["protect"] = "the UniFi Protect integration isn't loaded"
        return result
    try:
        cameras = await api.api_request("cameras")
    except Exception as err:  # noqa: BLE001
        result["protect"] = f"couldn't read the cameras: {err}"
        return result
    raw = []
    for cam in cameras if isinstance(cameras, list) else []:
        if not isinstance(cam, dict):
            continue
        flags = cam.get("featureFlags") if isinstance(cam.get("featureFlags"), dict) else {}
        channels = cam.get("channels") if isinstance(cam.get("channels"), list) else []
        item: dict[str, Any] = {
            "id": cam.get("id"),
            "mac": cam.get("mac"),
            "name": cam.get("name"),
            "type": cam.get("type"),
            "is_ptz": flags.get("isPtz"),
            "picture": [
                {"name": c.get("name"), "width": c.get("width"), "height": c.get("height")}
                for c in channels
                if isinstance(c, dict)
            ],
            "zones": {key: cam.get(key, "(key absent)") for _, key in ZONE_KINDS},
            "position_fields": {k: v for k, v in cam.items() if _POSITION.search(k)},
            "position_flags": {k: v for k, v in flags.items() if _POSITION.search(k)},
        }
        if flags.get("isPtz") and cam.get("id"):
            for path in _PTZ_READS:
                try:
                    item[path] = await api.api_request(f"cameras/{cam['id']}/{path}")
                except Exception as err:  # noqa: BLE001
                    item[path] = f"failed: {err}"
        raw.append(item)
    result["protect"] = raw
    return result
