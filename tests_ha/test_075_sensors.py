"""0.7.5: "Heat deficit" is "Energy to setpoint" (same entity ID, fresh
installs too), and Time to recover = energy / power."""

from __future__ import annotations

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_integration import STATE, URL, _setup


async def test_renamed_but_the_entity_id_is_kept_on_a_fresh_install(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, state={**STATE, "heat_deficit_wh": 420})
    state = hass.states.get("sensor.icomm_water_heater_heat_deficit")
    assert state.state == "420"
    assert state.attributes["friendly_name"] == "iCOMM Water Heater Energy to setpoint"
    assert hass.states.get("sensor.icomm_water_heater_energy_to_setpoint") is None


async def test_an_existing_customised_id_survives(hass, aioclient_mock):
    entry = MockConfigEntry(domain="icomm_local", data={"url": URL}, title="iCOMM relay")
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create("sensor", "icomm_local", "icomm_local_heat_deficit_wh",
                                           config_entry=entry,
                                           suggested_object_id="garage_deficit")
    aioclient_mock.get(f"{URL}/state", json={**STATE, "heat_deficit_wh": 420})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.garage_deficit").state == "420"


async def test_the_second_heater_gets_its_own_heat_deficit_id(hass, aioclient_mock):
    from test_multi_heater import _setup as _setup_two
    await _setup_two(hass, aioclient_mock)
    assert hass.states.get("sensor.icomm_water_heater_2_heat_deficit") is not None


@pytest.mark.parametrize(("energy", "watts", "expected"), [
    (420, 356, "71"),          # the 23:12 reading: ~70 min of compressor
    (4500, 4500, "60"),        # an element hour
    (0, 0, "0"),               # satisfied
    (300, 0, "unknown"),       # needs energy, not heating: no rate
    (None, 356, "unknown"),
])
async def test_time_to_recover(hass, aioclient_mock, energy, watts, expected):
    await _setup(hass, aioclient_mock, state={**STATE, "heat_deficit_wh": energy,
                                              "watts": watts})
    state = hass.states.get("sensor.icomm_water_heater_time_to_recover")
    assert state.state == expected
    assert state.attributes["unit_of_measurement"] == "min"


async def test_time_to_recover_without_telemetry(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, state={**STATE, "online": False, "fresh": False,
                                              "last_telemetry_age": None})
    assert hass.states.get("sensor.icomm_water_heater_time_to_recover").state in (
        "unknown", "unavailable")
