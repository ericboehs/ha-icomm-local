"""The water heater entity: HA's native thermostat card, writing through the relay.

A write is queued in the relay and delivered on the appliance's own LAN poll
(about every 10 s once LAN mode is armed; otherwise the next cloud poll). The
target shown is the queued value until the
appliance's own telemetry confirms it -- otherwise the card would snap back to the
old value for the seconds a write takes to land, and look broken.
"""

from __future__ import annotations

from homeassistant.components.water_heater import (
    STATE_ECO,
    STATE_ELECTRIC,
    STATE_HEAT_PUMP,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE, STATE_OFF, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    VACATION_DEFAULT_DAYS,
    as_dict,
    as_number,
    has_telemetry,
    is_stale,
)
from .heaters import HeaterEntity, add_heaters, relay_path, unique_id
from .services import _post

# Same mapping HA's own aosmith integration uses, so cards and automations that
# know that integration read this one the same way. Hybrid is HA's "eco".
TO_HA = {"electric": STATE_ELECTRIC, "heat_pump": STATE_HEAT_PUMP, "hybrid": STATE_ECO}
FROM_HA = {v: k for k, v in TO_HA.items()}
MIN_F = 95.0
# Vacation is HA's away mode, shown as operation "off" -- the same as HA's own
# aosmith integration. Leaving it restores the mode the tank was in before;
# with no record of that, heat pump, the lowest-energy mode.
AWAY_FALLBACK_MODE = "heat_pump"


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    add_heaters(coordinator, entry, async_add_entities,
                lambda dsn: [IcommWaterHeater(coordinator, dsn)])


class IcommWaterHeater(HeaterEntity, WaterHeaterEntity, RestoreEntity):
    # The device's own name: water_heater.icomm_water_heater (..._2 for a second).
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE
        | WaterHeaterEntityFeature.OPERATION_MODE
        | WaterHeaterEntityFeature.AWAY_MODE
    )
    _attr_operation_list = list(TO_HA.values())
    _attr_precision = 1.0
    _attr_target_temperature_step = 1.0

    def __init__(self, coordinator, dsn: str | None = None) -> None:
        super().__init__(coordinator, dsn)
        self._attr_unique_id = unique_id(dsn, "water_heater")
        self._mode_before_vacation: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Survive an HA restart mid-vacation: the mode to return to is otherwise
        # only in memory.
        if (last := await self.async_get_last_state()) is not None:
            saved = last.attributes.get("mode_before_vacation")
            if saved in TO_HA:
                self._mode_before_vacation = saved
        self._remember_mode()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._remember_mode()
        super()._handle_coordinator_update()

    def _remember_mode(self) -> None:
        mode = self._data.get("mode")
        if isinstance(mode, str) and mode in TO_HA:
            self._mode_before_vacation = mode

    @property
    def _data(self) -> dict:
        return self._state

    async def _write(self, payload: dict) -> None:
        await _post(self.hass, relay_path(self.coordinator, self._dsn, "/write"), payload,
                    self.coordinator)
        await self.coordinator.async_request_refresh()

    def _pending(self, kind: str):
        return as_dict(as_dict(self._data.get("pending")).get(kind)).get("value")

    @property
    def available(self) -> bool:
        # Stale is a warning, not unavailability: HA drops service calls to an
        # unavailable entity, and writes are exactly what an idle tank needs.
        return super().available and has_telemetry(self._data)

    @property
    def current_temperature(self):
        # The controller's own tank temperature (the relay's "water_bottom",
        # shown as "Water, tank average"): what it heats against, so the card
        # agrees with when it runs. The top thermistor is there as a sensor.
        data = self._data
        blend = as_number(data.get("water_bottom"))
        return blend if blend is not None else as_number(data.get("water_top"))

    @property
    def target_temperature(self):
        pending = as_number(self._pending("setpoint"))
        if pending is not None:
            return pending
        # In vacation the tank holds a lower target (50 F) while keeping the
        # setpoint to return to; show what it is really doing, as the app does.
        active = as_number(self._data.get("active_setpoint"))
        if self._data.get("mode") == "vacation" and active:
            return active
        return as_number(self._data.get("setpoint"))

    def _effective_mode(self) -> str | None:
        mode = self._pending("mode") or self._data.get("mode")
        return mode if isinstance(mode, str) else None

    @property
    def current_operation(self):
        mode = self._effective_mode()
        if mode == "vacation":
            return STATE_OFF
        return TO_HA.get(mode, mode)

    @property
    def is_away_mode_on(self) -> bool:
        return self._effective_mode() == "vacation"

    @property
    def min_temp(self) -> float:
        return MIN_F

    @property
    def max_temp(self) -> float:
        return float(as_number(self._data.get("max_setpoint")) or 140.0)

    @property
    def extra_state_attributes(self):
        # Nothing here may change on every poll (an age, a counter): each state
        # change makes HA's frontend before 2026.11 reset the card's target
        # while +/- is being pressed. The age is on the Relay health sensor.
        data = self._data
        return {
            "stale": is_stale(data),
            "confirmed_setpoint": data.get("setpoint"),
            "active_setpoint": data.get("active_setpoint"),
            "confirmed_mode": data.get("mode"),
            "water_average": data.get("water_bottom"),   # see sensor.py SPECS
            "water_bottom": data.get("water_mid"),
            "power_w": data.get("watts"),
            "pending": data.get("pending") or None,
            "write_status": {kind: as_dict(w).get("status") for kind, w in
                             as_dict(data.get("pending")).items()} or None,
            "vacation_days": data.get("vacation_days"),
            "mode_before_vacation": self._mode_before_vacation,
            "relay_health": as_dict(data.get("health")).get("state"),
            "lan_registered": as_dict(data.get("lan")).get("registered"),
        }

    async def async_set_temperature(self, **kwargs) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        await self._write({"setpoint": float(temperature)})

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        if operation_mode == STATE_OFF:
            # Shown during vacation but not in operation_list, which older HA
            # doesn't enforce; the relay has no "off" mode to write.
            raise ServiceValidationError(
                "This water heater can't be turned off; turn on away mode for vacation.")
        mode = FROM_HA.get(operation_mode, operation_mode)
        await self._write({"mode": mode})

    async def async_turn_away_mode_on(self) -> None:
        await self._write({"mode": "vacation", "days": VACATION_DEFAULT_DAYS})

    async def async_turn_away_mode_off(self) -> None:
        mode = self._mode_before_vacation or AWAY_FALLBACK_MODE
        await self._write({"mode": mode})
