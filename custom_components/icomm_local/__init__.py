"""iCOMM Local: sensors from the icomm_local relay running beside HA."""

from __future__ import annotations

from datetime import timedelta

from aiohttp import ClientError, ClientTimeout
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, POLL_SECONDS, TIMEOUT_SECONDS, is_stale
from .services import async_setup_services

PLATFORMS = ["sensor", "water_heater"]

# 0.4.13 relabelled the tank sensors once their order was measured: the sensor
# named "middle" is the bottom-most and the one named "bottom" sits above it.
# Installs still on the old default entity IDs move to the new ones, in this
# order so the second rename finds its target free. HA carries the recorder
# history with a registry rename. Customised IDs are left alone.
ENTITY_ID_MIGRATIONS = (
    ("icomm_local_water_bottom",
     "sensor.icomm_water_heater_water_tank_bottom", "sensor.icomm_water_heater_water_tank_lower"),
    ("icomm_local_water_mid",
     "sensor.icomm_water_heater_water_tank_middle", "sensor.icomm_water_heater_water_tank_bottom"),
)


def _migrate_entity_ids(hass: HomeAssistant) -> None:
    registry = er.async_get(hass)
    for unique_id, old, new in ENTITY_ID_MIGRATIONS:
        current = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if current == old and registry.async_get(new) is None:
            registry.async_update_entity(old, new_entity_id=new)


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    url = entry.data[CONF_URL].rstrip("/")
    session = async_get_clientsession(hass)

    async def fetch() -> dict:
        try:
            resp = await session.get(f"{url}/state", timeout=ClientTimeout(total=TIMEOUT_SECONDS))
        except (TimeoutError, ClientError) as err:
            raise UpdateFailed(f"relay unreachable: {err}") from err
        if resp.status != 200:
            raise UpdateFailed(f"relay returned {resp.status}")
        try:
            data = await resp.json(content_type=None)
        except ValueError as err:
            raise UpdateFailed(f"relay returned a non-JSON reply: {err}") from err
        # A stale-but-reachable relay is not a failure: entities keep their
        # last-known values and a Repairs warning says the data is old.
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
    issue_id = f"stale_telemetry_{entry.entry_id}"

    def _update_stale_issue() -> None:
        data = coordinator.data or {}
        if coordinator.last_update_success and is_stale(data):
            lan = data.get("lan") or {}
            ir.async_create_issue(
                hass, DOMAIN, issue_id, is_fixable=False, is_persistent=False,
                severity=ir.IssueSeverity.WARNING, translation_key="stale_telemetry",
                translation_placeholders={
                    "age": str(round((data.get("last_telemetry_age") or 0) / 60)),
                    "lan": "up" if lan.get("session") else "down",
                    "health": str((data.get("health") or {}).get("state")),
                })
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    entry.async_on_unload(coordinator.async_add_listener(_update_stale_issue))
    _update_stale_issue()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    if not hass.services.has_service(DOMAIN, "set_setpoint"):
        await async_setup_services(hass)
    _migrate_entity_ids(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    unload = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload:
        ir.async_delete_issue(hass, DOMAIN, f"stale_telemetry_{entry.entry_id}")
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            # Last relay gone: services would only raise, so unregister them.
            for service in ("set_setpoint", "set_mode", "cancel_writes"):
                hass.services.async_remove(DOMAIN, service)
    return unload
