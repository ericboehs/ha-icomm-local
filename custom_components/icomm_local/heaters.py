"""Several water heaters behind one relay: one HA device each (#18, phase 3).

The relay (0.7+) serves every heater it has confirmed. `/state` is still the
primary heater's, and lists every DSN under `heaters`; `/heaters/<dsn>/state`
is each one's own. This module is the integration's side of that:

  * the coordinator keeps one state per DSN (`coordinator.heaters`), primary
    first in `coordinator.order`;
  * entities are bound to a DSN: unique ID `icomm_local_<DSN>_<key>`, device
    `(icomm_local, <DSN>)`, named "iCOMM Water Heater", "iCOMM Water Heater 2"...;
  * an install from before (unique IDs `icomm_local_<key>`, one device
    `(icomm_local, water_heater)`) is re-keyed once the relay names the DSN.
    Only unique IDs and the device identifier change, so entity IDs, names,
    dashboards and recorder history stay exactly as they were.

An older relay (no DSN in `/state`) keeps the old single-heater keys.
"""

from __future__ import annotations

import logging
import re

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, as_dict

_LOGGER = logging.getLogger(__name__)
LEGACY_DEVICE = (DOMAIN, "water_heater")
PREFIX = f"{DOMAIN}_"
DSN_RE = re.compile(r"[A-Z0-9]{6,32}")
NEW_STYLE = re.compile(rf"^{PREFIX}([A-Z0-9]{{6,32}})_")
NAME = "iCOMM Water Heater"


def primary_dsn(data: dict | None) -> str | None:
    """The DSN of the heater `/state` describes (relay 0.6+), if known."""
    data = data or {}
    dsn = (as_dict(data.get("heater")).get("dsn")
           or as_dict(data.get("device")).get("dsn"))
    return dsn if isinstance(dsn, str) and DSN_RE.fullmatch(dsn) else None


def multi_capable(data: dict | None) -> bool:
    """The relay has the per-heater API (/heaters/<dsn>/...)."""
    return isinstance((data or {}).get("heaters"), list)


def unique_id(dsn: str | None, key: str) -> str:
    return f"{PREFIX}{dsn}_{key}" if dsn else f"{PREFIX}{key}"


def dsn_of(unique: str | None) -> str | None:
    found = NEW_STYLE.match(unique or "")
    return found.group(1) if found else None


def valid_dsn(value) -> str | None:
    return value if isinstance(value, str) and DSN_RE.fullmatch(value) else None


def heater_name(coordinator, dsn: str | None) -> str:
    """A heater's number is kept in the config entry, so it doesn't shift when
    another heater comes or goes."""
    number = getattr(coordinator, "numbers", {}).get(dsn, 1)
    return NAME if number == 1 else f"{NAME} {number}"


def device_info(coordinator, dsn: str | None) -> dict:
    return {"identifiers": {(DOMAIN, dsn) if dsn else LEGACY_DEVICE},
            "name": heater_name(coordinator, dsn), "manufacturer": "A. O. Smith"}


def relay_path(coordinator, dsn: str | None, path: str) -> str:
    """Where a heater's write goes: its own endpoint when the relay lists that
    heater, otherwise the single-heater one (an older relay, or one that has
    lost track of the DSN and only has its one heater).

    A second heater the relay no longer lists is refused: the bare endpoint is
    the primary heater's, so falling back would write to the wrong tank."""
    data = coordinator.data or {}
    if dsn and multi_capable(data) and dsn in data["heaters"]:
        return f"/heaters/{dsn}{path}"
    if dsn is None or dsn == getattr(coordinator, "primary", None) or not multi_capable(data):
        return path
    raise HomeAssistantError(
        "The relay no longer lists this water heater; nothing was written. "
        "Check the relay's heaters, or delete this device if the heater is gone.")


def heater_state(coordinator, dsn: str | None) -> dict:
    if dsn is None or dsn == getattr(coordinator, "primary", None):
        return coordinator.data or {}
    return getattr(coordinator, "heaters", {}).get(dsn) or {}


