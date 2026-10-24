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
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, has_telemetry, is_stale
from .sensor import DEVICE_INFO
from .services import _post

# Same mapping HA's own aosmith integration uses, so cards and automations that
# know that integration read this one the same way. Hybrid is HA's "eco".
TO_HA = {"electric": STATE_ELECTRIC, "heat_pump": STATE_HEAT_PUMP, "hybrid": STATE_ECO}
FROM_HA = {v: k for k, v in TO_HA.items()}
MIN_F = 95.0


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback):
    async_add_entities([IcommWaterHeater(hass.data[DOMAIN][entry.entry_id])])


class IcommWaterHeater(CoordinatorEntity, WaterHeaterEntity):
    _attr_unique_id = "icomm_local_water_heater"
    # The device's own name: water_heater.icomm_water_heater.
    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE | WaterHeaterEntityFeature.OPERATION_MODE
    )
    _attr_operation_list = list(TO_HA.values())
    _attr_precision = 1.0
    _attr_target_temperature_step = 1.0

    @property
    def device_info(self):
        return DEVICE_INFO

    @property
    def _data(self) -> dict:
        return self.coordinator.data or {}

    def _pending(self, kind: str):
        return ((self._data.get("pending") or {}).get(kind) or {}).get("value")

    @property
    def available(self) -> bool:
        # Stale is a warning, not unavailability: HA drops service calls to an
        # unavailable entity, and writes are exactly what an idle tank needs.
        return super().available and has_telemetry(self._data)

    @property
    def current_temperature(self):
        return self._data.get("water_top")

    @property
    def target_temperature(self):
        pending = self._pending("setpoint")
        return pending if pending is not None else self._data.get("setpoint")

    @property
    def current_operation(self):
        mode = self._pending("mode") or self._data.get("mode")
        return TO_HA.get(mode, mode)

    @property
    def min_temp(self) -> float:
        return MIN_F

    @property
    def max_temp(self) -> float:
        return float(self._data.get("max_setpoint") or 140.0)

    @property
    def extra_state_attributes(self):
        data = self._data
        return {
            "stale": is_stale(data),
            "last_telemetry_age_s": data.get("last_telemetry_age"),
            "confirmed_setpoint": data.get("setpoint"),
            "confirmed_mode": data.get("mode"),
            "water_bottom": data.get("water_bottom"),
            "power_w": data.get("watts"),
            "pending": data.get("pending") or None,
            "write_status": {kind: w.get("status") for kind, w in
                             (data.get("pending") or {}).items()} or None,
            "relay_health": (data.get("health") or {}).get("state"),
            "lan_registered": (data.get("lan") or {}).get("registered"),
        }

    async def async_set_temperature(self, **kwargs) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        await _post(self.hass, "/write", {"setpoint": float(temperature)}, self.coordinator)
        await self.coordinator.async_request_refresh()

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        mode = FROM_HA.get(operation_mode, operation_mode)
        await _post(self.hass, "/write", {"mode": mode}, self.coordinator)
        await self.coordinator.async_request_refresh()
