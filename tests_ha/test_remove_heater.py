"""Deleting a heater's device in HA (#18): the relay forgets it, then HA
removes the device and its entities. _delete does what HA's
config/device_registry/remove_config_entry command (the device page's Delete)
does: ask the integration's hook, and on True drop the device."""

from __future__ import annotations

from datetime import timedelta

import aiohttp
import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from test_integration import URL
from test_multi_heater import STATE_A, STATE_B, A, B, _device, _relay, _setup

GONE_B = {**STATE_B, "fresh": False, "online": False}


async def _delete(hass, hass_ws_client, entry, dsn):
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import device_registry as dr
    from homeassistant.loader import async_get_integration
    component = (await async_get_integration(hass, "icomm_local")).get_component()
    device = _device(hass, entry, dsn)
    try:
        allowed = await component.async_remove_config_entry_device(hass, entry, device)
    except HomeAssistantError as err:
        return {"success": False, "error": {"message": str(err)}}
    if not allowed:
        return {"success": False, "error": {"message": "rejected"}}
    dr.async_get(hass).async_remove_device(device.id)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))   # the reload
    return {"success": True}


def _forget(aioclient_mock, dsn, status=200, json=None, exc=None):
    url, params = f"{URL}/heaters/{dsn}", {"confirm": dsn}
    if isinstance(json, str):
        aioclient_mock.delete(url, params=params, status=status, text=json)
    else:
        aioclient_mock.delete(url, params=params, status=status, exc=exc,
                              json=json if json is not None else {"removed": dsn})


def _deletes(aioclient_mock):
    return [str(url) for method, url, *_ in aioclient_mock.mock_calls if method == "DELETE"]


async def test_deleting_a_gone_heater(hass, hass_ws_client, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, b=GONE_B)
    ir.async_create_issue(hass, "icomm_local", f"stale_telemetry_{entry.entry_id}_{B}",
                          is_fixable=False, severity=ir.IssueSeverity.WARNING,
                          translation_key="stale_telemetry")
    _relay(aioclient_mock, a={**STATE_A, "heaters": [A]})      # what it says once restarted
    _forget(aioclient_mock, B)
    reply = await _delete(hass, hass_ws_client, entry, B)
    assert reply["success"], reply
    assert _deletes(aioclient_mock) == [f"{URL}/heaters/{B}?confirm={B}"]
    await hass.async_block_till_done()                         # the reload
    assert _device(hass, entry, B) is None
    assert not [e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
                if B in e.unique_id]
    assert _device(hass, entry, A) is not None                 # A untouched
    assert entry.data["heater_numbers"] == {A: 1}              # B's number is free
    assert ir.async_get(hass).async_get_issue(
        "icomm_local", f"stale_telemetry_{entry.entry_id}_{B}") is None


async def test_a_heater_still_reporting_is_refused(hass, hass_ws_client, aioclient_mock):
    entry = await _setup(hass, aioclient_mock)                 # B fresh
    reply = await _delete(hass, hass_ws_client, entry, B)
    assert not reply["success"] and "still reporting" in reply["error"]["message"]
    assert _deletes(aioclient_mock) == [] and _device(hass, entry, B) is not None


@pytest.mark.parametrize(("status", "body", "exc", "message"), [
    (409, {"error": "the module_ip or device_dsn option names this heater"}, None,
     "The relay refused: the module_ip"),
    (405, {}, None, "too old"),
    (500, {}, None, "HTTP 500"),
    (404, "404: Not Found", None, "too old"),                  # plain text: no such route
    (200, {}, aiohttp.ClientError("boom"), "Couldn't reach the relay"),
])
async def test_the_relay_says_no(hass, hass_ws_client, aioclient_mock, status, body, exc,
                                 message):
    entry = await _setup(hass, aioclient_mock, b=GONE_B)
    _forget(aioclient_mock, B, status, body, exc)
    reply = await _delete(hass, hass_ws_client, entry, B)
    assert not reply["success"] and message in reply["error"]["message"]
    assert _device(hass, entry, B) is not None


