"""Config flow: where is the relay's state API?"""

from __future__ import annotations

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from aiohttp import ClientError, ClientTimeout
from homeassistant import config_entries
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_POWER_ENTITY

STEP_SCHEMA = vol.Schema(
    {vol.Required(CONF_URL, default="http://127.0.0.1:8124"): cv.string}
)


async def _relay_reachable(hass: HomeAssistant, url: str) -> dict | None:
    """The relay answers /health with {"ok": true} when it is running."""
    session = async_get_clientsession(hass)
    try:
        resp = await session.get(f"{url.rstrip('/')}/health", timeout=ClientTimeout(total=5))
    except (ClientError, TimeoutError):
        return None
    if resp.status != 200:
        return None
    try:
        data = await resp.json(content_type=None)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("ok") else None


class IcommLocalConfigFlow(config_entries.ConfigFlow, domain="icomm_local"):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return IcommLocalOptionsFlow()

    async def async_step_user(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            if await _relay_reachable(self.hass, user_input[CONF_URL]):
                return self.async_create_entry(
                    title="iCOMM relay", data=user_input
                )
            errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="user", data_schema=STEP_SCHEMA, errors=errors
        )


class IcommLocalOptionsFlow(config_entries.OptionsFlow):
    """Optional: a power sensor on the heater's circuit (Emporia, a smart plug).

    The module can keep reporting the same snapshot while the heater runs. An
    independent power reading shows the heater working while the data stands
    still, so the frozen-telemetry repair can be raised sooner.
    """

    async def async_step_init(self, user_input: dict | None = None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_POWER_ENTITY)
        schema = vol.Schema({
            vol.Optional(CONF_POWER_ENTITY, description={"suggested_value": current}):
                selector.EntitySelector(selector.EntitySelectorConfig(
                    domain="sensor", device_class="power")),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
