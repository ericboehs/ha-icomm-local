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
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .const import DOMAIN, FAULTS, MODES, has_telemetry, is_stale
from .heaters import HeaterEntity, add_heaters, heater_name, unique_id

F = UnitOfTemperature.FAHRENHEIT

# field on the relay's state, display name, unit, device class, state class
SPECS = [
    # Relay keys predate knowing what they are: "water_mid" is the bottom
    # thermistor; "water_bottom" is the blend (TankAverageSensor, below).
    ("water_top", "Water, tank top", F, SensorDeviceClass.TEMPERATURE, None),
    ("water_mid", "Water, tank bottom", F, SensorDeviceClass.TEMPERATURE, None),
    ("hot_water_availability", "Hot water available", PERCENTAGE, None,
     SensorStateClass.MEASUREMENT),
    ("setpoint", "Setpoint", F, SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT),
    ("max_setpoint", "Max setpoint", F, SensorDeviceClass.TEMPERATURE, None),
    # What the tank heats to now: the setpoint, or 50 F in vacation.
    ("active_setpoint", "Active setpoint", F, SensorDeviceClass.TEMPERATURE,
     SensorStateClass.MEASUREMENT),
    ("watts", "Power", UnitOfPower.WATT, SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT),
    ("volts", "Line voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE,
     SensorStateClass.MEASUREMENT),
    # Block 27 param 15 (research phase 49): what was shown as "Air at unit"
    # until 0.7.4. The relay reads it in Wh; it steps with the setpoint and mode.
    # The maintenance display's P06 (research phase 51). Reads a few degrees
    # low while the compressor draws air past it.
    ("ambient", "Ambient temperature", F, SensorDeviceClass.TEMPERATURE,
     SensorStateClass.MEASUREMENT),
    ("energy_capacity_wh", "Energy capacity", UnitOfEnergy.WATT_HOUR,
     SensorDeviceClass.ENERGY_STORAGE, SensorStateClass.MEASUREMENT),
    ("rssi", "Wi-Fi RSSI", SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
     SensorDeviceClass.SIGNAL_STRENGTH, SensorStateClass.MEASUREMENT),
]


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback):
    coordinator = hass.data[DOMAIN][entry.entry_id]

    def build(dsn: str | None) -> list[SensorEntity]:
        entities: list[SensorEntity] = [
            IcommSensor(coordinator, field, name, unit, device_class, state_class, dsn=dsn)
            for field, name, unit, device_class, state_class in SPECS
        ]
        return entities + [
            TankAverageSensor(coordinator, dsn), ModeSensor(coordinator, dsn),
            EnergyToSetpointSensor(coordinator, dsn), RecoverySensor(coordinator, dsn),
            EnergySensor(coordinator, dsn), EnumSensor(coordinator, "operating_state",
                                                       ("idle", "heating", "fault"), dsn),
            EnumSensor(coordinator, "hot_water_level", ("low", "medium", "high"), dsn),
            ValveSensor(coordinator, dsn), FaultSensor(coordinator, dsn),
            HeatingElementSensor(coordinator, dsn), RelayHealthSensor(coordinator, dsn),
            PendingWriteSensor(coordinator, dsn), LastAppWriteSensor(coordinator, dsn),
            FirmwareSensor(coordinator, "controller_firmware", "Controller firmware",
                           ("controller_firmware_standby", "model_number", "serial_number"),
                           dsn),
            FirmwareSensor(coordinator, "module_firmware", "Wi-Fi module firmware",
                           ("module_hardware", "model", "dsn", "mac"), dsn)]

    add_heaters(coordinator, entry, async_add_entities, build)


class IcommSensor(HeaterEntity, SensorEntity):
    # Entity names are relative to the device: sensor.icomm_water_heater_power.
    def __init__(self, coordinator, field, name, unit=None, device_class=None,
                 state_class=None, dsn: str | None = None):
        super().__init__(coordinator, dsn)
        self._field = field
        self._attr_unique_id = unique_id(dsn, field)
        self._attr_name = name
        if unit is not None:
            self._attr_native_unit_of_measurement = unit
        if device_class is not None:
            self._attr_device_class = device_class
        if state_class is not None:
            self._attr_state_class = state_class

    @property
    def native_value(self):
        data = self._state
        if not has_telemetry(data):
            return None
        return data.get(self._field)

    @property
    def available(self) -> bool:
        # Last-known values while stale (with `stale` set), not unavailable: an
        # idle tank goes quiet for long stretches, and unavailable entities make
        # Home Assistant drop writes. See const.has_telemetry.
        return super().available and has_telemetry(self._state)

    @property
    def extra_state_attributes(self):
        data = self._state
        return {"stale": is_stale(data),
                "last_telemetry_age_s": data.get("last_telemetry_age")}



