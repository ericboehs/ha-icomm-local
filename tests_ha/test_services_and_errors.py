"""Service calls, relay failures, and the entity properties HA only reads sometimes.

The relay is mocked with aioclient_mock. Every failure mode a household will
eventually hit -- relay down, relay erroring, relay restarting behind a proxy --
should surface as a Home Assistant error with a readable message, never as a
silent success.
"""

from __future__ import annotations

import copy

import pytest
from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_integration import STATE, URL, _setup

DOMAIN = "icomm_local"


async def _call(hass: HomeAssistant, service: str, data: dict | None = None):
    return await hass.services.async_call(DOMAIN, service, data or {}, blocking=True,
                                          return_response=True)


def _posts(aioclient_mock) -> list:
    return [(str(c[1]), c[2]) for c in aioclient_mock.mock_calls if c[0] == "POST"]


# -- services: success --------------------------------------------------------------

async def test_set_setpoint_service_returns_the_queued_write(hass, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    queued = {"kind": "setpoint", "value": 140.0, "status": "queued"}
    aioclient_mock.post(f"{URL}/write", json={"queued": queued, "pending": 1})
    response = await _call(hass, "set_setpoint", {"temperature": "140"})
    assert response == {"queued": queued}
    assert _posts(aioclient_mock)[-1] == (f"{URL}/write", {"setpoint": 140.0})


async def test_set_mode_service_sends_the_relay_mode_name(hass, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.post(f"{URL}/write", json={"queued": {"kind": "mode"}})
    response = await _call(hass, "set_mode", {"mode": "heat_pump"})
    assert response == {"queued": {"kind": "mode"}}
    assert _posts(aioclient_mock)[-1] == (f"{URL}/write", {"mode": "heat_pump"})


async def test_set_mode_rejects_uncaptured_modes_before_the_relay(hass, aioclient_mock) -> None:
    import voluptuous as vol

    await _setup(hass, aioclient_mock)
    with pytest.raises(vol.Invalid):
        await _call(hass, "set_mode", {"mode": "guest"})
    assert _posts(aioclient_mock) == []


async def test_cancel_writes_returns_what_was_dropped(hass, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    dropped = {"setpoint": {"kind": "setpoint", "value": 140.0}}
    aioclient_mock.post(f"{URL}/write/cancel", json={"cancelled": dropped})
    assert await _call(hass, "cancel_writes") == {"cancelled": dropped}
    assert _posts(aioclient_mock)[-1] == (f"{URL}/write/cancel", {})


async def test_missing_fields_in_the_reply_read_as_empty(hass, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.post(f"{URL}/write", json={})
    aioclient_mock.post(f"{URL}/write/cancel", json={})
    assert await _call(hass, "set_setpoint", {"temperature": 140}) == {"queued": {}}
    assert await _call(hass, "cancel_writes") == {"cancelled": {}}


# -- services: failure ------------------------------------------------------------------

async def test_relay_error_message_is_passed_to_the_user(hass, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.post(f"{URL}/write", status=400,
                        json={"error": "setpoint 200.0 outside 95.0-150.0 F"})
    with pytest.raises(HomeAssistantError, match="outside 95.0-150.0 F"):
        await _call(hass, "set_setpoint", {"temperature": 200})


async def test_relay_error_without_a_message_names_the_status(hass, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.post(f"{URL}/write/cancel", status=500, json={})
    with pytest.raises(HomeAssistantError, match="relay returned 500"):
        await _call(hass, "cancel_writes")


@pytest.mark.parametrize("exc", [ClientError("refused"), TimeoutError()])
async def test_unreachable_relay_is_a_readable_error(hass, aioclient_mock, exc) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.post(f"{URL}/write", exc=exc)
    with pytest.raises(HomeAssistantError, match=f"relay unreachable at {URL}/write"):
        await _call(hass, "set_mode", {"mode": "electric"})


async def test_entity_write_to_an_unreachable_relay_raises(hass, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.post(f"{URL}/write", exc=ClientError("refused"))
    with pytest.raises(HomeAssistantError, match="relay unreachable"):
        await hass.services.async_call(
            "water_heater", "set_temperature",
            {"entity_id": "water_heater.icomm_water_heater", "temperature": 140},
            blocking=True)


async def test_non_json_reply_is_a_readable_error(hass, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.post(f"{URL}/write", status=502, text="<html>Bad Gateway</html>")
    with pytest.raises(HomeAssistantError, match="502 with no JSON body"):
        await _call(hass, "set_setpoint", {"temperature": 140})


async def test_services_refuse_without_a_relay(hass) -> None:
    from custom_components.icomm_local.services import async_setup_services

    await async_setup_services(hass)
    with pytest.raises(HomeAssistantError, match="not set up"):
        await _call(hass, "cancel_writes")


async def test_services_refuse_to_guess_between_two_relays(hass, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock)
    coordinators = hass.data[DOMAIN]
    coordinators["second"] = coordinators[entry.entry_id]
    try:
        with pytest.raises(HomeAssistantError, match="more than one"):
            await _call(hass, "set_setpoint", {"temperature": 140})
    finally:
        coordinators.pop("second")
    assert _posts(aioclient_mock) == []


# -- setup and polling failures -----------------------------------------------------------

def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: URL + "/"}, title="iCOMM relay")
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize("mock_kw", [{"exc": TimeoutError()}, {"exc": ClientError()},
                                     {"status": 503, "json": {}},
                                     {"text": "<html>proxy error</html>"}])
async def test_unreachable_relay_at_startup_retries_later(hass, aioclient_mock, mock_kw) -> None:
    aioclient_mock.get(f"{URL}/state", **mock_kw)
    entry = _entry(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.services.has_service(DOMAIN, "set_setpoint")


async def test_trailing_slash_in_the_url_is_tolerated(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/state", json=STATE)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert hass.data[DOMAIN][entry.entry_id].relay_url == URL


async def test_relay_lost_after_startup_marks_entities_unavailable(hass, aioclient_mock) -> None:
    """A failed poll is unavailability (and clears the stale warning: the
    relay being down is a different problem from a quiet tank)."""
    state = copy.deepcopy(STATE)
    state["fresh"] = False
    entry = await _setup(hass, aioclient_mock, state)
    issues = ir.async_get(hass)
    issue_id = f"stale_telemetry_{entry.entry_id}"
    assert issues.async_get_issue(DOMAIN, issue_id) is not None

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/state", status=500, json={})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success is False
    assert hass.states.get("water_heater.icomm_water_heater").state == "unavailable"
    assert hass.states.get("sensor.icomm_water_heater_power").state == "unavailable"
    assert issues.async_get_issue(DOMAIN, issue_id) is None


# -- config flow ------------------------------------------------------------------------------

@pytest.mark.parametrize("mock_kw", [{"exc": ClientError()}, {"exc": TimeoutError()},
                                     {"json": {"ok": False}},
                                     {"text": "<html>proxy error</html>"},
                                     {"json": ["not", "an", "object"]}])
async def test_config_flow_rejects_a_relay_that_is_not_answering(
        hass, aioclient_mock, mock_kw) -> None:
    aioclient_mock.get(f"{URL}/health", **mock_kw)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == "form" and result["errors"] == {}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}


# -- entity properties HA does not always read ---------------------------------------------

def _entity(hass: HomeAssistant, domain: str, entity_id: str):
    return hass.data[domain].get_entity(entity_id)


async def test_sensor_value_is_none_before_any_report(hass, aioclient_mock) -> None:
    state = copy.deepcopy(STATE)
    state.update({"online": False, "setpoint": None})
    await _setup(hass, aioclient_mock, state)
    power = _entity(hass, "sensor", "sensor.icomm_water_heater_power")
    assert power.available is False
    assert power.native_value is None        # not a stale 356 W from the payload


async def test_last_app_write_sensor_describes_the_cloud_write(hass, aioclient_mock) -> None:
    state = copy.deepcopy(STATE)
    state["last_cloud_write"] = {"at": 1_700_000_000.0, "kind": "setpoint",
                                 "value": 138.0, "frame": "BD 40 08 0B 06 3A E4 92",
                                 "replaced_by_local": True}
    state["cloud_writes"] = 2
    await _setup(hass, aioclient_mock, state)
    sensor = hass.states.get("sensor.icomm_water_heater_last_app_write")
    assert sensor.state == "setpoint 138.0"
    assert sensor.attributes["at"] == "2023-11-14T22:13:20+00:00"
    assert sensor.attributes["frame"] == "BD 40 08 0B 06 3A E4 92"
    assert sensor.attributes["replaced_by_local"] is True
    assert sensor.attributes["cloud_writes"] == 2


async def test_set_temperature_without_a_temperature_does_nothing(hass, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    heater = _entity(hass, "water_heater", "water_heater.icomm_water_heater")
    await heater.async_set_temperature()
    assert _posts(aioclient_mock) == []
