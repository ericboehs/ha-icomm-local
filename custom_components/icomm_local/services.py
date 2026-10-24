"""Services so writes are usable from automations instead of curl.

Everything here queues; nothing delivers. The appliance asks for commands on its
own schedule, so a service call returns as soon as the write is accepted and the
sensor entities report whether it was ever confirmed.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from aiohttp import ClientError, ClientTimeout
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, MODES, TIMEOUT_SECONDS

_LOGGER = logging.getLogger(__name__)

SET_SETPOINT_SCHEMA = vol.Schema({vol.Required("temperature"): vol.Coerce(float)})
SET_MODE_SCHEMA = vol.Schema({vol.Required("mode"): vol.In(sorted(MODES))})


def _coordinator(hass: HomeAssistant):
    """The relay this integration is talking to.

    One relay per config entry is expected; with several, the automation has to say
    which, so refuse rather than silently picking one and writing to a tank.
    """
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("icomm_local is not set up")
    if len(entries) > 1:
        raise HomeAssistantError(
            "more than one icomm_local relay is configured; this build cannot "
            "tell them apart yet"
        )
    return next(iter(entries.values()))


async def _post(hass: HomeAssistant, path: str, payload: dict | None = None,
                coordinator=None) -> dict:
    coordinator = coordinator or _coordinator(hass)
    session = async_get_clientsession(hass)
    url = f"{coordinator.relay_url}{path}"
    try:
        async with session.post(url, json=payload or {},
                                timeout=ClientTimeout(total=TIMEOUT_SECONDS)) as resp:
            try:
                body = await resp.json(content_type=None)
            except ValueError:
                # A proxy's HTML 502 or a relay mid-restart: say so plainly.
                body = None
            if not isinstance(body, dict):
                raise HomeAssistantError(f"relay returned {resp.status} with no JSON body")
            if resp.status != 200:
                raise HomeAssistantError(body.get("error") or f"relay returned {resp.status}")
            return body
    except (ClientError, TimeoutError) as err:
        raise HomeAssistantError(f"relay unreachable at {url}: {err}") from err


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register the icomm_local services once per HA instance."""

    async def set_setpoint(call: ServiceCall) -> ServiceResponse:
        temperature = call.data["temperature"]
        body = await _post(hass, "/write", {"setpoint": temperature})
        _LOGGER.debug("queued setpoint %s: %s", temperature, body)
        # Queued is not applied: telemetry confirms, and it may take minutes.
        return {"queued": body.get("queued", {})}

    async def set_mode(call: ServiceCall) -> ServiceResponse:
        mode = call.data["mode"]
        body = await _post(hass, "/write", {"mode": mode})
        _LOGGER.debug("queued mode %s: %s", mode, body)
        return {"queued": body.get("queued", {})}

    async def cancel_writes(call: ServiceCall) -> ServiceResponse:
        body = await _post(hass, "/write/cancel")
        return {"cancelled": body.get("cancelled", {})}

    hass.services.async_register(
        DOMAIN, "set_setpoint", set_setpoint, schema=SET_SETPOINT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, "set_mode", set_mode, schema=SET_MODE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, "cancel_writes", cancel_writes, schema=vol.Schema({}),
        supports_response=SupportsResponse.OPTIONAL,
    )
