"""The integration, inside real Home Assistant, against a mocked relay.

Written after reading found `session.get(..., timeout_seconds=...)` in both setup
paths -- not an aiohttp keyword, so the integration would have raised on its first
refresh. Nothing had ever run it inside HA. This does.
"""

from __future__ import annotations

import copy

from homeassistant import config_entries
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

URL = "http://relay.test:8124"

STATE = {
    "online": True, "fresh": True, "water_top": 129.0, "water_bottom": 128.0,
    "setpoint": 150.0, "max_setpoint": 150.0, "mode": "heat_pump", "watts": 356,
    "volts": 233, "air": 49.0, "rssi": -45, "last_telemetry_age": 12.0,
    "upstream_errors": 0, "pending": {}, "last_write": None,
    "health": {"state": "healthy", "reasons": [], "unobservable": ["ops UDP"]},
    "doorbell": {"enabled": True, "rings": 0, "last": None}, "writes_confirmed": 0,
}


async def _setup(hass: HomeAssistant, aioclient_mock, state=None,
                 units=US_CUSTOMARY_SYSTEM) -> MockConfigEntry:
    hass.config.units = units
    aioclient_mock.get(f"{URL}/state", json=state or STATE)
    entry = MockConfigEntry(domain="icomm_local", data={CONF_URL: URL}, title="iCOMM relay")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_config_flow_checks_the_relay(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/health", json={"ok": True})
    aioclient_mock.get(f"{URL}/state", json=STATE)
    result = await hass.config_entries.flow.async_init(
        "icomm_local", context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL})
    assert result["type"] == "create_entry"


async def test_config_flow_unreachable(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/health", status=502)
    result = await hass.config_entries.flow.async_init(
        "icomm_local", context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_entities_appear(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    wh = hass.states.get("water_heater.icomm_water_heater")
    assert wh is not None, sorted(hass.states.async_entity_ids())
    assert wh.state == "heat_pump"
    assert wh.attributes["temperature"] == 150.0
    assert wh.attributes["current_temperature"] == 129.0
    assert wh.attributes["max_temp"] == 150.0
    assert set(wh.attributes["operation_list"]) == {"electric", "heat_pump", "eco"}
    assert hass.states.get("sensor.icomm_water_heater_relay_health").state == "healthy"
    assert hass.states.get("sensor.icomm_water_heater_pending_write").state == "none"
    assert hass.states.get("sensor.icomm_water_heater_power").state == "356"


async def test_set_temperature_posts_to_the_relay(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.post(f"{URL}/write", json={"queued": {}, "pending": 1, "doorbell": True})
    await hass.services.async_call(
        "water_heater", "set_temperature",
        {"entity_id": "water_heater.icomm_water_heater", "temperature": 140}, blocking=True)
    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert posts and posts[-1][2] == {"setpoint": 140.0}


async def test_set_mode_maps_eco_to_hybrid(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.post(f"{URL}/write", json={"queued": {}, "pending": 1})
    await hass.services.async_call(
        "water_heater", "set_operation_mode",
        {"entity_id": "water_heater.icomm_water_heater", "operation_mode": "eco"}, blocking=True)
    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert posts[-1][2] == {"mode": "hybrid"}


async def test_pending_value_is_shown_until_confirmed(hass: HomeAssistant, aioclient_mock) -> None:
    """Otherwise the card snaps back to the old value while the write is in flight."""
    state = copy.deepcopy(STATE)
    state["pending"] = {"setpoint": {"kind": "setpoint", "value": 140.0}}
    await _setup(hass, aioclient_mock, state)
    wh = hass.states.get("water_heater.icomm_water_heater")
    assert wh.attributes["temperature"] == 140.0
    assert wh.attributes["confirmed_setpoint"] == 150.0
    assert hass.states.get("sensor.icomm_water_heater_pending_write").state == "setpoint 140.0"


async def test_stale_relay_marks_unavailable(hass: HomeAssistant, aioclient_mock) -> None:
    state = copy.deepcopy(STATE)
    state["fresh"] = False
    await _setup(hass, aioclient_mock, state)
    assert hass.states.get("water_heater.icomm_water_heater").state == "unavailable"
    # The relay itself is still answering, so its own health stays visible.
    assert hass.states.get("sensor.icomm_water_heater_relay_health").state == "healthy"


async def test_services_still_registered(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    for service in ("set_setpoint", "set_mode", "cancel_writes"):
        assert hass.services.has_service("icomm_local", service)


async def test_unload(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not hass.services.has_service("icomm_local", "set_setpoint")


async def test_metric_home_converts_both_ways(hass: HomeAssistant, aioclient_mock) -> None:
    """A Celsius household sees Celsius, and the tank still receives Fahrenheit."""
    await _setup(hass, aioclient_mock, units=METRIC_SYSTEM)
    wh = hass.states.get("water_heater.icomm_water_heater")
    assert round(wh.attributes["temperature"]) == 66          # 150 F
    aioclient_mock.post(f"{URL}/write", json={"queued": {}, "pending": 1})
    await hass.services.async_call(
        "water_heater", "set_temperature",
        {"entity_id": "water_heater.icomm_water_heater", "temperature": 60}, blocking=True)
    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert posts[-1][2] == {"setpoint": 140.0}                  # 60 C


async def test_mode_is_a_translated_enum(hass: HomeAssistant, aioclient_mock) -> None:
    """Raw `heat_pump` with an eye icon on the dashboard was the bug report."""
    await _setup(hass, aioclient_mock)
    state = hass.states.get("sensor.icomm_water_heater_mode")   # entity_id unchanged
    assert state.state == "heat_pump"
    assert state.attributes["device_class"] == "enum"
    assert state.attributes["options"] == ["electric", "hybrid", "heat_pump"]
    assert state.attributes["friendly_name"] == "iCOMM Water Heater Mode"
    from homeassistant.helpers import entity_registry as er
    entry = er.async_get(hass).async_get("sensor.icomm_water_heater_mode")
    assert entry.translation_key == "mode"


async def test_mode_icons_and_labels_ship(hass: HomeAssistant) -> None:
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "custom_components" / "icomm_local"
    icons = json.loads((root / "icons.json").read_text())["entity"]["sensor"]["mode"]["state"]
    labels = json.loads((root / "translations" / "en.json").read_text())
    labels = labels["entity"]["sensor"]["mode"]["state"]
    for mode in ("electric", "hybrid", "heat_pump"):
        assert icons[mode].startswith("mdi:") and labels[mode]


async def test_uncaptured_mode_reads_unknown_not_an_error(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """guest/vacation were never captured; an ENUM outside its options raises in HA."""
    state = copy.deepcopy(STATE)
    state["mode"] = "vacation"
    await _setup(hass, aioclient_mock, state)
    assert hass.states.get("sensor.icomm_water_heater_mode").state == "unknown"