class _RelaySensor(HeaterEntity, SensorEntity):
    """About the relay's view of a heater rather than the tank: available
    whenever the relay answers for it."""

    _key = ""

    def __init__(self, coordinator, dsn: str | None = None) -> None:
        super().__init__(coordinator, dsn)
        self._attr_unique_id = unique_id(dsn, self._key)


class FirmwareSensor(_RelaySensor):
    """Firmware the heater reports: the controller's (the app's "Firmware
    Version") at each boot, the Wi-Fi module's agent in its config reply."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:chip"

    def __init__(self, coordinator, key: str, name: str, extras: tuple[str, ...],
                 dsn: str | None = None) -> None:
        self._key, self._extras = key, extras
        super().__init__(coordinator, dsn)
        self._attr_name = name

    @property
    def native_value(self):
        return (self._state.get("device") or {}).get(self._key)

    @property
    def extra_state_attributes(self):
        device = self._state.get("device") or {}
        return {key: device.get(key) for key in self._extras if device.get(key)}


class RelayHealthSensor(_RelaySensor):
    """healthy / idle / degraded / unknown -- with what the relay cannot see."""

    _key = "health"
    _attr_name = "Relay health"
    _attr_icon = "mdi:heart-pulse"

    @property
    def native_value(self):
        return (self._state.get("health") or {}).get("state")

    @property
    def extra_state_attributes(self):
        data = self._state
        health = data.get("health") or {}
        evidence = health.get("evidence") or {}
        return {
            "reasons": health.get("reasons"),
            "remediation": health.get("remediation"),
            # Cloud side over 24 h (relay 0.7.3+): a stopped cloud side shows
            # as few dials and a long gap.
            "cloud_dials_24h": evidence.get("cloud_dials_24h"),
            "cloud_quiet_s": evidence.get("cloud_quiet_s"),
            "longest_cloud_gap_24h_s": evidence.get("longest_cloud_gap_24h_s"),
            "unobservable": health.get("unobservable"),
            "last_telemetry_age_s": data.get("last_telemetry_age"),
            "upstream_errors": data.get("upstream_errors"),
            "lan": data.get("lan"),
        }


class PendingWriteSensor(_RelaySensor):
    """What is queued and not yet confirmed by the tank's own telemetry."""

    _key = "pending"
    _attr_name = "Pending write"
    _attr_icon = "mdi:timer-sand"

    @property
    def native_value(self):
        pending = self._state.get("pending") or {}
        if not pending:
            return "none"
        # e.g. "setpoint 140.0: acknowledged". Only telemetry ever says confirmed.
        return ", ".join(f"{kind} {w.get('value')}: {w.get('status', 'queued')}"
                         for kind, w in sorted(pending.items()))

    @property
    def extra_state_attributes(self):
        data = self._state
        pending = data.get("pending") or {}
        return {"status": {kind: w.get("status") for kind, w in pending.items()} or None,
                "pending": pending or None, "last_write": data.get("last_write"),
                "writes_confirmed": data.get("writes_confirmed")}


class LastAppWriteSensor(_RelaySensor):
    """The last write that came through the cloud (A.O. Smith app or its API).

    Seen by the relay as setBlockParameter in a commands.xml reply, so app and
    Home Assistant writes can be told apart. Since the relay started.
    """

    _key = "last_app_write"
    _attr_name = "Last app write"
    _attr_icon = "mdi:cellphone-arrow-down"

    @property
    def native_value(self):
        last = self._state.get("last_cloud_write")
        if not last:
            return "none"
        return f"{last.get('kind')} {last.get('value')}"

    @property
    def extra_state_attributes(self):
        from datetime import UTC, datetime

        data = self._state
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

    def __init__(self, coordinator, dsn: str | None = None) -> None:
        super().__init__(coordinator, "water_bottom", "Water, tank average", F,
                         SensorDeviceClass.TEMPERATURE, dsn=dsn)

    @property
    def extra_state_attributes(self):
        data = self._state
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

    def __init__(self, coordinator, dsn: str | None = None) -> None:
        super().__init__(coordinator, "heating_element", None,
                         device_class=SensorDeviceClass.ENUM, dsn=dsn)
        del self._attr_name
        self._attr_options = ELEMENTS

    @property
    def native_value(self):
        value = super().native_value
        return value if value in ELEMENTS else None

    @property
    def extra_state_attributes(self):
        flags = self._state.get("heat_flags")
        return {"flags": f"0x{flags:02x}" if isinstance(flags, int) else None}


