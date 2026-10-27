"""Review fixes: HA-version compatibility, malformed relay replies, and guards."""

from __future__ import annotations

import copy
import logging

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_integration import STATE, URL, _setup
from test_multi_heater import STATE_A, A, B
from test_multi_heater import _refresh as _refresh_multi
from test_multi_heater import _setup as _setup_multi

from custom_components.icomm_local.config_flow import IcommLocalOptionsFlow
from custom_components.icomm_local.sensor import ICON_TRANSLATIONS

DOMAIN = "icomm_local"


def _entity(hass: HomeAssistant, domain: str, entity_id: str):
    return hass.data[domain].get_entity(entity_id)


def _issue(hass, issue_id):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


async def _refresh(hass, entry, aioclient_mock, state) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/state", json=state)
    await hass.data[DOMAIN][entry.entry_id].async_refresh()
    await hass.async_block_till_done()


# -- 1: the options flow on HA before 2024.11 ---------------------------------------------

async def test_options_flow_needs_no_config_entry_attribute(hass, aioclient_mock) -> None:
    """HA sets OptionsFlow.config_entry itself only from 2024.11: a flow built
    the way older HA builds it (no entry) must still show its form."""
    entry = await _setup(hass, aioclient_mock)
    hass.config_entries.async_update_entry(entry, options={"power_entity": "sensor.p"})
    await hass.async_block_till_done()                     # the options reload
    flow = IcommLocalOptionsFlow()
    flow.hass, flow.handler = hass, entry.entry_id
    result = await flow.async_step_init()
    assert result["type"] == "form"
    field = next(iter(result["data_schema"].schema))
    assert field.description == {"suggested_value": "sensor.p"}
    flow.handler = "gone"                                  # entry removed meanwhile
    result = await flow.async_step_init()
    assert next(iter(result["data_schema"].schema)).description == {"suggested_value": None}


# -- 5: the same relay twice ------------------------------------------------------------------

async def _user_flow(hass, url):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: url})


async def test_the_same_relay_twice_is_refused(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/health", json={"ok": True})
    aioclient_mock.get(f"{URL}/state", json=STATE)
    first = await _user_flow(hass, URL)
    assert first["type"] == "create_entry"
    assert first["result"].unique_id == URL
    again = await _user_flow(hass, f" {URL.upper()}/ ")
    assert again["type"] == "abort" and again["reason"] == "already_configured"


async def test_an_entry_from_before_unique_ids_is_matched_by_url(hass, aioclient_mock) -> None:
    MockConfigEntry(domain=DOMAIN, data={CONF_URL: f"{URL}/"}).add_to_hass(hass)
    aioclient_mock.get(f"{URL}/health", json={"ok": True})
    result = await _user_flow(hass, URL)
    assert result["type"] == "abort" and result["reason"] == "already_configured"
    assert aioclient_mock.call_count == 0                  # not even probed


# -- 8: the /state reply ---------------------------------------------------------------------

@pytest.mark.parametrize("mock_kw", [{"json": ["not", "an", "object"]}, {"json": "text"},
                                     {"status": 503, "json": {"error": "busy"}}])
async def test_a_bad_state_reply_is_an_update_failure(hass, aioclient_mock, mock_kw) -> None:
    aioclient_mock.get(f"{URL}/state", **mock_kw)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: URL})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is config_entries.ConfigEntryState.SETUP_RETRY


async def test_a_bad_reply_after_setup_marks_the_entities_unavailable(
        hass, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock)
    await _refresh(hass, entry, aioclient_mock, [1, 2])
    assert not hass.data[DOMAIN][entry.entry_id].last_update_success
    assert hass.states.get("sensor.icomm_water_heater_power").state == "unavailable"


# -- 10: the coordinator knows its entry -----------------------------------------------------

async def test_the_coordinator_is_given_its_entry(hass, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock)
    assert hass.data[DOMAIN][entry.entry_id].config_entry is entry


# -- 2: malformed fields in /state ------------------------------------------------------------

