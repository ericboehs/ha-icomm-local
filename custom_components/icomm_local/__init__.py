"""iCOMM Local: sensors from the icomm_local relay running beside HA."""

from __future__ import annotations

from datetime import timedelta

from aiohttp import ClientError
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, POLL_SECONDS


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    url = entry.data[CONF_URL].rstrip("/")
    session = async_get_clientsession(hass)

    async def fetch() -> dict:
        try:
            resp = await session.get(f"{url}/state", timeout_seconds=5)
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
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    unload = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    if unload:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload
