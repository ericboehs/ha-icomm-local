"""The heater's cloud side is down: turn its network switch off for 90 s, then on.

Phase 53: after a long relay outage the module stops connecting to Ayla until
its network connection drops. A 90-second UniFi client block, then unblock,
brought it back like a power cycle; a quick kick didn't.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant import config_entries
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)
from test_integration import URL
from test_multi_heater import STATE_A, STATE_B, B, _refresh, _setup

from custom_components.icomm_local.const import (
    NETWORK_OFF_SECONDS,
    NETWORK_ON_RETRIES,
    NETWORK_ON_RETRY_SECONDS,
    NETWORK_RESET_COOLDOWN,
)

SWITCH = "switch.water_heater_network"
DOWN = {"state": "degraded", "code": "cloud_down", "reasons": ["cloud side down"],
        "remediation": "Turn its network off", "evidence": {"cloud_down_s": 600}}
HEALTHY = {"state": "healthy", "reasons": [], "evidence": {}}


def _entry(hass, switch=SWITCH, entry_id="net1") -> MockConfigEntry:
    entry = MockConfigEntry(domain="icomm_local", data={"url": URL}, title="iCOMM relay",
                            entry_id=entry_id,
                            options={"network_switch": switch} if switch else {})
    entry.add_to_hass(hass)
    return entry


def _issue(hass, issue_id):
    return ir.async_get(hass).async_get_issue("icomm_local", issue_id)


@pytest.fixture
def calls(hass):
    return {"off": async_mock_service(hass, "switch", "turn_off"),
            "on": async_mock_service(hass, "switch", "turn_on")}


async def _later(hass, freezer, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def test_off_for_90_seconds_then_on(hass, aioclient_mock, calls, freezer, hass_storage):
    entry = await _setup(hass, aioclient_mock, entry=_entry(hass),
                         a={**STATE_A, "health": DOWN})
    assert [c.data["entity_id"] for c in calls["off"]] == [SWITCH] and not calls["on"]
    assert hass_storage["icomm_local.network_reset.net1"]["data"]["off"] is True
    issue = _issue(hass, f"cloud_idle_{entry.entry_id}")
    assert issue.translation_key == "cloud_down"
    assert SWITCH in issue.translation_placeholders["fix"]
    assert issue.translation_placeholders["minutes"] == "10"

    await _later(hass, freezer, NETWORK_OFF_SECONDS - 1)
    assert not calls["on"]
    await _later(hass, freezer, 1)
    assert [c.data["entity_id"] for c in calls["on"]] == [SWITCH]
    assert hass_storage["icomm_local.network_reset.net1"]["data"]["off"] is False

    attrs = hass.states.get("sensor.icomm_water_heater_relay_health").attributes
    assert attrs["network_reset"]["switch"] == SWITCH and attrs["network_reset"]["resets"] == 1
    assert attrs["network_reset"]["last"] is not None

    # Back to healthy: the repair goes.
    await _refresh(hass, entry, aioclient_mock, a={**STATE_A, "health": HEALTHY})
    assert _issue(hass, f"cloud_idle_{entry.entry_id}") is None


async def test_at_most_once_per_cooldown(hass, aioclient_mock, calls, freezer):
    entry = await _setup(hass, aioclient_mock, entry=_entry(hass),
                         a={**STATE_A, "health": DOWN})
    await _later(hass, freezer, NETWORK_OFF_SECONDS)
    for _ in range(3):                                      # still down: polls continue
        await _refresh(hass, entry, aioclient_mock, a={**STATE_A, "health": DOWN})
    assert len(calls["off"]) == 1
    await _later(hass, freezer, NETWORK_RESET_COOLDOWN)
    await _refresh(hass, entry, aioclient_mock, a={**STATE_A, "health": DOWN})
    assert len(calls["off"]) == 2


async def test_nothing_without_cloud_down(hass, aioclient_mock, calls):
    idle = {**DOWN, "code": "cloud_idle", "evidence": {"cloud_quiet_s": 1800}}
    entry = await _setup(hass, aioclient_mock, entry=_entry(hass),
                         a={**STATE_A, "health": idle})
    assert not calls["off"]
    assert _issue(hass, f"cloud_idle_{entry.entry_id}").translation_key == "cloud_idle"


async def test_only_the_primary_heaters_switch(hass, aioclient_mock, calls):
    entry = await _setup(hass, aioclient_mock, entry=_entry(hass),
                         a={**STATE_A, "health": HEALTHY}, b={**STATE_B, "health": DOWN})
    assert not calls["off"]
    issue = _issue(hass, f"cloud_idle_{entry.entry_id}_{B}")
    assert issue.translation_key == "cloud_down"
    second = hass.states.get("sensor.icomm_water_heater_2_relay_health").attributes
    assert second["network_reset"] is None
    assert "Configure" in issue.translation_placeholders["fix"]


async def test_without_a_switch_the_repair_says_how(hass, aioclient_mock, calls):
    entry = await _setup(hass, aioclient_mock, entry=_entry(hass, switch=None),
                         a={**STATE_A, "health": DOWN})
    assert not calls["off"]
    fix = _issue(hass, f"cloud_idle_{entry.entry_id}").translation_placeholders["fix"]
    attrs = hass.states.get("sensor.icomm_water_heater_relay_health").attributes
    assert attrs["network_reset"] is None
    assert "90 seconds" in fix and "breaker" in fix and "Configure" in fix


async def test_an_interrupted_reset_turns_the_switch_back_on(hass, aioclient_mock, calls,
                                                            hass_storage):
    hass_storage["icomm_local.network_reset.net1"] = {
        "version": 1, "minor_version": 1, "key": "icomm_local.network_reset.net1",
        "data": {"last": dt_util.utcnow().timestamp() - 30, "off": True}}
    await _setup(hass, aioclient_mock, entry=_entry(hass), a={**STATE_A, "health": DOWN})
    assert [c.data["entity_id"] for c in calls["on"]] == [SWITCH]
    assert not calls["off"]                                 # and inside the cooldown


async def test_unloading_mid_reset_turns_it_on(hass, aioclient_mock, calls):
    entry = await _setup(hass, aioclient_mock, entry=_entry(hass),
                         a={**STATE_A, "health": DOWN})
    assert calls["off"] and not calls["on"]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in calls["on"]] == [SWITCH]


async def test_a_switch_that_fails_to_turn_off(hass, aioclient_mock, freezer, caplog):
    async def _fail(call):
        raise HomeAssistantError("controller unreachable")
    hass.services.async_register("switch", "turn_off", _fail)
    on = async_mock_service(hass, "switch", "turn_on")
    entry = await _setup(hass, aioclient_mock, entry=_entry(hass),
                         a={**STATE_A, "health": DOWN})
    assert "couldn't turn switch.water_heater_network off" in caplog.text
    await _later(hass, freezer, NETWORK_OFF_SECONDS)
    assert not on                                           # never turned off: nothing to undo
    reset = hass.data["icomm_local"][entry.entry_id].network_reset
    assert reset.status()["in_progress"] is False and reset.status()["resets"] == 1


async def test_turning_it_back_on_is_retried(hass, aioclient_mock, freezer, caplog):
    off = async_mock_service(hass, "switch", "turn_off")
    attempts = []

    async def _on(call):
        attempts.append(call)
        if len(attempts) < 3:
            raise HomeAssistantError("controller busy")
    hass.services.async_register("switch", "turn_on", _on)
    entry = await _setup(hass, aioclient_mock, entry=_entry(hass),
                         a={**STATE_A, "health": DOWN})
    assert off
    await _later(hass, freezer, NETWORK_OFF_SECONDS)
    for _ in range(NETWORK_ON_RETRIES):
        await _later(hass, freezer, NETWORK_ON_RETRY_SECONDS)
    assert len(attempts) == 3 and "couldn't turn switch.water_heater_network back on" in (
        caplog.text)
    reset = hass.data["icomm_local"][entry.entry_id].network_reset
    assert reset.status()["in_progress"] is False and reset.status()["last"]


async def test_retries_give_up(hass, aioclient_mock, freezer):
    async_mock_service(hass, "switch", "turn_off")
    attempts = []

    async def _on(call):
        attempts.append(call)
        raise HomeAssistantError("gone")
    hass.services.async_register("switch", "turn_on", _on)
    await _setup(hass, aioclient_mock, entry=_entry(hass), a={**STATE_A, "health": DOWN})
    await _later(hass, freezer, NETWORK_OFF_SECONDS)
    for _ in range(NETWORK_ON_RETRIES + 3):
        await _later(hass, freezer, NETWORK_ON_RETRY_SECONDS)
    assert len(attempts) == NETWORK_ON_RETRIES


async def test_options_flow_offers_the_switch(hass, aioclient_mock, calls):
    entry = await _setup(hass, aioclient_mock, entry=_entry(hass, switch=None),
                         a={**STATE_A, "health": HEALTHY})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    assert {str(k) for k in result["data_schema"].schema} == {"power_entity", "network_switch"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"network_switch": SWITCH})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options == {"network_switch": SWITCH}
    coordinator = hass.data["icomm_local"][entry.entry_id]
    assert coordinator.network_reset is not None and coordinator.network_reset.switch == SWITCH
    assert entry.state == config_entries.ConfigEntryState.LOADED
