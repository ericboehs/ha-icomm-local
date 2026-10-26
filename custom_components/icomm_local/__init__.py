"""iCOMM Local: sensors from the icomm_local relay running beside HA."""

from __future__ import annotations

import asyncio
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
from .heaters import (
    device_info,
    migrate_to_dsn,
    multi_capable,
    primary_dsn,
    update_device_pages,
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


def _heater_issue_id(kind: str, entry_id: str, dsn: str | None, primary: str | None) -> str:
    """The primary heater keeps the single-heater issue IDs; others add their DSN."""
    return f"{kind}_{entry_id}" if dsn is None or dsn == primary else f"{kind}_{entry_id}_{dsn}"


def _frozen_issue(hass: HomeAssistant, entry, data: dict, issue_id: str | None = None,
                  heater: str = "iCOMM Water Heater") -> None:
    """Reports arrive, but their contents haven't changed: the data is frozen."""
    issue_id = issue_id or f"telemetry_frozen_{entry.entry_id}"
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
            "heater": heater,
            "minutes": str(round((unchanged or 0) / 60)),
            "power": (f" Meanwhile your power sensor shows the heater drawing {round(power)} W."
                      if running else ""),
            "fix": _frozen_fix(data.get("reporting_mask") or {}),
        })


def _frozen_fix(mask: dict) -> str:
    """What to do about frozen data: the relay re-arms it itself, or the app."""
    if mask.get("renew"):
        return ("The relay re-arms reporting automatically: after "
                f"{round((mask.get('after_s') or 1800) / 60)} minutes frozen it re-sends the "
                "app's reporting request over the local connection "
                f"({mask.get('sent', 0)} sent since the relay started), which brings live data "
                "back within a few minutes. If this warning stays, **open the A.O. Smith app** "
                "and view the water heater.")
    return ("To fix it now, **open the A.O. Smith app** and view the water heater: that "
            "re-arms reporting within seconds. To have the relay do it automatically, turn on "
            "the add-on's `reporting_mask_renew` option.")


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
        await _fetch_heaters(data)
        return data

    async def _fetch_one(dsn: str) -> dict | None:
        try:
            resp = await session.get(f"{url}/heaters/{dsn}/state",
                                     timeout=ClientTimeout(total=TIMEOUT_SECONDS))
            body = await resp.json(content_type=None) if resp.status == 200 else None
        except (TimeoutError, ClientError, ValueError):
            return None
        return body if isinstance(body, dict) else None

    async def _fetch_heaters(data: dict) -> None:
        """Every heater's own state (relay 0.7+): /state is the primary's. One
        failing heater only makes its own entities unavailable."""
        primary = primary_dsn(data)
        others = [d for d in (data.get("heaters") or [])
                  if isinstance(d, str) and d != primary] if multi_capable(data) else []
        states = dict(zip(others, await asyncio.gather(*(_fetch_one(d) for d in others)),
                          strict=True))
        coordinator.primary = primary
        coordinator.order = ([primary] if primary else []) + others
        coordinator.heaters = {d: s for d, s in states.items() if s}

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
    coordinator.primary, coordinator.order, coordinator.heaters = None, [], {}
    await coordinator.async_config_entry_first_refresh()
    # Before the platforms: the tank renames (keyed by the old unique IDs), then
    # the move to per-heater keys, so entities come up under their new IDs with
    # their old entity IDs and history.
    _migrate_entity_ids(hass)
    if coordinator.primary:
        migrate_to_dsn(hass, entry, coordinator.primary)
    else:
        # An older relay, or one that hasn't met the heater yet: single-heater
        # keys for now, and a reload to move them once the DSN is known.
        def _dsn_arrived() -> None:
            if coordinator.last_update_success and coordinator.primary:
                hass.config_entries.async_schedule_reload(entry.entry_id)

        entry.async_on_unload(coordinator.async_add_listener(_dsn_arrived))
    heater_issues: set[str] = set()

    def _each_heater():
        for dsn in list(coordinator.order) or [None]:
            data = (coordinator.data if dsn in (None, coordinator.primary)
                    else coordinator.heaters.get(dsn)) or {}
            yield dsn, data, device_info(coordinator, dsn)["name"]

    def _update_stale_issue() -> None:
        current: set[str] = set()
        for dsn, data, name in _each_heater():
            issue_id = _heater_issue_id("stale_telemetry", entry.entry_id, dsn,
                                        coordinator.primary)
            if coordinator.last_update_success and is_stale(data):
                lan = data.get("lan") or {}
                current.add(issue_id)
                ir.async_create_issue(
                    hass, DOMAIN, issue_id, is_fixable=False, is_persistent=False,
                    severity=ir.IssueSeverity.WARNING, translation_key="stale_telemetry",
                    translation_placeholders={
                        "heater": name,
                        "age": str(round((data.get("last_telemetry_age") or 0) / 60)),
                        "lan": "up" if lan.get("session") else "down",
                        "health": str((data.get("health") or {}).get("state")),
                    })
            else:
                ir.async_delete_issue(hass, DOMAIN, issue_id)
        _forget(current, "stale_telemetry")

    def _forget(current: set[str], kind: str) -> None:
        """A heater that left: its issues go too."""
        for issue_id in {i for i in heater_issues if i.startswith(kind)} - current:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            heater_issues.discard(issue_id)
        heater_issues.update(current)

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
        if not coordinator.last_update_success:
            return
        current: set[str] = set()
        for dsn, data, name in _each_heater():
            issue_id = _heater_issue_id("telemetry_frozen", entry.entry_id, dsn,
                                        coordinator.primary)
            current.add(issue_id)
            _frozen_issue(hass, entry, data, issue_id, name)
        _forget(current, "telemetry_frozen")

    entry.async_on_unload(coordinator.async_add_listener(_update_frozen_issue))
    _update_frozen_issue()

    def _update_dns_issue() -> None:
        if coordinator.last_update_success:
            _dns_issue(hass, entry.entry_id, coordinator.data or {})

    entry.async_on_unload(coordinator.async_add_listener(_update_dns_issue))
    _update_dns_issue()
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    entry.async_on_unload(lambda: _firmware_issues(hass, entry.entry_id, {}, firmware_issues))

    def _drop_heater_issues() -> None:
        for issue_id in heater_issues:
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    entry.async_on_unload(_drop_heater_issues)
    _update_firmware_issues()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    if not hass.services.has_service(DOMAIN, "set_setpoint"):
        await async_setup_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    def _update_device_registry() -> None:
        if coordinator.last_update_success:
            update_device_pages(hass, coordinator, entry.entry_id)

    entry.async_on_unload(coordinator.async_add_listener(_update_device_registry))
    _update_device_registry()
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
