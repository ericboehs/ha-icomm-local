"""0.7.4 (research phase 49): lifetime energy, energy capacity (what "Air at
unit" really was), operating state, the heater's hot-water level, the
expansion valve and the fault code -- and Air at unit removed on upgrade."""

from __future__ import annotations

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_integration import STATE, URL, _setup

NEW = {**STATE, "energy_wh": 5225595, "energy_capacity_wh": 1812, "operating_state": "heating",
       "operating_state_raw": 12, "hot_water_level": "low", "eev_pulses": 480,
       "device": {"fault_code": "0"}}


async def test_the_new_sensors(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, state=NEW)
    s = hass.states.get
    energy = s("sensor.icomm_water_heater_energy")
    assert (energy.attributes["device_class"], energy.attributes["state_class"]) == (
        "energy", "total_increasing")
    assert energy.attributes["unit_of_measurement"] == "kWh"          # suggested unit
    assert float(energy.state) == 5225.595
    cap = s("sensor.icomm_water_heater_energy_capacity")
    assert (cap.state, cap.attributes["unit_of_measurement"]) == ("1812", "Wh")
    state = s("sensor.icomm_water_heater_operating_state")
    assert state.state == "heating" and state.attributes["raw"] == 12
    assert state.attributes["options"] == ["idle", "heating"]
    assert s("sensor.icomm_water_heater_hot_water_level").state == "low"
    valve = s("sensor.icomm_water_heater_expansion_valve_position")
    assert (valve.state, valve.attributes["unit_of_measurement"]) == ("480", "steps")
    fault = s("sensor.icomm_water_heater_fault")
    assert (fault.state, fault.attributes["code"]) == ("No fault", 0)
    assert s("sensor.icomm_water_heater_air_at_unit") is None


async def test_fault_titles(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, state={**NEW, "device": {"fault_code": "80"}})
    assert hass.states.get("sensor.icomm_water_heater_fault").state == "Air filter is dirty"


async def test_an_unlisted_fault_and_no_fault_yet(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, state={**NEW, "device": {"fault_code": "77"}})
    assert hass.states.get("sensor.icomm_water_heater_fault").state == "Fault 77"


async def test_an_older_relay_leaves_them_unknown(hass, aioclient_mock):
    """0.7.3 and earlier send none of the new fields (and still send "air")."""
    await _setup(hass, aioclient_mock, state={**STATE, "air": 44.7})
    for key in ("energy", "energy_capacity", "operating_state", "hot_water_level",
                "expansion_valve_position", "fault"):
        assert hass.states.get(f"sensor.icomm_water_heater_{key}").state == "unknown", key
    assert hass.states.get("sensor.icomm_water_heater_air_at_unit") is None


async def test_an_unknown_state_reads_unknown(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, state={**NEW, "operating_state": "defrost",
                                               "hot_water_level": None})
    assert hass.states.get("sensor.icomm_water_heater_operating_state").state == "unknown"


async def test_air_at_unit_is_removed_on_upgrade(hass, aioclient_mock):
    """Legacy or DSN-keyed, the entity goes (its history was Wh read as F);
    nothing else does."""
    entry = MockConfigEntry(domain="icomm_local", data={"url": URL}, title="iCOMM relay")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    air = registry.async_get_or_create("sensor", "icomm_local", "icomm_local_air",
                                       config_entry=entry, suggested_object_id="garage_air")
    dsn_air = registry.async_get_or_create("sensor", "icomm_local",
                                           "icomm_local_AC000W019949045_air", config_entry=entry,
                                           suggested_object_id="other_air")
    kept = registry.async_get_or_create("sensor", "icomm_local", "icomm_local_watts",
                                        config_entry=entry,
                                        suggested_object_id="icomm_water_heater_power")
    from test_multi_heater import STATE_A
    from test_multi_heater import _setup as _setup_entry
    await _setup_entry(hass, aioclient_mock, entry=entry, a={**STATE_A, **{
        k: v for k, v in NEW.items() if k != "device"}, "heaters": ["AC000W019949045"]})
    assert registry.async_get(air.entity_id) is None
    assert registry.async_get(dsn_air.entity_id) is None
    assert registry.async_get(kept.entity_id) is not None
    assert hass.states.get("sensor.icomm_water_heater_energy_capacity").state == "1812"
