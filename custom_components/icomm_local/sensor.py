"""Sensors from the relay's decoded telemetry."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfPower,
    UnitOfTemperature,
    UnitOfElectricPotential,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities: AddEntitiesCallback):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        IcommSensor(coordinator, "water_top", "Water, tank top", UnitOfTemperature.FAHRENHEIT, SensorDeviceClass.TEMPERATURE),
        IcommSensor(coordinator, "water_bottom", "Water, tank bottom", UnitOfTemperature.FAHRENHEIT, SensorDeviceClass.TEMPERATURE),
        IcommSensor(coordinator, "setpoint", "Setpoint", UnitOfTemperature.FAHRENHEIT, SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT),
        IcommSensor(coordinator, "max_setpoint", "Max setpoint", UnitOfTemperature.FAHRENHEIT, SensorDeviceClass.TEMPERATURE),
        IcommSensor(coordinator, "mode", "Mode"),
        IcommSensor(coordinator, "watts", "Power", UnitOfPower.WATT, SensorDeviceClass.POWER, state_class=SensorStateClass.MEASUREMENT),
        IcommSensor(coordinator, "volts", "Line voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, state_class=SensorStateClass.MEASUREMENT),
        IcommSensor(coordinator, "air", "Air at unit", UnitOfTemperature.FAHRENHEIT, SensorDeviceClass.TEMPERATURE),
        IcommSensor(coordinator, "rssi", "Wi-Fi RSSI", SIGNAL_STRENGTH_DECIBELS_MILLIWATT, SensorDeviceClass.SIGNAL_STRENGTH, state_class=SensorStateClass.MEASUREMENT),
    ]
    async_add_entities(entities)


class IcommSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, field, name, unit=None, device_class=None, state_class=None):
        super().__init__(coordinator)
        self._field = field
        self._attr_unique_id = f"icomm_local_{field}"
        self._attr_name = name
        if unit:
            self._attr_native_unit_of_measurement = unit
        if device_class:
            self._attr_device_class = device_class
        if state_class:
            self._attr_state_class = state_class

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        if not data.get("online"):
            return None
        return data.get(self._field)

    @property
    def available(self) -> bool:
        return bool((self.coordinator.data or {}).get("fresh"))

    @property
    def device_info(self):
        return {
            "identifiers": {("icomm_local", "water_heater")},
            "name": "Water Heater (iCOMM local)",
            "manufacturer": "A. O. Smith",
        }
