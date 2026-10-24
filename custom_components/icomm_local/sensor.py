"""Sensors from the relay's decoded telemetry."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODES, has_telemetry, is_stale

F = UnitOfTemperature.FAHRENHEIT

# field on the relay's state, display name, unit, device class, state class
SPECS = [
    # Relay keys predate knowing what they are: "water_mid" is the bottom
    # thermistor; "water_bottom" is the blend (TankAverageSensor, below).
    ("water_top", "Water, tank top", F, SensorDeviceClass.TEMPERATURE, None),
    ("water_mid", "Water, tank bottom", F, SensorDeviceClass.TEMPERATURE, None),
    ("hot_water_availability", "Hot water available", PERCENTAGE, None,
     SensorStateClass.MEASUREMENT),
    ("heat_deficit_wh", "Heat deficit", UnitOfEnergy.WATT_HOUR, SensorDeviceClass.ENERGY_STORAGE,
     SensorStateClass.MEASUREMENT),
    ("setpoint", "Setpoint", F, SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT),
    ("max_setpoint", "Max setpoint", F, SensorDeviceClass.TEMPERATURE, None),
    # What the tank heats to now: the setpoint, or 50 F in vacation.
    ("active_setpoint", "Active setpoint", F, SensorDeviceClass.TEMPERATURE,
     SensorStateClass.MEASUREMENT),
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
    entities += [TankAverageSensor(coordinator),
                 ModeSensor(coordinator), HeatingElementSensor(coordinator),
                 RelayHealthSensor(coordinator),
                 PendingWriteSensor(coordinator), LastAppWriteSensor(coordinator)]
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
        if not has_telemetry(data):
            return None
        return data.get(self._field)

    @property
    def available(self) -> bool:
        # Last-known values while stale (with `stale` set), not unavailable: an
        # idle tank goes quiet for long stretches, and unavailable entities make
        # Home Assistant drop writes. See const.has_telemetry.
        return super().available and has_telemetry(self.coordinator.data)

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        return {"stale": is_stale(data),
                "last_telemetry_age_s": data.get("last_telemetry_age")}

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
        # e.g. "setpoint 140.0: acknowledged". Only telemetry ever says confirmed.
        return ", ".join(f"{kind} {w.get('value')}: {w.get('status', 'queued')}"
                         for kind, w in sorted(pending.items()))

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        pending = data.get("pending") or {}
        return {"status": {kind: w.get("status") for kind, w in pending.items()} or None,
                "pending": pending or None, "last_write": data.get("last_write"),
                "writes_confirmed": data.get("writes_confirmed")}


class LastAppWriteSensor(_RelaySensor):
    """The last write that came through the cloud (A.O. Smith app or its API).

    Seen by the relay as setBlockParameter in a commands.xml reply, so app and
    Home Assistant writes can be told apart. Since the relay started.
    """

    _attr_unique_id = "icomm_local_last_app_write"
    _attr_name = "Last app write"
    _attr_icon = "mdi:cellphone-arrow-down"

    @property
    def native_value(self):
        last = (self.coordinator.data or {}).get("last_cloud_write")
        if not last:
            return "none"
        return f"{last.get('kind')} {last.get('value')}"

    @property
    def extra_state_attributes(self):
        from datetime import UTC, datetime

        data = self.coordinator.data or {}
        last = data.get("last_cloud_write") or {}
        at = last.get("at")
        return {"at": datetime.fromtimestamp(at, UTC).isoformat() if at else None,
                "frame": last.get("frame"),
                "replaced_by_local": last.get("replaced_by_local"),
                "cloud_writes": data.get("cloud_writes")}


class TankAverageSensor(IcommSensor):
    """The controller's tank temperature: a blend of the two thermistors.

    Not a third sensor (research phase 37). Measured: (top + bottom) / 2 in
    electric mode, about (3 top + bottom) / 4 with the heat pump. The live
    weight is shown so the blend can be followed (and re-measured) in other
    modes.
    """

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "water_bottom", "Water, tank average", F,
                         SensorDeviceClass.TEMPERATURE)

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        top, bottom, blend = (data.get(k) for k in ("water_top", "water_mid", "water_bottom"))
        weight = None
        if all(isinstance(v, (int, float)) for v in (top, bottom, blend)) and top - bottom >= 5:
            weight = round((blend - bottom) / (top - bottom), 2)
        return {
            **super().extra_state_attributes,
            "computed": True,
            "weighting": "electric: (top + bottom) / 2; heat pump: about (3 × top + bottom) / 4",
            "weight_on_top": weight,
        }


ELEMENTS = ["upper", "lower", "off"]


class HeatingElementSensor(IcommSensor):
    """Which resistive element is heating: upper, lower or off (neither).

    "off" includes the heat pump running alone; the relay leaves a flag
    combination it doesn't know as unknown.
    """

    _attr_translation_key = "heating_element"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "heating_element", None,
                         device_class=SensorDeviceClass.ENUM)
        del self._attr_name
        self._attr_options = ELEMENTS

    @property
    def native_value(self):
        value = super().native_value
        return value if value in ELEMENTS else None

    @property
    def extra_state_attributes(self):
        flags = (self.coordinator.data or {}).get("heat_flags")
        return {"flags": f"0x{flags:02x}" if isinstance(flags, int) else None}


class ModeSensor(IcommSensor):
    """Operating mode as a translated enum: "Heat pump", not `heat_pump`.

    Icons per state live in icons.json. A mode the relay has never seen (guest was
    never captured) reads as unknown: an ENUM sensor that reports a value outside
    its options raises inside Home Assistant, and an uncaptured mode must not take
    the entity down.
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

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        return {"vacation_days": data.get("vacation_days"), "raw": data.get("mode_raw")}
