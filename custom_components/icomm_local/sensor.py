"""Sensors from the relay's decoded telemetry."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfElectricPotential,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

F = UnitOfTemperature.FAHRENHEIT

# field on the relay's state, display name, unit, device class, state class
SPECS = [
    ("water_top", "Water, tank top", F, SensorDeviceClass.TEMPERATURE, None),
    ("water_bottom", "Water, tank bottom", F, SensorDeviceClass.TEMPERATURE, None),
    ("setpoint", "Setpoint", F, SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT),
    ("max_setpoint", "Max setpoint", F, SensorDeviceClass.TEMPERATURE, None),
    ("mode", "Mode", None, None, None),
    ("watts", "Power", UnitOfPower.WATT, SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT),
    ("volts", "Line voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE,
     SensorStateClass.MEASUREMENT),
    ("air", "Air at unit", F, SensorDeviceClass.TEMPERATURE, None),
    ("rssi", "Wi-Fi RSSI", SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
     SensorDeviceClass.SIGNAL_STRENGTH, SensorStateClass.MEASUREMENT),
]


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        IcommSensor(coordinator, field, name, unit, device_class, state_class)
        for field, name, unit, device_class, state_class in SPECS
    )


class IcommSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, field, name, unit=None, device_class=None,
                 state_class=None):
        super().__init__(coordinator)
        self._field = field
        self._attr_unique_id = f"icomm_local_{field}"
        self._attr_name = name
        if unit is not None:
            self._attr_native_unit_of_measurement = unit
        if device_class is not None:
            self._attr_device_class = device_class
        if state_class is not None:
            self._attr_state_class = state_class

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        if not data.get("online"):
            return None
        return data.get(self._field)

    @property
    def available(self) -> bool:
        # `fresh`, not `online`: the module is event-driven and goes quiet for long
        # stretches when the tank is satisfied, so "seen at some point" is not the
        # same as "reporting now".
        return bool((self.coordinator.data or {}).get("fresh"))

    @property
    def device_info(self):
        return {
            "identifiers": {("icomm_local", "water_heater")},
            "name": "Water Heater (iCOMM local)",
            "manufacturer": "A. O. Smith",
        }