class EnergyToSetpointSensor(IcommSensor):
    """What 0.7.4 and earlier called "Heat deficit" (block 4 offset 24, research
    phase 49b): the controller's estimate of the electric energy still needed
    to reach the setpoint in the current mode. It counts down at the heater's
    nominal power (356 W -> 6 Wh/min), so it is electric Wh, not heat in the
    water.

    The entity ID stays ..._heat_deficit, on fresh installs too, so dashboards
    and automations written for it keep working (an existing entity keeps its
    ID anyway; this pins what a new one gets).
    """

    _attr_icon = "mdi:fire-alert"

    def __init__(self, coordinator, dsn: str | None = None) -> None:
        super().__init__(coordinator, "heat_deficit_wh", "Energy to setpoint",
                         UnitOfEnergy.WATT_HOUR, SensorDeviceClass.ENERGY_STORAGE,
                         SensorStateClass.MEASUREMENT, dsn=dsn)
        self.entity_id = f"sensor.{slugify(heater_name(coordinator, dsn))}_heat_deficit"


class RecoverySensor(IcommSensor):
    """Minutes of heating left: energy to setpoint / the power the heater
    reports. 0 once satisfied; unknown while it needs energy but isn't heating
    (between cycles), since no rate is known then."""

    _attr_icon = "mdi:timer-sand"

    def __init__(self, coordinator, dsn: str | None = None) -> None:
        super().__init__(coordinator, "recovery_minutes", "Time to recover",
                         UnitOfTime.MINUTES, SensorDeviceClass.DURATION,
                         SensorStateClass.MEASUREMENT, dsn=dsn)

    @property
    def native_value(self):
        # Without telemetry the entity is unavailable (IcommSensor.available).
        data = self._state
        energy, watts = data.get("heat_deficit_wh"), data.get("watts")
        if not isinstance(energy, (int, float)):
            return None
        if energy <= 0:
            return 0
        if not isinstance(watts, (int, float)) or watts <= 0:
            return None
        return round(energy / watts * 60)


class EnergySensor(IcommSensor):
    """The heater's lifetime energy (Block 27 params 7-9, research phase 49): a
    running Wh total of the power it reports, so it can go in HA's Energy
    dashboard. It counts the nominal 356 W / 4500 W, not a measurement, so it
    reads above a meter on the circuit."""

    _attr_suggested_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator, dsn: str | None = None) -> None:
        super().__init__(coordinator, "energy_wh", "Energy", UnitOfEnergy.WATT_HOUR,
                         SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, dsn=dsn)


class EnumSensor(IcommSensor):
    """A translated enum; a value outside the options reads unknown rather than
    taking the entity down (see ModeSensor)."""

    def __init__(self, coordinator, field: str, options: tuple[str, ...],
                 dsn: str | None = None) -> None:
        super().__init__(coordinator, field, None, device_class=SensorDeviceClass.ENUM,
                         dsn=dsn)
        del self._attr_name
        self._attr_translation_key = field
        self._attr_options = list(options)

    @property
    def native_value(self):
        value = super().native_value
        return value if value in self._attr_options else None

    @property
    def extra_state_attributes(self):
        raw = self._state.get(f"{self._field}_raw")
        return {**super().extra_state_attributes, **({"raw": raw} if raw is not None else {})}


class ValveSensor(IcommSensor):
    """Expansion valve opening (handbook maintenance display P7): likely, not
    proven, so diagnostic."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:valve"

    def __init__(self, coordinator, dsn: str | None = None) -> None:
        super().__init__(coordinator, "eev_pulses", "Expansion valve position", "steps",
                         None, SensorStateClass.MEASUREMENT, dsn=dsn)


class FaultSensor(_RelaySensor):
    """The controller's fault code (the faultCode property, posted at boot and
    on change), with the manufacturer's title."""

    _key = "fault"
    _attr_name = "Fault"
    _attr_icon = "mdi:alert-circle-outline"

    def _code(self) -> int | None:
        try:
            return int((self._state.get("device") or {}).get("fault_code"))
        except (TypeError, ValueError):
            return None

    @property
    def native_value(self):
        code = self._code()
        if code is None:
            return None
        return "No fault" if code == 0 else FAULTS.get(code, f"Fault {code}")

    @property
    def extra_state_attributes(self):
        device = self._state.get("device") or {}
        history = self._state.get("fault_history")
        attrs = {"code": self._code(), "source": device.get("fault_source")}
        if isinstance(history, list):
            # The maintenance display's H1-H4, newest first (0 = empty slot).
            attrs["history"] = [FAULTS.get(c, f"Fault {c}") for c in history
                                if isinstance(c, int) and c]
            attrs["history_codes"] = history
        return attrs


class ModeSensor(IcommSensor):
    """Operating mode as a translated enum: "Heat pump", not `heat_pump`.

    Icons per state live in icons.json. A mode the relay has never seen (guest was
    never captured) reads as unknown: an ENUM sensor that reports a value outside
    its options raises inside Home Assistant, and an uncaptured mode must not take
    the entity down.
    """

    _attr_translation_key = "mode"

    def __init__(self, coordinator, dsn: str | None = None) -> None:
        super().__init__(coordinator, "mode", None, device_class=SensorDeviceClass.ENUM,
                         dsn=dsn)
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
        data = self._state
        return {"vacation_days": data.get("vacation_days"), "raw": data.get("mode_raw")}