async def test_the_relay_had_already_forgotten_it(hass, hass_ws_client, aioclient_mock):
    """Its own 404 ("no such heater") counts as done."""
    entry = await _setup(hass, aioclient_mock, b=GONE_B)
    _forget(aioclient_mock, B, 404, {"error": "no such heater", "heaters": [A]})
    assert (await _delete(hass, hass_ws_client, entry, B))["success"]


async def test_a_heater_the_relay_no_longer_lists(hass, hass_ws_client, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, b=GONE_B)
    coordinator = hass.data["icomm_local"][entry.entry_id]
    coordinator.data = {**coordinator.data, "heaters": [A]}    # forgotten since the last poll
    reply = await _delete(hass, hass_ws_client, entry, B)
    assert reply["success"] and _deletes(aioclient_mock) == []


async def test_deleting_the_primary_clears_the_stored_dsn(hass, hass_ws_client, aioclient_mock):
    """The single-heater replacement: nothing falls back to the forgotten DSN."""
    gone = {**STATE_A, "heaters": [A], "fresh": False}
    entry = await _setup(hass, aioclient_mock, a=gone)
    assert entry.data["dsn"] == A
    from test_integration import STATE
    _relay(aioclient_mock, a={**STATE, "heaters": []})         # restarted with no heater
    _forget(aioclient_mock, A)
    assert (await _delete(hass, hass_ws_client, entry, A))["success"]
    await hass.async_block_till_done()
    assert entry.data["dsn"] is None                           # nothing brought it back
    # The single-heater keys stand in, under the default entity IDs, until
    # the new heater names its DSN -- then they migrate to it.
    assert er.async_get(hass).async_get("water_heater.icomm_water_heater").unique_id == (
        "icomm_local_water_heater")
    x2 = "AC000W044444444"                                     # the replacement boots
    new = {**STATE, "heater": {"dsn": x2}, "heaters": [x2], "device": {"dsn": x2}}
    _relay(aioclient_mock, a=new)
    await hass.data["icomm_local"][entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get("water_heater.icomm_water_heater").unique_id == (
        f"icomm_local_{x2}_water_heater")
    assert entry.data["dsn"] == x2 and entry.data["heater_numbers"] == {x2: 1}
    assert _device(hass, entry, x2).name == "iCOMM Water Heater"
    assert not [e for e in hass.states.async_entity_ids() if e.startswith(
        ("water_heater.icomm_water_heater_", "sensor.icomm_water_heater_2"))]


async def test_the_old_relays_device_is_refused(hass, hass_ws_client, aioclient_mock):
    from test_integration import STATE
    entry = await _setup(hass, aioclient_mock, a={**STATE, "fresh": False})  # names no DSN
    reply = await _delete(hass, hass_ws_client, entry, "water_heater")
    assert not reply["success"] and "hasn't named its DSN" in reply["error"]["message"]


async def test_the_reload_waits_out_the_relays_restart(hass, hass_ws_client, aioclient_mock):
    """The relay answers 503 until it has restarted: the reload retries rather
    than re-creating the heater from the old state."""
    from datetime import timedelta

    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    entry = await _setup(hass, aioclient_mock, b=GONE_B)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/state", status=503, json={"error": "restarting"})
    _forget(aioclient_mock, B)
    assert (await _delete(hass, hass_ws_client, entry, B))["success"]
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert _device(hass, entry, B) is None
    _relay(aioclient_mock, a={**STATE_A, "heaters": [A]})      # back up
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED and _device(hass, entry, B) is None
    assert hass.states.get("water_heater.icomm_water_heater_2") is None


async def test_no_reload_if_ha_never_removes_the_device(hass, hass_ws_client, aioclient_mock):
    from datetime import timedelta

    from homeassistant.loader import async_get_integration
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    entry = await _setup(hass, aioclient_mock, b=GONE_B)
    _forget(aioclient_mock, B)
    component = (await async_get_integration(hass, "icomm_local")).get_component()
    device = _device(hass, entry, B)
    assert await component.async_remove_config_entry_device(hass, entry, device)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    gets = len([c for c in aioclient_mock.mock_calls if c[0] == "GET"])
    from homeassistant.helpers import device_registry as dr
    dr.async_get(hass).async_update_device(device.id, name_by_user="still here")  # not a removal
    await hass.async_block_till_done()
    assert len([c for c in aioclient_mock.mock_calls if c[0] == "GET"]) == gets
