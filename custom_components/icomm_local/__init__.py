"""iCOMM Local: sensors from the icomm_local relay running beside HA."""

from __future__ import annotations

import time
from datetime import timedelta

from aiohttp import ClientError, ClientTimeout
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_POWER_ENTITY,
    DOMAIN,
    FROZEN_POWER_W,
    FROZEN_WITH_POWER_S,
    POLL_SECONDS,
    TIMEOUT_SECONDS,
    is_stale,
)
from .services import async_setup_services

PLATFORMS = ["sensor", "water_heater"]

# The tank sensors were relabelled twice as their meaning was measured. 0.4.13:
# "middle" is the bottom thermistor, so "bottom" became "lower". 0.5.2: "lower"
# is not a thermistor but the controller's blend of top and bottom, so it is
# the "average". Installs still on an old default entity ID move along the
# chain, in this order so each rename finds its target free. HA carries the
# recorder history with a registry rename. Customised IDs are left alone.
ENTITY_ID_MIGRATIONS = (
    ("icomm_local_water_bottom",
     "sensor.icomm_water_heater_water_tank_bottom", "sensor.icomm_water_heater_water_tank_lower"),
    ("icomm_local_water_mid",
     "sensor.icomm_water_heater_water_tank_middle", "sensor.icomm_water_heater_water_tank_bottom"),
    ("icomm_local_water_bottom",
     "sensor.icomm_water_heater_water_tank_lower", "sensor.icomm_water_heater_water_tank_average"),
)


FIRMWARE_TARGETS = {"module": "Wi-Fi module", "host_mcu": "water heater controller"}


def firmware_issue_id(entry_id: str, key: str) -> str:
    return f"firmware_held_{entry_id}_" + "".join(
        c if c.isalnum() else "_" for c in key.lower())


def _firmware_issues(hass: HomeAssistant, entry_id: str, data: dict, raised: set[str]) -> None:
    """One fixable repair per update the relay is holding; gone once it isn't."""
    firmware = data.get("firmware") or {}
    current: set[str] = set()
    for offer in firmware.get("held") or []:
        key = str(offer.get("key"))
        issue_id = firmware_issue_id(entry_id, key)
        current.add(issue_id)
        size = offer.get("size")
        placeholders = {
            "target": FIRMWARE_TARGETS.get(offer.get("type"), str(offer.get("type"))),
            "version": str(offer.get("version")),
            "size": f"{round(size / 1024)} KB" if isinstance(size, int) else "unknown size",
            "host": str(offer.get("url_host") or "unknown"),
            "agent": str(firmware.get("agent_version") or "unknown"),
        }
        ir.async_create_issue(
            hass, DOMAIN, issue_id, is_fixable=True, is_persistent=False,
            severity=ir.IssueSeverity.WARNING, translation_key="firmware_held",
            translation_placeholders=placeholders,
            data={"entry_id": entry_id, "key": key, "placeholders": placeholders})
    for issue_id in raised - current:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    raised.clear()
    raised.update(current)


def _tls_issue(hass: HomeAssistant, entry_id: str, data: dict) -> None:
    """Ayla's certificate fails the relay's check: cloud sync is down, quietly."""
    issue_id = f"upstream_certificate_{entry_id}"
    tls = data.get("upstream_tls") or {}
    if not tls.get("failing"):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    last_ok = tls.get("last_ok")
    ir.async_create_issue(
        hass, DOMAIN, issue_id, is_fixable=False, is_persistent=False,
        severity=ir.IssueSeverity.ERROR, translation_key="upstream_certificate",
        translation_placeholders={
            "error": str(tls.get("last_error") or "unknown"),
            "failures": str(tls.get("failures") or 0),
            "issues_url": "https://github.com/ericboehs/ha-icomm-local/issues",
            "since": (f"{round((time.time() - last_ok) / 60)} minutes ago"
                      if isinstance(last_ok, (int, float)) else "not since the relay started"),
        })


