"""Reports keep arriving with identical contents: a repair says the data is frozen."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_integration import STATE, URL, _setup

POWER = "sensor.vue_garage_water_heater_power"


def _issue(hass, entry):
    return ir.async_get(hass).async_get_issue("icomm_local", f"telemetry_frozen_{entry.entry_id}")


async def _setup_with_power(hass, aioclient_mock, state, watts, unit="W"):
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set(POWER, str(watts), {"unit_of_measurement": unit})
    aioclient_mock.get(f"{URL}/state", json=state)
    entry = MockConfigEntry(domain="icomm_local", data={CONF_URL: URL},
                            options={"power_entity": POWER}, title="iCOMM relay")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _refresh(hass, entry, aioclient_mock, state):
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/state", json=state)
    await hass.data["icomm_local"][entry.entry_id].async_refresh()
    await hass.async_block_till_done()


async def test_relay_flagged_stale_raises_and_a_change_clears(hass: HomeAssistant, aioclient_mock):
    entry = await _setup(hass, aioclient_mock,
                         {**STATE, "contents_unchanged_s": 6000, "contents_stale": True})
    issue = _issue(hass, entry)
    assert issue.severity is ir.IssueSeverity.WARNING and not issue.is_fixable
    assert issue.translation_placeholders == {"minutes": "100", "power": ""}
    await _refresh(hass, entry, aioclient_mock,
                   {**STATE, "contents_unchanged_s": 4, "contents_stale": False})
    assert _issue(hass, entry) is None


async def test_power_sensor_flags_it_sooner(hass: HomeAssistant, aioclient_mock):
    state = {**STATE, "contents_unchanged_s": 2000, "contents_stale": False}
    entry = await _setup_with_power(hass, aioclient_mock, state, 268.4)
    assert "268 W" in _issue(hass, entry).translation_placeholders["power"]
    hass.states.async_set(POWER, "1.2", {"unit_of_measurement": "W"})     # heater idle
    await _refresh(hass, entry, aioclient_mock, state)
    assert _issue(hass, entry) is None


async def test_kilowatts_and_short_runs(hass: HomeAssistant, aioclient_mock):
    entry = await _setup_with_power(
        hass, aioclient_mock, {**STATE, "contents_unchanged_s": 600}, 0.27, unit="kW")
    assert _issue(hass, entry) is None                     # running, but only 10 minutes
    await _refresh(hass, entry, aioclient_mock, {**STATE, "contents_unchanged_s": 1900})
    assert "270 W" in _issue(hass, entry).translation_placeholders["power"]


async def test_not_while_reports_have_stopped_or_power_unknown(hass, aioclient_mock):
    entry = await _setup_with_power(
        hass, aioclient_mock,
        {**STATE, "fresh": False, "contents_unchanged_s": 9000, "contents_stale": True}, 300)
    assert _issue(hass, entry) is None                     # the stale repair covers that
    hass.states.async_set(POWER, "unavailable")
    await _refresh(hass, entry, aioclient_mock, {**STATE, "contents_unchanged_s": 9000})
    assert _issue(hass, entry) is None                     # older relay: no contents_stale


async def test_options_flow_sets_the_power_sensor_and_reloads(hass: HomeAssistant, aioclient_mock):
    entry = await _setup(hass, aioclient_mock)
    hass.states.async_set(POWER, "250", {"unit_of_measurement": "W", "device_class": "power"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form" and result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"power_entity": POWER})
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert entry.options == {"power_entity": POWER}
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_unload_clears_it(hass: HomeAssistant, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, {**STATE, "contents_stale": True,
                                                "contents_unchanged_s": 6000})
    assert _issue(hass, entry) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, entry) is None
