"""Availability rules for the HA entities, tested without Home Assistant.

Live finding: entities tied to `fresh` went unavailable after 10 quiet minutes,
and Home Assistant silently drops service calls to unavailable entities.
"""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "icomm_const", Path(__file__).parents[1] / "custom_components/icomm_local/const.py")
const = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(const)


def test_fresh_tank_is_available_and_not_stale():
    data = {"online": True, "fresh": True, "setpoint": 140.0}
    assert const.has_telemetry(data) and not const.is_stale(data)


def test_quiet_tank_stays_available_but_is_flagged_stale():
    data = {"online": True, "fresh": False, "setpoint": 140.0, "last_telemetry_age": 796}
    assert const.has_telemetry(data)          # entity stays up, writes are accepted
    assert const.is_stale(data)               # ...and HA shows a warning


def test_relay_with_no_tank_report_yet_is_unavailable_not_stale():
    data = {"online": False, "fresh": False, "setpoint": None}
    assert not const.has_telemetry(data)
    assert not const.is_stale(data)
    assert not const.has_telemetry(None)