MALFORMED = {
    **STATE, "fresh": True, "contents_stale": True, "contents_unchanged_s": "abc",
    "reporting_mask": "on", "upstream_tls": "failing", "dns": ["x"], "lan": "up",
    "health": "healthy", "device": "AC000W", "firmware": {"held": ["x", 3]},
    "pending": {"setpoint": "140", "mode": {"value": ["electric"]}},
    "last_cloud_write": {"at": 1e20, "kind": "mode", "value": "electric"},
    "max_setpoint": "abc", "watts": "abc", "volts": True, "mode": ["heat_pump"],
    "setpoint": "150", "water_bottom": "x", "water_top": None, "last_telemetry_age": "x",
    "fault_history": [3, "x"], "heater": "AC000W",
}


async def test_malformed_fields_do_not_take_anything_down(hass, aioclient_mock, caplog) -> None:
    entry = await _setup(hass, aioclient_mock, MALFORMED)
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert "failed" not in caplog.text
    assert hass.states.get("sensor.icomm_water_heater_power").state == "unknown"
    assert hass.states.get("sensor.icomm_water_heater_line_voltage").state == "unknown"
    assert hass.states.get("sensor.icomm_water_heater_relay_health").state == "unknown"
    assert hass.states.get("sensor.icomm_water_heater_pending_write").state == \
        "mode ['electric']: queued, setpoint None: queued"
    assert hass.states.get("sensor.icomm_water_heater_last_app_write").attributes["at"] is None
    heater = hass.states.get("water_heater.icomm_water_heater")
    assert heater.attributes["max_temp"] == 140.0
    assert heater.attributes["temperature"] is None
    assert heater.attributes["current_temperature"] is None
    frozen = _issue(hass, f"telemetry_frozen_{entry.entry_id}")
    assert frozen.translation_placeholders["minutes"] == "0"
    assert "turn on the add-on's" in frozen.translation_placeholders["fix"]
    assert _issue(hass, f"upstream_certificate_{entry.entry_id}") is None
    # Then a good reply: everything updates again.
    await _refresh(hass, entry, aioclient_mock, STATE)
    assert hass.states.get("sensor.icomm_water_heater_power").state == "356"
    assert hass.states.get("water_heater.icomm_water_heater").state == "heat_pump"


async def test_malformed_nested_fields(hass, aioclient_mock) -> None:
    state = {**STATE, "fresh": True, "contents_stale": True, "contents_unchanged_s": 3600,
             "reporting_mask": {"renew": True, "after_s": "x", "sent": "y"},
             "health": {"state": "degraded", "code": "cloud_idle", "evidence": "x"},
             "upstream_tls": {"failing": True, "last_ok": "x"},
             "firmware": {"held": ["x", {"key": "k", "type": "module", "version": "2"}]},
             "pending": {"setpoint": "x"}, "last_cloud_write": "x"}
    entry = await _setup(hass, aioclient_mock, state)
    fix = _issue(hass, f"telemetry_frozen_{entry.entry_id}").translation_placeholders["fix"]
    assert "after 30 minutes" in fix and "(0 sent" in fix
    idle = _issue(hass, f"cloud_idle_{entry.entry_id}")
    assert idle.translation_placeholders["minutes"] == "0"
    tls = _issue(hass, f"upstream_certificate_{entry.entry_id}")
    assert tls.translation_placeholders["since"] == "not since the relay started"
    issues = [i for (d, i) in ir.async_get(hass).issues if d == DOMAIN
              and i.startswith("firmware_held")]
    assert len(issues) == 1
    pending = hass.states.get("sensor.icomm_water_heater_pending_write")
    assert pending.state == "setpoint None: queued"
    assert pending.attributes["status"] == {"setpoint": None}
    assert hass.states.get("sensor.icomm_water_heater_last_app_write").state == "none"


async def test_one_failing_listener_leaves_the_others(hass, aioclient_mock, monkeypatch,
                                                      caplog) -> None:
    def broken(*_args, **_kwargs):
        raise KeyError("boom")

    monkeypatch.setattr("custom_components.icomm_local._tls_issue", broken)
    entry = await _setup(hass, aioclient_mock)
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert "_update_tls_issue failed on the relay's state (KeyError)" in caplog.text
    # Registered after it: the DNS repair and the entities still update.
    await _refresh(hass, entry, aioclient_mock,
                   {**STATE, "watts": 4500, "dns": {"enabled": True, "error": "port 53"}})
    assert _issue(hass, f"dns_server_failed_{entry.entry_id}") is not None
    assert hass.states.get("sensor.icomm_water_heater_power").state == "4500"


