"""Repairs: allow a firmware update the relay is holding.

Ignoring the repair keeps the update held. Allowing it tells the relay to let
that exact update through; Ayla re-sends a held command on the next poll.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .services import _post


class AllowFirmwareFlow(RepairsFlow):
    def __init__(self, data: dict) -> None:
        self._data = data

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="confirm", data_schema=vol.Schema({}),
                description_placeholders=self._data.get("placeholders") or {})
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._data.get("entry_id"))
        if coordinator is None:
            return self.async_abort(reason="not_loaded")
        try:
            await _post(self.hass, "/firmware/allow", {"key": self._data["key"]},
                        coordinator=coordinator)
        except HomeAssistantError:
            return self.async_abort(reason="relay_refused")
        await coordinator.async_request_refresh()
        return self.async_create_entry(data={})


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str,
                                data: dict | None) -> RepairsFlow:
    return AllowFirmwareFlow(data or {})
