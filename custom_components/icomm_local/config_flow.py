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

from .const import CONF_NETWORK_SWITCH, CONF_POWER_ENTITY

STEP_SCHEMA = vol.Schema(
    {vol.Required(CONF_URL, default="http://127.0.0.1:8124"): cv.string}
)


def normalize_url(url: str) -> str:
    """The same relay however it was typed: no trailing slash, any case."""
    return url.strip().rstrip("/").lower()


async def _relay_reachable(hass: HomeAssistant, url: str) -> dict | None:
    """The relay answers /health with {"ok": true} when it is running."""
    session = async_get_clientsession(hass)
    try:
        async with session.get(f"{url.rstrip('/')}/health",
                               timeout=ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except (ClientError, TimeoutError, ValueError):
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
            url = normalize_url(user_input[CONF_URL])
            # Entries from before unique IDs have none: match them by URL too.
            if any(normalize_url(e.data.get(CONF_URL) or "") == url
                   for e in self._async_current_entries()):
                return self.async_abort(reason="already_configured")
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()
            if await _relay_reachable(self.hass, user_input[CONF_URL]):
                return self.async_create_entry(
                    title="iCOMM relay", data=user_input
                )
            errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="user", data_schema=STEP_SCHEMA, errors=errors
        )


class IcommLocalOptionsFlow(config_entries.OptionsFlow):
    """Optional helpers, each an existing entity:

    - a power sensor on the heater's circuit (Emporia, a smart plug). The module
      can keep reporting the same snapshot while the heater runs; an independent
      power reading shows it working, so the frozen-data repair comes sooner;
    - a switch that cuts the heater's network access while off (the UniFi
      integration's block-client switch). When the relay says the heater's
      cloud side is down, it's turned off for 90 s and on again.
    """

    async def async_step_init(self, user_input: dict | None = None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        # Not self.config_entry: HA sets that itself only from 2024.11, and
        # setting it on newer HA is deprecated. The handler is the entry ID.
        entry = self.hass.config_entries.async_get_entry(self.handler)
        options = entry.options if entry else {}
        schema = vol.Schema({
            vol.Optional(CONF_POWER_ENTITY,
                         description={"suggested_value": options.get(CONF_POWER_ENTITY)}):
                selector.EntitySelector(selector.EntitySelectorConfig(
                    domain="sensor", device_class="power")),
            vol.Optional(CONF_NETWORK_SWITCH,
                         description={"suggested_value": options.get(CONF_NETWORK_SWITCH)}):
                selector.EntitySelector(selector.EntitySelectorConfig(domain="switch")),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