async def test_one_failing_entity_leaves_the_others(hass, aioclient_mock, monkeypatch,
                                                    caplog) -> None:
    entry = await _setup(hass, aioclient_mock)

    def broken(_self):
        raise ValueError("boom")

    monkeypatch.setattr("custom_components.icomm_local.sensor.IcommSensor.native_value",
                        property(broken))
    caplog.set_level(logging.ERROR)
    await _refresh(hass, entry, aioclient_mock, {**STATE, "mode": "electric"})
    assert "failed to update (ValueError)" in caplog.text
    assert hass.states.get("water_heater.icomm_water_heater").state == "electric"


# -- 3, 6: numeric sensors -----------------------------------------------------------------

async def test_tank_sensors_are_measurements(hass, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock, {**STATE, "water_mid": 120.0})
    for name in ("top", "bottom", "average"):
        state = hass.states.get(f"sensor.icomm_water_heater_water_tank_{name}")
        assert state.attributes["state_class"] == "measurement", name


async def test_numeric_sensors_report_only_numbers(hass, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock, {**STATE, "watts": "356", "rssi": False})
    assert hass.states.get("sensor.icomm_water_heater_power").state == "unknown"
    assert hass.states.get("sensor.icomm_water_heater_wi_fi_rssi").state == "unknown"
    assert hass.states.get("sensor.icomm_water_heater_mode").state == "heat_pump"  # enums kept


# -- 4: writes to a heater the relay dropped --------------------------------------------------

async def test_a_dropped_second_heater_is_not_written_to_the_primary(
        hass, aioclient_mock) -> None:
    entry = await _setup_multi(hass, aioclient_mock)
    await _refresh_multi(hass, entry, aioclient_mock, a={**STATE_A, "heaters": [A]})
    second = _entity(hass, "water_heater", "water_heater.icomm_water_heater_2")
    with pytest.raises(HomeAssistantError, match="no longer lists this water heater"):
        await second.async_set_temperature(temperature=130)
    assert not [c for c in aioclient_mock.mock_calls if c[0] == "POST"]


async def test_a_relay_without_the_heaters_list_takes_bare_writes(hass) -> None:
    from types import SimpleNamespace

    from custom_components.icomm_local.heaters import relay_path

    coordinator = SimpleNamespace(data={**STATE}, primary=A)
    assert relay_path(coordinator, B, "/write") == "/write"            # pre-0.7 relay
    coordinator.data["heaters"] = [A]
    assert relay_path(coordinator, A, "/write") == f"/heaters/{A}/write"
    assert relay_path(coordinator, None, "/write") == "/write"


# -- 11: icons on HA before 2024.2 ------------------------------------------------------------

async def test_state_icons_without_icon_translations(hass, aioclient_mock, monkeypatch) -> None:
    monkeypatch.setattr("custom_components.icomm_local.sensor.ICON_TRANSLATIONS", False)
    await _setup(hass, aioclient_mock, {**STATE, "heating_element": "upper"})
    assert hass.states.get("sensor.icomm_water_heater_mode").attributes["icon"] == \
        "mdi:heat-pump"
    assert hass.states.get("sensor.icomm_water_heater_heating_element").attributes["icon"] == \
        "mdi:arrow-up-bold-box"


async def test_icons_json_is_left_to_do_it(hass, aioclient_mock) -> None:
    """From 2024.2 icons.json does it; before that, the Python fallback."""
    await _setup(hass, aioclient_mock)
    attrs = hass.states.get("sensor.icomm_water_heater_mode").attributes
    assert ("icon" in attrs) is not ICON_TRANSLATIONS


# -- 12: "off" ------------------------------------------------------------------------------

async def test_off_is_refused_not_written(hass, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock, copy.deepcopy(STATE))
    heater = _entity(hass, "water_heater", "water_heater.icomm_water_heater")
    with pytest.raises(ServiceValidationError, match="away mode"):
        await heater.async_set_operation_mode("off")
    assert not [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
