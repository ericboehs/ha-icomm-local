"""Services so writes are usable from automations instead of curl.

Everything here queues; nothing delivers. The appliance asks for commands on its
own schedule, so a service call returns as soon as the write is accepted and the
sensor entities report whether it was ever confirmed.
"""

from __future__ import annotations

import inspect
import logging

import voluptuous as vol
from aiohttp import ClientError, ClientTimeout
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import service as service_helper
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, MAX_VACATION_DAYS, MODES, TIMEOUT_SECONDS
from .heaters import dsn_of, relay_path

_LOGGER = logging.getLogger(__name__)

# Every service takes an optional target (water heater entities, their devices,
# or an area): required once there is more than one heater (#18).
TARGET = cv.TARGET_SERVICE_FIELDS
SET_SETPOINT_SCHEMA = vol.Schema({**TARGET, vol.Required("temperature"): vol.Coerce(float)})
def _days_only_for_vacation(data: dict) -> dict:
    if "days" in data and data["mode"] != "vacation":
        raise vol.Invalid("days applies only to vacation")
    return data


SET_MODE_SCHEMA = vol.All(
    vol.Schema({
        **TARGET,
        vol.Required("mode"): vol.In(sorted(MODES)),
        vol.Optional("days"): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_VACATION_DAYS)),
    }),
    _days_only_for_vacation,
)


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


async def _targets(hass: HomeAssistant, call: ServiceCall) -> list[tuple]:
    """(coordinator, dsn) for each heater the call names; with no target, the
    only heater there is -- several, and the call has to say which."""
    if any(key in call.data for key in TARGET):
        extract = service_helper.async_extract_entity_ids
        # Home Assistant dropped the hass argument (it rides on the call now),
        # and older releases made it a coroutine.
        takes_hass = "hass" in inspect.signature(extract).parameters
        ids = extract(hass, call) if takes_hass else extract(call)
        if inspect.isawaitable(ids):
            ids = await ids
        registry = er.async_get(hass)
        found = []
        for entity_id in sorted(ids):
            entity = registry.async_get(entity_id)
            coordinator = (hass.data.get(DOMAIN, {}).get(entity.config_entry_id)
                           if entity and entity.platform == DOMAIN
                           and entity.domain == "water_heater" else None)
            if coordinator is not None:
                found.append((coordinator, dsn_of(entity.unique_id)))
        if not found:
            raise HomeAssistantError("the target has no iCOMM water heater in it")
        return found
    coordinator = _coordinator(hass)
    if len(coordinator.order) > 1:
        raise HomeAssistantError(
            "there are several water heaters: choose one (or more) as the target")
    return [(coordinator, coordinator.primary)]


async def _post_each(hass: HomeAssistant, call: ServiceCall, path: str,
                     payload: dict | None, key: str) -> ServiceResponse:
    targets = await _targets(hass, call)
    replies = {}
    for coordinator, dsn in targets:
        body = await _post(hass, relay_path(coordinator, dsn, path), payload, coordinator)
        replies[dsn or "water_heater"] = body.get(key, {})
    # One heater: the same response as before multi-heater support.
    return {key: next(iter(replies.values()))} if len(replies) == 1 else {"heaters": replies}


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
        response = await _post_each(hass, call, "/write", {"setpoint": temperature}, "queued")
        _LOGGER.debug("queued setpoint %s: %s", temperature, response)
        # Queued is not applied: telemetry confirms, and it may take minutes.
        return response

    async def set_mode(call: ServiceCall) -> ServiceResponse:
        mode = call.data["mode"]
        payload = {"mode": mode}
        if "days" in call.data:
            payload["days"] = call.data["days"]
        response = await _post_each(hass, call, "/write", payload, "queued")
        _LOGGER.debug("queued mode %s: %s", mode, response)
        return response

    async def cancel_writes(call: ServiceCall) -> ServiceResponse:
        return await _post_each(hass, call, "/write/cancel", None, "cancelled")

    hass.services.async_register(
        DOMAIN, "set_setpoint", set_setpoint, schema=SET_SETPOINT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, "set_mode", set_mode, schema=SET_MODE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, "cancel_writes", cancel_writes, schema=vol.Schema(TARGET),
        supports_response=SupportsResponse.OPTIONAL,
    )
