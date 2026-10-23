"""iCOMM Local: sensors from the icomm_local relay running beside HA."""

from __future__ import annotations

from datetime import timedelta

from aiohttp import ClientError
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, POLL_SECONDS, TIMEOUT_SECONDS
from .services import async_setup_services


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    url = entry.data[CONF_URL].rstrip("/")
    session = async_get_clientsession(hass)

    async def fetch() -> dict:
        try:
            resp = await session.get(f"{url}/state", timeout_seconds=TIMEOUT_SECONDS)
        except (TimeoutError, ClientError) as err:
            raise UpdateFailed(f"relay unreachable: {err}") from err
        if resp.status != 200:
            raise UpdateFailed(f"relay returned {resp.status}")
        data = await resp.json(content_type=None)
        # A stale-but-reachable relay is not a failure; freshness is exposed
        # as the `fresh` field so the sensor can show unavailable instead.
        return data

    coordinator = DataUpdateCoordinator(
        hass,
        logger=__import__("logging").getLogger(__name__),
        name="icomm_local",
        update_method=fetch,
        update_interval=timedelta(seconds=POLL_SECONDS),
    )
    # The services need to know where the relay is; keeping this on the coordinator
    # avoids a second lookup path that could disagree with this one.
    coordinator.relay_url = url
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    if not hass.services.has_service(DOMAIN, "set_setpoint"):
        await async_setup_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    unload = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    if unload:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            # Last relay gone: services would only raise, so unregister them.
            for service in ("set_setpoint", "set_mode", "cancel_writes"):
                hass.services.async_remove(DOMAIN, service)
    return unload
