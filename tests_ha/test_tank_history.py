"""The 0.4.13 entity-ID migration carries recorder history, with a real recorder.

The other migration tests run without a recorder. On the live install the
history API briefly showed the old mapping after the restart (a cache, before
the recorder caught up), while the database itself was right. This pins the
database side: old "bottom" history ends up under ..._average and old "middle"
history under ..._bottom.
"""
from __future__ import annotations

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.const import CONF_URL
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from test_integration import STATE, URL

P = "sensor.icomm_water_heater_water_tank_"


@pytest.fixture
def mock_recorder_before_hass(async_test_recorder) -> None:
    """Start the recorder before hass, as the plugin requires."""


async def test_history_follows_the_renamed_sensors(recorder_mock, hass, aioclient_mock):
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = MockConfigEntry(domain="icomm_local", data={CONF_URL: URL}, title="iCOMM relay")
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    reg.async_get_or_create("sensor", "icomm_local", "icomm_local_water_bottom", config_entry=entry,
                            suggested_object_id="icomm_water_heater_water_tank_bottom")
    reg.async_get_or_create("sensor", "icomm_local", "icomm_local_water_mid", config_entry=entry,
                            suggested_object_id="icomm_water_heater_water_tank_middle")
    start = dt_util.utcnow()
    hass.states.async_set(P + "bottom", "117")      # the old "bottom" (the blend)
    hass.states.async_set(P + "middle", "77")       # the old "middle" (really bottom)
    await async_wait_recording_done(hass)
    hass.states.async_remove(P + "bottom")          # a restart: no live states yet
    hass.states.async_remove(P + "middle")
    await async_wait_recording_done(hass)
    aioclient_mock.get(f"{URL}/state", json={**STATE, "water_mid": 70.0, "water_bottom": 110.0})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    hist = await get_instance(hass).async_add_executor_job(
        lambda: get_significant_states(
            hass, start, None, [P + name for name in ("average", "bottom", "middle", "lower")]))
    assert [s.state for s in hist.get(P + "average", [])][0] == "117"  # old "bottom"
    assert P + "lower" not in hist
    assert [s.state for s in hist.get(P + "bottom", [])][0] == "77"    # old "middle"
    assert P + "middle" not in hist
