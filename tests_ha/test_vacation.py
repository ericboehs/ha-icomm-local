"""Vacation mode in Home Assistant: away mode, the mode sensor, the service.

Modelled on HA's own aosmith integration: vacation is away mode, and the
water heater's operation reads "off" while it is on.
"""

from __future__ import annotations

import copy

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant, State
from pytest_homeassistant_custom_component.common import mock_restore_cache
from test_integration import STATE, URL, _setup

ENTITY = "water_heater.icomm_water_heater"


def _vacation(days: int = 1) -> dict:
    state = copy.deepcopy(STATE)
    state.update(mode="vacation", mode_raw=f"{days}/2", vacation_days=days)
    return state


def _posts(aioclient_mock) -> list[dict]:
    return [call[2] for call in aioclient_mock.mock_calls if call[0] == "POST"]


async def _refresh_with(hass, aioclient_mock, entry, state) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/state", json=state)
    aioclient_mock.post(f"{URL}/write", json={"queued": {"kind": "mode"}})
    await hass.data["icomm_local"][entry.entry_id].async_refresh()
    await hass.async_block_till_done()


async def test_vacation_reads_as_away_and_off(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock, _vacation(3))
    heater = hass.states.get(ENTITY)
    assert heater.state == "off"
    assert heater.attributes["away_mode"] == "on"
    assert heater.attributes["vacation_days"] == 3
    mode = hass.states.get("sensor.icomm_water_heater_mode")
    assert mode.state == "vacation"
    assert mode.attributes["vacation_days"] == 3 and mode.attributes["raw"] == "3/2"


async def test_heat_pump_is_not_away(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    heater = hass.states.get(ENTITY)
    assert heater.state == "heat_pump" and heater.attributes["away_mode"] == "off"
    assert heater.attributes["mode_before_vacation"] == "heat_pump"


async def test_away_on_sends_one_day_vacation(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock)
    await _refresh_with(hass, aioclient_mock, entry, STATE)
    await hass.services.async_call("water_heater", "set_away_mode",
                                   {"entity_id": ENTITY, "away_mode": True}, blocking=True)
    assert _posts(aioclient_mock)[-1] == {"mode": "vacation", "days": 1}


async def test_away_off_returns_to_the_mode_before(hass: HomeAssistant, aioclient_mock) -> None:
    """Hybrid before vacation -> hybrid after, not a hard-coded default."""
    hybrid = copy.deepcopy(STATE)
    hybrid["mode"] = "hybrid"
    entry = await _setup(hass, aioclient_mock, hybrid)
    await _refresh_with(hass, aioclient_mock, entry, _vacation())
    assert hass.states.get(ENTITY).attributes["mode_before_vacation"] == "hybrid"
    await hass.services.async_call("water_heater", "set_away_mode",
                                   {"entity_id": ENTITY, "away_mode": False}, blocking=True)
    assert _posts(aioclient_mock)[-1] == {"mode": "hybrid"}


async def test_away_off_with_no_history_uses_heat_pump(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock, _vacation())
    assert hass.states.get(ENTITY).attributes["mode_before_vacation"] is None
    await _refresh_with(hass, aioclient_mock, entry, _vacation())
    await hass.services.async_call("water_heater", "set_away_mode",
                                   {"entity_id": ENTITY, "away_mode": False}, blocking=True)
    assert _posts(aioclient_mock)[-1] == {"mode": "heat_pump"}


async def test_mode_before_vacation_survives_an_ha_restart(
        hass: HomeAssistant, aioclient_mock) -> None:
    mock_restore_cache(hass, [State(ENTITY, "off", {"mode_before_vacation": "electric"})])
    entry = await _setup(hass, aioclient_mock, _vacation())
    assert hass.states.get(ENTITY).attributes["mode_before_vacation"] == "electric"
    await _refresh_with(hass, aioclient_mock, entry, _vacation())
    await hass.services.async_call("water_heater", "set_away_mode",
                                   {"entity_id": ENTITY, "away_mode": False}, blocking=True)
    assert _posts(aioclient_mock)[-1] == {"mode": "electric"}


async def test_a_bogus_restored_mode_is_ignored(hass: HomeAssistant, aioclient_mock) -> None:
    mock_restore_cache(hass, [State(ENTITY, "off", {"mode_before_vacation": "vacation"})])
    await _setup(hass, aioclient_mock, _vacation())
    assert hass.states.get(ENTITY).attributes["mode_before_vacation"] is None


async def test_pending_vacation_shows_away_before_confirmation(
        hass: HomeAssistant, aioclient_mock) -> None:
    state = copy.deepcopy(STATE)
    state["pending"] = {"mode": {"kind": "mode", "value": "vacation", "days": 1,
                                 "status": "delivered"}}
    await _setup(hass, aioclient_mock, state)
    heater = hass.states.get(ENTITY)
    assert heater.state == "off" and heater.attributes["away_mode"] == "on"
    # The confirmed mode is still heat pump: that is what away-off returns to.
    assert heater.attributes["mode_before_vacation"] == "heat_pump"


async def test_set_mode_service_passes_vacation_days(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.post(f"{URL}/write", json={"queued": {"kind": "mode"}})
    await hass.services.async_call("icomm_local", "set_mode",
                                   {"mode": "vacation", "days": 5},
                                   blocking=True, return_response=True)
    assert _posts(aioclient_mock)[-1] == {"mode": "vacation", "days": 5}


@pytest.mark.parametrize("data", [{"mode": "heat_pump", "days": 2},
                                  {"mode": "vacation", "days": 0},
                                  {"mode": "vacation", "days": 100}])
async def test_set_mode_service_refuses_bad_days(hass: HomeAssistant, aioclient_mock, data) -> None:
    await _setup(hass, aioclient_mock)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call("icomm_local", "set_mode", data,
                                       blocking=True, return_response=True)
    assert _posts(aioclient_mock) == []
