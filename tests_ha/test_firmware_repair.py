"""A firmware update the relay holds becomes a fixable repair; fixing it allows it."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from test_integration import STATE, URL, _setup

from custom_components.icomm_local import firmware_issue_id
from custom_components.icomm_local.repairs import AllowFirmwareFlow, async_create_fix_flow

OFFER = {"key": "module:bc 2.12.0", "type": "module", "version": "bc 2.12.0",
         "size": 412000, "url_host": "ota.example-s3.test", "source": "remote"}
HELD = {**STATE, "firmware": {"policy": "hold", "agent_version": "bc 2.11.4",
                              "held": [OFFER], "allowed": [], "status_reports": []}}


async def _refresh(hass, entry, aioclient_mock, state) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/state", json=state)
    await hass.data["icomm_local"][entry.entry_id].async_refresh()
    await hass.async_block_till_done()


async def test_a_held_update_raises_a_fixable_repair(hass: HomeAssistant, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, state=HELD)
    issue_id = firmware_issue_id(entry.entry_id, OFFER["key"])
    issue = ir.async_get(hass).async_get_issue("icomm_local", issue_id)
    assert issue is not None and issue.is_fixable
    assert issue.translation_key == "firmware_held"
    assert issue.translation_placeholders == {
        "target": "Wi-Fi module", "version": "bc 2.12.0", "size": "402 KB",
        "host": "ota.example-s3.test", "agent": "bc 2.11.4"}
    assert issue.data["key"] == "module:bc 2.12.0"

    await _refresh(hass, entry, aioclient_mock, STATE)      # no longer held
    assert ir.async_get(hass).async_get_issue("icomm_local", issue_id) is None


async def test_a_controller_update_with_odd_fields(hass: HomeAssistant, aioclient_mock):
    odd = {**OFFER, "key": "host_mcu:?", "type": "host_mcu", "version": "?", "size": None,
           "url_host": None}
    state = {**STATE, "firmware": {"held": [odd]}}
    entry = await _setup(hass, aioclient_mock, state=state)
    issue = ir.async_get(hass).async_get_issue(
        "icomm_local", firmware_issue_id(entry.entry_id, "host_mcu:?"))
    assert issue.translation_placeholders["target"] == "water heater controller"
    assert issue.translation_placeholders["size"] == "unknown size"
    assert issue.translation_placeholders["host"] == "unknown"
    assert issue.translation_placeholders["agent"] == "unknown"


async def test_unloading_removes_the_repairs(hass: HomeAssistant, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, state=HELD)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(
        "icomm_local", firmware_issue_id(entry.entry_id, OFFER["key"])) is None


async def _flow(hass, entry) -> AllowFirmwareFlow:
    issue = ir.async_get(hass).async_get_issue(
        "icomm_local", firmware_issue_id(entry.entry_id, OFFER["key"]))
    flow = await async_create_fix_flow(hass, issue.issue_id, issue.data)
    flow.hass = hass
    return flow


async def test_fixing_it_allows_the_update(hass: HomeAssistant, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, state=HELD)
    flow = await _flow(hass, entry)
    form = await flow.async_step_init()
    assert form["type"] is FlowResultType.FORM and form["step_id"] == "confirm"
    assert form["description_placeholders"]["version"] == "bc 2.12.0"

    aioclient_mock.post(f"{URL}/firmware/allow", json={"allowed": OFFER["key"]})
    done = await flow.async_step_confirm({})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert posts[-1][2] == {"key": "module:bc 2.12.0"}


async def test_a_relay_that_refuses_aborts(hass: HomeAssistant, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, state=HELD)
    flow = await _flow(hass, entry)
    aioclient_mock.post(f"{URL}/firmware/allow", status=404, json={"error": "gone"})
    result = await flow.async_step_confirm({})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "relay_refused"


async def test_an_unloaded_integration_aborts(hass: HomeAssistant, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, state=HELD)
    flow = await _flow(hass, entry)
    flow._data = {**flow._data, "entry_id": "gone"}
    result = await flow.async_step_confirm({})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "not_loaded"


async def test_a_flow_without_data_still_builds(hass: HomeAssistant):
    flow = await async_create_fix_flow(hass, "x", None)
    assert flow._data == {}
