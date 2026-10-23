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

from .const import DOMAIN, MODES

F = UnitOfTemperature.FAHRENHEIT

# field on the relay's state, display name, unit, device class, state class
SPECS = [
    ("water_top", "Water, tank top", F, SensorDeviceClass.TEMPERATURE, None),
    ("water_bottom", "Water, tank bottom", F, SensorDeviceClass.TEMPERATURE, None),
    ("setpoint", "Setpoint", F, SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT),
    ("max_setpoint", "Max setpoint", F, SensorDeviceClass.TEMPERATURE, None),
    ("watts", "Power", UnitOfPower.WATT, SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT),
    ("volts", "Line voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE,
     SensorStateClass.MEASUREMENT),
    ("air", "Air at unit", F, SensorDeviceClass.TEMPERATURE, None),
    ("rssi", "Wi-Fi RSSI", SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
     SensorDeviceClass.SIGNAL_STRENGTH, SensorStateClass.MEASUREMENT),
]


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        IcommSensor(coordinator, field, name, unit, device_class, state_class)
        for field, name, unit, device_class, state_class in SPECS
    ]
    entities += [ModeSensor(coordinator), RelayHealthSensor(coordinator),
                 PendingWriteSensor(coordinator)]
    async_add_entities(entities)


class IcommSensor(CoordinatorEntity, SensorEntity):
    # Entity names are relative to the device: sensor.icomm_water_heater_power.
    _attr_has_entity_name = True
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
            "name": "iCOMM Water Heater",
            "manufacturer": "A. O. Smith",
        }


DEVICE_INFO = {
    "identifiers": {("icomm_local", "water_heater")},
    "name": "iCOMM Water Heater",
    "manufacturer": "A. O. Smith",
}


class _RelaySensor(CoordinatorEntity, SensorEntity):
    """About the relay rather than the tank: available whenever the relay answers."""

    _attr_has_entity_name = True

    @property
    def device_info(self):
        return DEVICE_INFO


class RelayHealthSensor(_RelaySensor):
    """healthy / idle / degraded / unknown -- with what the relay cannot see."""

    _attr_unique_id = "icomm_local_health"
    _attr_name = "Relay health"
    _attr_icon = "mdi:heart-pulse"

    @property
    def native_value(self):
        return ((self.coordinator.data or {}).get("health") or {}).get("state")

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        health = data.get("health") or {}
        return {
            "reasons": health.get("reasons"),
            "remediation": health.get("remediation"),
            "unobservable": health.get("unobservable"),
            "last_telemetry_age_s": data.get("last_telemetry_age"),
            "upstream_errors": data.get("upstream_errors"),
            "lan": data.get("lan"),
        }


class PendingWriteSensor(_RelaySensor):
    """What is queued and not yet confirmed by the tank's own telemetry."""

    _attr_unique_id = "icomm_local_pending"
    _attr_name = "Pending write"
    _attr_icon = "mdi:timer-sand"

    @property
    def native_value(self):
        pending = (self.coordinator.data or {}).get("pending") or {}
        if not pending:
            return "none"
        return ", ".join(f"{kind} {w.get('value')}" for kind, w in sorted(pending.items()))

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        return {"pending": data.get("pending"), "last_write": data.get("last_write"),
                "writes_confirmed": data.get("writes_confirmed")}


class ModeSensor(IcommSensor):
    """Operating mode as a translated enum: "Heat pump", not `heat_pump`.

    Icons per state live in icons.json. A mode the relay has never seen (guest and
    vacation were never captured) reads as unknown: an ENUM sensor that reports a
    value outside its options raises inside Home Assistant, and an uncaptured mode
    must not take the entity down.
    """

    _attr_translation_key = "mode"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "mode", None, device_class=SensorDeviceClass.ENUM)
        # The name comes from translations; unique_id unchanged, so the entity_id
        # (sensor.icomm_water_heater_mode) and its history survive the upgrade.
        del self._attr_name
        self._attr_options = list(MODES)

    @property
    def native_value(self):
        value = super().native_value
        return value if value in MODES else None