class HeaterEntity(CoordinatorEntity):
    """An entity of one heater (dsn None: an older relay's only heater)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, dsn: str | None = None) -> None:
        super().__init__(coordinator)
        self._dsn = dsn

    @property
    def _state(self) -> dict:
        return heater_state(self.coordinator, self._dsn)

    @property
    def available(self) -> bool:
        return super().available and bool(self._state)

    @property
    def device_info(self):
        return device_info(self.coordinator, self._dsn)

    @callback
    def _handle_coordinator_update(self) -> None:
        # The coordinator calls its listeners in turn with nothing around
        # them: one entity raising on a malformed field would leave every
        # later one (other heaters', the device pages) un-updated.
        try:
            super()._handle_coordinator_update()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("%s failed to update (%s)", self.entity_id, type(err).__name__)
            _LOGGER.debug("%s traceback", self.entity_id, exc_info=True)


@callback
def add_heaters(coordinator, entry, add_entities, build) -> None:
    """Entities for every heater now, and for each new one as it appears."""
    seen: set[str | None] = set()

    @callback
    def _add() -> None:
        if None in seen:
            # Started on an older relay's single-heater keys: when the DSN
            # turns up the entry reloads and migrates; adding DSN-keyed
            # entities here first would register duplicates.
            return
        dsns = list(coordinator.order) or [None]
        new = [dsn for dsn in dsns if dsn not in seen]
        if not new:
            return
        seen.update(new)
        add_entities([entity for dsn in new for entity in build(dsn)])

    _add()
    entry.async_on_unload(coordinator.async_add_listener(_add))


def _device_by_identifier(hass: HomeAssistant, identifier: tuple, entry_id: str):
    registry = dr.async_get(hass)
    lookup = getattr(registry, "async_get_device_by_identifier", None)
    if lookup is not None:                                  # HA 2026.x+
        return lookup(identifier, entry_id)
    return registry.async_get_device(identifiers={identifier})


@callback
def migrate_to_dsn(hass: HomeAssistant, entry, dsn: str) -> int:
    """Re-key a single-heater install to its DSN. Idempotent; returns how many
    entities moved."""
    registry = er.async_get(hass)
    moved = 0
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if not entity.unique_id.startswith(PREFIX) or NEW_STYLE.match(entity.unique_id):
            continue
        new = unique_id(dsn, entity.unique_id[len(PREFIX):])
        if registry.async_get_entity_id(entity.domain, DOMAIN, new):
            continue                     # already there (a half-done earlier run)
        registry.async_update_entity(entity.entity_id, new_unique_id=new)
        moved += 1
    device = _device_by_identifier(hass, LEGACY_DEVICE, entry.entry_id)
    if device is not None and _device_by_identifier(hass, (DOMAIN, dsn), entry.entry_id) is None:
        dr.async_get(hass).async_update_device(device.id, new_identifiers={(DOMAIN, dsn)})
    return moved


# Keys whose entities were wrong and are gone: "air" (0.7.4) showed the energy
# capacity in Wh as degrees F (research phase 49). Removed, not repurposed, so
# its history can't sit under a new meaning or unit.
RETIRED = ("air",)


@callback
def retire(hass: HomeAssistant, entry) -> list[str]:
    """Remove this entry's entities for retired keys; returns their entity IDs."""
    registry = er.async_get(hass)
    gone = []
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique = entity.unique_id
        dsn = dsn_of(unique)
        key = unique[len(PREFIX) + (len(dsn) + 1 if dsn else 0):] if unique.startswith(
            PREFIX) else None
        if key in RETIRED:
            registry.async_remove(entity.entity_id)
            gone.append(entity.entity_id)
    return gone


@callback
def rekey(hass: HomeAssistant, entry, old: str, new: str) -> int:
    """A replaced heater (#18): the old DSN's entities and device become the new
    one's, so entity IDs, dashboards and history carry on as for any upgrade."""
    registry = er.async_get(hass)
    old_prefix, moved = f"{PREFIX}{old}_", 0
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if not entity.unique_id.startswith(old_prefix):
            continue
        new_id = unique_id(new, entity.unique_id[len(old_prefix):])
        if registry.async_get_entity_id(entity.domain, DOMAIN, new_id):
            continue
        registry.async_update_entity(entity.entity_id, new_unique_id=new_id)
        moved += 1
    device = _device_by_identifier(hass, (DOMAIN, old), entry.entry_id)
    if device is not None and _device_by_identifier(hass, (DOMAIN, new), entry.entry_id) is None:
        dr.async_get(hass).async_update_device(device.id, new_identifiers={(DOMAIN, new)})
    return moved


@callback
def update_device_pages(hass: HomeAssistant, coordinator, entry_id: str) -> None:
    """Show each heater's firmware, model and serial on its HA device page."""
    for dsn in list(coordinator.order) or [None]:
        device = as_dict(heater_state(coordinator, dsn).get("device"))
        entry = _device_by_identifier(hass, (DOMAIN, dsn) if dsn else LEGACY_DEVICE, entry_id)
        wanted = {"sw_version": device.get("controller_firmware"),
                  "model": device.get("model_number") or device.get("model"),
                  "serial_number": device.get("serial_number")}
        wanted = {k: v for k, v in wanted.items() if v}
        if entry and any(getattr(entry, k) != v for k, v in wanted.items()):
            dr.async_get(hass).async_update_device(entry.id, **wanted)
