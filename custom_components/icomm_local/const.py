DOMAIN = "icomm_local"
POLL_SECONDS = 10
# The relay answers locally; this is only generous enough to ride a slow host.
TIMEOUT_SECONDS = 10

# The mode names the relay can write and decode. Vacation was captured from the
# app (`01 02` for "1 day"); guest never has been, so it is absent on purpose
# rather than guessed at.
MODES = ("electric", "hybrid", "heat_pump", "vacation")
# Vacation's day count lives in the mode register's first byte (captured from the
# app as 1 and 3 days). Whether the controller counts it down is not yet observed.
VACATION_DEFAULT_DAYS = 1
MAX_VACATION_DAYS = 99


def as_dict(value) -> dict:
    """A nested object from the relay's JSON, or {} if it isn't one: a relay
    bug or a proxy must not raise inside a coordinator listener."""
    return value if isinstance(value, dict) else {}


def as_number(value) -> int | float | None:
    """An int or float from the relay's JSON (not a bool), else None."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def has_telemetry(data: dict | None) -> bool:
    """Tank entities stay available while the relay answers and has *ever* decoded
    a report. Live finding: tying availability to `fresh` made an idle tank go
    unavailable after 10 quiet minutes, and Home Assistant then silently drops
    service calls to an unavailable entity -- writes vanished with only a log line.
    Staleness is a warning (attribute + Repairs issue), not unavailability."""
    data = data or {}
    return bool(data.get("online")) or data.get("setpoint") is not None


def is_stale(data: dict | None) -> bool:
    """Reachable relay, known tank, but no fresh report: warn, do not hide."""
    data = data or {}
    return has_telemetry(data) and not data.get("fresh")

# The controller's fault codes and the manufacturer's titles (via
# stumpylog/aosmith-ble's docs/protocol/faults.md and the service handbook,
# research phase 49). 25-27 are named per sensor as the handbook does.
# The Service Handbook's diagnostic code chart (2000620230 pp. 15-17, which
# lists HPS10-50H45DV; see docs/REFERENCES.md). 2, 101 and 102 come from
# stumpylog/aosmith-ble's table and appear in no manual.
FAULTS = {
    1: "Dry fire", 2: "High water temperature", 3: "Upper thermistor failure",
    4: "Lower thermistor failure", 6: "Internal processor error",
    9: "Power supply voltage error", 21: "Upper element circuit failure",
    22: "Lower element circuit failure", 25: "Coil temperature sensor error",
    26: "Suction temperature sensor error", 27: "Discharge temperature sensor error",
    28: "Ambient temperature sensor failure", 29: "Outlet temperature sensor error",
    31: "Water leak", 44: "Anode depleted", 46: "Water shut-off valve error",
    47: "Smart valve failure", 48: "Battery low", 80: "Air filter is dirty",
    81: "Condensate management error", 83: "Compressor low pressure",
    84: "Compressor error", 85: "High discharge temperature", 86: "Fan error",
    101: "Upper thermostat error", 102: "Lower thermostat error",
}

# Kept in the config entry (#18): the primary heater's DSN once known, and the
# number each heater's device is named with ("iCOMM Water Heater 2").
CONF_DSN = "dsn"
CONF_NUMBERS = "heater_numbers"

# Options: a power sensor on the heater's own circuit, for the frozen-data check.
CONF_POWER_ENTITY = "power_entity"
FROZEN_POWER_W = 150          # above this the heater is running (heat pump ~260 W)
FROZEN_WITH_POWER_S = 1800    # unchanged this long while drawing power: frozen

# Options: a switch that cuts the heater's network access while off (the UniFi
# integration's block-client switch). When the relay says the cloud side is
# down, it's turned off this long and on again (research phase 53).
CONF_NETWORK_SWITCH = "network_switch"
NETWORK_OFF_SECONDS = 90
NETWORK_RESET_COOLDOWN = 6 * 3600   # at most one reset this often
NETWORK_ON_RETRIES = 5
NETWORK_ON_RETRY_SECONDS = 30
