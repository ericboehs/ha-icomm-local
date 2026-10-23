"""Config flow: where is the relay's state API?"""

from __future__ import annotations

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

STEP_SCHEMA = vol.Schema(
    {vol.Required(CONF_URL, default="http://127.0.0.1:8124"): cv.string}
)


async def _relay_reachable(hass: HomeAssistant, url: str) -> dict | None:
    """The relay answers /health with {"ok": true} when it is running."""
    session = async_get_clientsession(hass)
    try:
        resp = await session.get(f"{url.rstrip('/')}/health", timeout_seconds=5)
    except (ClientError, TimeoutError):
        return None
    if resp.status != 200:
        return None
    data = await resp.json(content_type=None)
    return data if data.get("ok") else None


class IcommLocalConfigFlow(config_entries.ConfigFlow, domain="icomm_local"):
    VERSION = 1

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
