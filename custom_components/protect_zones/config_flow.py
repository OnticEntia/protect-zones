"""One-click setup: uses the existing UniFi Protect integration's connection."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN
from .stream import protect_api


class ProtectZonesFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if protect_api(self.hass) is None:
            return self.async_abort(reason="protect_not_loaded")
        if user_input is None:
            return self.async_show_form(step_id="user")
        return self.async_create_entry(title="Protect Zones", data={})