def _power_w(hass: HomeAssistant, entity_id: str | None) -> float | None:
    state = hass.states.get(entity_id) if entity_id else None
    try:
        value = float(state.state)
    except (AttributeError, TypeError, ValueError):
        return None
    return value * 1000 if state.attributes.get("unit_of_measurement") == "kW" else value


def _frozen_issue(hass: HomeAssistant, entry, data: dict) -> None:
    """Reports arrive, but their contents haven't changed: the data is frozen."""
    issue_id = f"telemetry_frozen_{entry.entry_id}"
    unchanged = data.get("contents_unchanged_s")
    power = _power_w(hass, entry.options.get(CONF_POWER_ENTITY))
    running = (power is not None and power > FROZEN_POWER_W
               and isinstance(unchanged, (int, float)) and unchanged >= FROZEN_WITH_POWER_S)
    if not data.get("fresh") or not (data.get("contents_stale") or running):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass, DOMAIN, issue_id, is_fixable=False, is_persistent=False,
        severity=ir.IssueSeverity.WARNING, translation_key="telemetry_frozen",
        translation_placeholders={
            "minutes": str(round((unchanged or 0) / 60)),
            "power": (f" Meanwhile your power sensor shows the heater drawing {round(power)} W."
                      if running else ""),
        })


def _dns_issue(hass: HomeAssistant, entry_id: str, data: dict) -> None:
    """dns_server is on but can't steer: port 53 or 443 is taken on the host."""
    issue_id = f"dns_server_failed_{entry_id}"
    dns = data.get("dns") or {}
    problem = dns.get("error") or dns.get("https_error") if dns.get("enabled") else None
    if not problem:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass, DOMAIN, issue_id, is_fixable=False, is_persistent=False,
        severity=ir.IssueSeverity.ERROR, translation_key="dns_server_failed",
        translation_placeholders={"problem": str(problem)})


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
    firmware_issues: set[str] = set()

    def _update_firmware_issues() -> None:
        if coordinator.last_update_success:
            _firmware_issues(hass, entry.entry_id, coordinator.data or {}, firmware_issues)

    entry.async_on_unload(coordinator.async_add_listener(_update_firmware_issues))

    def _update_tls_issue() -> None:
        if coordinator.last_update_success:
            _tls_issue(hass, entry.entry_id, coordinator.data or {})

    entry.async_on_unload(coordinator.async_add_listener(_update_tls_issue))
    _update_tls_issue()

    def _update_frozen_issue() -> None:
        if coordinator.last_update_success:
            _frozen_issue(hass, entry, coordinator.data or {})

    entry.async_on_unload(coordinator.async_add_listener(_update_frozen_issue))
    _update_frozen_issue()

    def _update_dns_issue() -> None:
        if coordinator.last_update_success:
            _dns_issue(hass, entry.entry_id, coordinator.data or {})

    entry.async_on_unload(coordinator.async_add_listener(_update_dns_issue))
    _update_dns_issue()
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    entry.async_on_unload(lambda: _firmware_issues(hass, entry.entry_id, {}, firmware_issues))
    _update_firmware_issues()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    if not hass.services.has_service(DOMAIN, "set_setpoint"):
        await async_setup_services(hass)
    _migrate_entity_ids(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _options_updated(hass: HomeAssistant, entry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    unload = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload:
        ir.async_delete_issue(hass, DOMAIN, f"stale_telemetry_{entry.entry_id}")
        ir.async_delete_issue(hass, DOMAIN, f"upstream_certificate_{entry.entry_id}")
        ir.async_delete_issue(hass, DOMAIN, f"telemetry_frozen_{entry.entry_id}")
        ir.async_delete_issue(hass, DOMAIN, f"dns_server_failed_{entry.entry_id}")
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            # Last relay gone: services would only raise, so unregister them.
            for service in ("set_setpoint", "set_mode", "cancel_writes"):
                hass.services.async_remove(DOMAIN, service)
    return unload
