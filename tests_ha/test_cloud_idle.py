"""The relay says the heater's cloud side stopped: a repair, per heater."""

from __future__ import annotations

from homeassistant.helpers import issue_registry as ir
from test_multi_heater import STATE_A, STATE_B, B, _refresh, _setup

IDLE = {"state": "degraded", "code": "cloud_idle", "reasons": ["cloud side idle"],
        "remediation": "Flip the breaker", "evidence": {"cloud_quiet_s": 2460}}
HEALTHY = {"state": "healthy", "reasons": [], "evidence": {}}


def _issue(hass, issue_id):
    return ir.async_get(hass).async_get_issue("icomm_local", issue_id)


async def test_raised_and_cleared_for_each_heater(hass, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, a={**STATE_A, "health": IDLE},
                         b={**STATE_B, "health": HEALTHY})
    primary, second = f"cloud_idle_{entry.entry_id}", f"cloud_idle_{entry.entry_id}_{B}"
    issue = _issue(hass, primary)
    assert issue is not None and issue.translation_key == "cloud_idle"
    assert issue.translation_placeholders == {"heater": "iCOMM Water Heater", "minutes": "41"}
    assert _issue(hass, second) is None

    await _refresh(hass, entry, aioclient_mock, a={**STATE_A, "health": HEALTHY},
                   b={**STATE_B, "health": IDLE})
    assert _issue(hass, primary) is None
    assert _issue(hass, second).translation_placeholders["heater"] == "iCOMM Water Heater 2"

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, second) is None


async def test_another_degraded_reason_is_not_this_repair(hass, aioclient_mock):
    other = {**IDLE, "code": None, "reasons": ["silent 20 min while calling for heat"]}
    entry = await _setup(hass, aioclient_mock, a={**STATE_A, "health": other})
    assert _issue(hass, f"cloud_idle_{entry.entry_id}") is None


async def test_a_failed_poll_leaves_it_alone(hass, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, a={**STATE_A, "health": IDLE})
    aioclient_mock.clear_requests()
    aioclient_mock.get("http://relay.test:8124/state", status=500)
    await hass.data["icomm_local"][entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    assert _issue(hass, f"cloud_idle_{entry.entry_id}") is not None


async def test_the_health_sensor_shows_the_24h_cloud_history(hass, aioclient_mock):
    health = {**HEALTHY, "evidence": {"cloud_dials_24h": 42, "cloud_quiet_s": 75,
                                      "longest_cloud_gap_24h_s": 4870}}
    await _setup(hass, aioclient_mock, a={**STATE_A, "health": health})
    attrs = hass.states.get("sensor.icomm_water_heater_relay_health").attributes
    assert (attrs["cloud_dials_24h"], attrs["cloud_quiet_s"],
            attrs["longest_cloud_gap_24h_s"]) == (42, 75, 4870)
