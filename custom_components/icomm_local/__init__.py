"""iCOMM Local: sensors from the icomm_local relay running beside HA."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

import aiohttp
from aiohttp import ClientError, ClientTimeout
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_DSN,
    CONF_NUMBERS,
    CONF_POWER_ENTITY,
    DOMAIN,
    FROZEN_POWER_W,
    FROZEN_WITH_POWER_S,
    POLL_SECONDS,
    TIMEOUT_SECONDS,
    is_stale,
)
from .heaters import (
    heater_name,
    heater_state,
    migrate_to_dsn,
    multi_capable,
    primary_dsn,
    rekey,
    retire,
    update_device_pages,
    valid_dsn,
)
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

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


def _cloud_idle_issue(hass: HomeAssistant, data: dict, issue_id: str,
                      heater: str = "iCOMM Water Heater") -> None:
    """The relay says the heater's cloud side stopped (health code cloud_idle):
    HA is fine over Local Connect, Ayla and the app get nothing."""
    health = data.get("health") or {}
    if health.get("code") != "cloud_idle":
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    quiet = (health.get("evidence") or {}).get("cloud_quiet_s") or 0
    ir.async_create_issue(
        hass, DOMAIN, issue_id, is_fixable=False, is_persistent=False,
        severity=ir.IssueSeverity.WARNING, translation_key="cloud_idle",
        translation_placeholders={"heater": heater, "minutes": str(round(quiet / 60))})


def _frozen_fix(mask: dict) -> str:
    """What to do about frozen data: the relay re-arms it itself, or the app."""
    if mask.get("renew"):
        return ("The relay tries to re-arm reporting itself: after "
                f"{round((mask.get('after_s') or 1800) / 60)} minutes frozen it re-sends the "
                "app's reporting request over the local connection "
                f"({mask.get('sent', 0)} sent since the relay started). That has brought live "
                "data back within minutes, but not every time. Until then, power and operating "
                "state show as unknown. If this warning stays, **open the A.O. Smith app** "
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
        # The DSN this entry was migrated to stands in while the relay can't
        # name one (its data reset, reinstalled, downgraded): falling back to
        # single-heater keys then would register every entity a second time.
        stored, reported = valid_dsn(entry.data.get(CONF_DSN)), primary_dsn(data)
        listed = data.get("heaters") if multi_capable(data) else []
        if (reported and stored and reported != stored and stored not in listed
                and reported not in coordinator.numbers):     # new, not a promoted secondary
            # The heater was replaced: the old one is gone from the relay
            # entirely. Its entities and device become the new one's, by a
            # (re)load that re-keys them before anything is added under the
            # new DSN -- never as a second device beside the dead one.
            coordinator.replaced = (stored, reported)
            if coordinator.ready:
                return                    # the reload does it; keep things as they are
            coordinator.numbers[reported] = coordinator.numbers.pop(stored, 1)
        primary = reported or stored
        others = [d for d in listed if valid_dsn(d) and d != primary]
        states = dict(zip(others, await asyncio.gather(*(_fetch_one(d) for d in others)),
                          strict=True))
        coordinator.primary = primary
        # No primary DSN yet but other heaters listed: the primary's /state
        # still has its (single-heater-keyed) entities, first.
        coordinator.order = ([primary] if primary else [None] if others else []) + others
        coordinator.heaters = {d: s for d, s in states.items() if s}
        _remember(primary, coordinator.order)

    def _remember(primary: str | None, order: list[str]) -> None:
        """Keep the primary's DSN and each heater's number in the entry."""
        numbers = dict(coordinator.numbers)
        for dsn in order:
            if dsn is None or dsn in numbers:
                continue
            taken = set(numbers.values())
            if dsn == primary and 1 not in taken:
                numbers[dsn] = 1
            else:
                numbers[dsn] = min(n for n in range(2, len(taken) + 3) if n not in taken)
        coordinator.numbers = numbers
        wanted = {CONF_DSN: primary or entry.data.get(CONF_DSN), CONF_NUMBERS: numbers}
        if any(entry.data.get(k) != v for k, v in wanted.items()):
            hass.config_entries.async_update_entry(entry, data={**entry.data, **wanted})

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
    coordinator.options = dict(entry.options)
    coordinator.ready, coordinator.replaced = False, None

    def reload_once() -> None:
        """One reload, which migrates or re-keys before any entity is added."""
        reload_once.done = True
        _schedule_reload(hass, entry.entry_id)

    reload_once.done = False
    coordinator.primary, coordinator.order, coordinator.heaters = None, [], {}
    coordinator.numbers = {k: v for k, v in (entry.data.get(CONF_NUMBERS) or {}).items()
                           if valid_dsn(k) and isinstance(v, int)}
    await coordinator.async_config_entry_first_refresh()
    # Before the platforms: the tank renames (keyed by the old unique IDs), then
    # the move to per-heater keys, so entities come up under their new IDs with
    # their old entity IDs and history.
    _migrate_entity_ids(hass)
    for entity_id in retire(hass, entry):
        _LOGGER.warning("removed %s: it was the energy capacity shown as a temperature "
                        "(see the Energy capacity sensor)", entity_id)
    if coordinator.replaced:
        rekey(hass, entry, *coordinator.replaced)
        coordinator.replaced = None
    if coordinator.primary:
        migrate_to_dsn(hass, entry, coordinator.primary)

        def _replaced() -> None:
            if coordinator.replaced and not reload_once.done:
                reload_once()

        entry.async_on_unload(coordinator.async_add_listener(_replaced))
    else:
        # An older relay, or one that hasn't met the heater yet: single-heater
        # keys for now, and a reload to move them once the DSN is known.
        def _dsn_arrived() -> None:
            if (coordinator.last_update_success and coordinator.primary
                    and not reload_once.done):
                reload_once()

        entry.async_on_unload(coordinator.async_add_listener(_dsn_arrived))
    coordinator.ready = True
    heater_issues: set[str] = set()

    def _each_heater():
        for dsn in list(coordinator.order) or [None]:
            data = (coordinator.data if dsn in (None, coordinator.primary)
                    else coordinator.heaters.get(dsn)) or {}
            yield dsn, data, heater_name(coordinator, dsn)

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

    def _update_cloud_idle_issue() -> None:
        if not coordinator.last_update_success:
            return
        current: set[str] = set()
        for dsn, data, name in _each_heater():
            issue_id = _heater_issue_id("cloud_idle", entry.entry_id, dsn, coordinator.primary)
            current.add(issue_id)
            _cloud_idle_issue(hass, data, issue_id, name)
        _forget(current, "cloud_idle")

    entry.async_on_unload(coordinator.async_add_listener(_update_cloud_idle_issue))
    _update_cloud_idle_issue()

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


def _schedule_reload(hass: HomeAssistant, entry_id: str) -> None:
    schedule = getattr(hass.config_entries, "async_schedule_reload", None)
    if schedule is not None:
        schedule(entry_id)
    else:                                          # HA before 2024.2
        hass.async_create_task(hass.config_entries.async_reload(entry_id))


async def async_remove_config_entry_device(hass: HomeAssistant, entry, device) -> bool:
    """The device page's Delete (#18): a heater that's gone or was replaced.

    The relay forgets it first (its key and files, DELETE /heaters/<dsn>), so
    it can't come back on the next poll; then HA removes the device and its
    entities. A heater that's still reporting is refused: deleting it would
    only bring it back, under new entities, on its next boot.
    """
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    dsn = next((valid_dsn(value) for domain, value in device.identifiers
                if domain == DOMAIN), None)
    if coordinator is None or dsn is None:
        raise HomeAssistantError(
            "This device can't be deleted: it's the water heater of a relay that hasn't "
            "named its DSN yet (or the integration isn't loaded). Update the add-on first.")
    data = coordinator.data or {}
    listed = (dsn in data["heaters"]) if multi_capable(data) else dsn == coordinator.primary
    if listed:
        if heater_state(coordinator, dsn).get("fresh"):
            raise HomeAssistantError(
                f"{device.name_by_user or device.name} is still reporting to the relay. "
                "Power it off or stop steering it to the relay first; otherwise it comes "
                "back on its next boot.")
        await _relay_forget(hass, coordinator.relay_url, dsn)
    # No fallback to a forgotten heater, and its number is free again (a
    # replacement takes it: "iCOMM Water Heater", not "... 2").
    coordinator.numbers = {k: v for k, v in coordinator.numbers.items() if k != dsn}
    data = {**entry.data, CONF_NUMBERS: dict(coordinator.numbers)}
    if entry.data.get(CONF_DSN) == dsn:
        data[CONF_DSN] = None
    hass.config_entries.async_update_entry(entry, data=data)
    for kind in ("stale_telemetry", "telemetry_frozen", "cloud_idle"):
        ir.async_delete_issue(hass, DOMAIN,
                              _heater_issue_id(kind, entry.entry_id, dsn, coordinator.primary))
    _reload_once_removed(hass, entry.entry_id, device.id)
    return True


def _reload_once_removed(hass: HomeAssistant, entry_id: str, device_id: str) -> None:
    """Reload (to start clean after the relay's restart) once HA has removed
    the device, which it does after the hook returns: earlier, the old
    entities would still hold their entity IDs and the reload would add new
    ones beside them as ..._2 (the single-heater replacement wants the
    default IDs back)."""
    @callback
    def _reload(_now) -> None:
        _schedule_reload(hass, entry_id)

    @callback
    def _give_up(_now) -> None:                    # HA never removed it
        unsubscribe()

    @callback
    def _removed(event) -> None:
        if event.data.get("action") == "remove" and event.data.get("device_id") == device_id:
            unsubscribe()
            cancel()
            # A moment later: the entity registry drops the device's entities
            # in its own listener for this same event.
            async_call_later(hass, 1, _reload)

    unsubscribe = hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, _removed)
    cancel = async_call_later(hass, 60, _give_up)


async def _relay_forget(hass: HomeAssistant, url: str, dsn: str) -> None:
    session = async_get_clientsession(hass)
    try:
        async with session.delete(f"{url}/heaters/{dsn}", params={"confirm": dsn},
                                  timeout=aiohttp.ClientTimeout(total=10)) as resp:
            status = resp.status
            try:
                body = await resp.json(content_type=None)
            except ValueError:                     # an older relay's plain-text 404/405
                body = None
    except (TimeoutError, aiohttp.ClientError) as err:
        raise HomeAssistantError(f"Couldn't reach the relay to remove the heater: {err}") from err
    body = body if isinstance(body, dict) else {}
    if status == 200 or (status == 404 and body.get("error") == "no such heater"):
        return
    if status in (404, 405):                       # no such endpoint: before 0.7.2
        raise HomeAssistantError("The relay is too old to remove a heater; update the add-on.")
    raise HomeAssistantError(f"The relay refused: {body.get('error') or f'HTTP {status}'}")


async def _options_updated(hass: HomeAssistant, entry) -> None:
    """Reload for the owner's options only. The entry's data also changes as
    heaters are numbered (#18), and that must not reload everything."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None and coordinator.options == dict(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    unload = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload:
        ir.async_delete_issue(hass, DOMAIN, f"stale_telemetry_{entry.entry_id}")
        ir.async_delete_issue(hass, DOMAIN, f"upstream_certificate_{entry.entry_id}")
        ir.async_delete_issue(hass, DOMAIN, f"telemetry_frozen_{entry.entry_id}")
        ir.async_delete_issue(hass, DOMAIN, f"cloud_idle_{entry.entry_id}")
        ir.async_delete_issue(hass, DOMAIN, f"dns_server_failed_{entry.entry_id}")
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            # Last relay gone: services would only raise, so unregister them.
            for service in ("set_setpoint", "set_mode", "cancel_writes"):
                hass.services.async_remove(DOMAIN, service)
    return unload
