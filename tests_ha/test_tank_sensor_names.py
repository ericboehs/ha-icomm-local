"""0.4.13: tank sensors relabelled by measured order, entity IDs migrated.

The sensor first called "middle" (relay key water_mid) is the bottom-most: it
falls first and furthest during a draw. The one called "bottom" (water_bottom)
sits above it, so it is now "lower".
"""

from __future__ import annotations

from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_integration import STATE, URL

P = "sensor.icomm_water_heater_water_tank_"


async def _setup_with_registered(hass, aioclient_mock, preexisting: dict[str, str]):
    """Set up after registering entities as an older version would have."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    aioclient_mock.get(f"{URL}/state", json={**STATE, "water_mid": 77.0})
    entry = MockConfigEntry(domain="icomm_local", data={CONF_URL: URL}, title="iCOMM relay")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for unique_id, entity_id in preexisting.items():
        registry.async_get_or_create("sensor", "icomm_local", unique_id, config_entry=entry,
                                     suggested_object_id=entity_id.split(".", 1)[1])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return registry


async def test_old_default_ids_move_to_the_new_names(hass: HomeAssistant, aioclient_mock) -> None:
    registry = await _setup_with_registered(hass, aioclient_mock, {
        "icomm_local_water_bottom": P + "bottom", "icomm_local_water_mid": P + "middle"})
    assert registry.async_get_entity_id("sensor", "icomm_local", "icomm_local_water_bottom") \
        == P + "lower"
    assert registry.async_get_entity_id("sensor", "icomm_local", "icomm_local_water_mid") \
        == P + "bottom"
    assert hass.states.get(P + "middle") is None
    bottom = hass.states.get(P + "bottom")
    assert bottom.state == "77.0"                       # the bottom-most sensor's value
    assert bottom.attributes["friendly_name"] == "iCOMM Water Heater Water, tank bottom"
    assert hass.states.get(P + "lower").attributes["friendly_name"].endswith("tank lower")


async def test_customised_ids_are_left_alone(hass: HomeAssistant, aioclient_mock) -> None:
    registry = await _setup_with_registered(hass, aioclient_mock, {
        "icomm_local_water_bottom": "sensor.my_tank_low", "icomm_local_water_mid": P + "middle"})
    assert registry.async_get_entity_id("sensor", "icomm_local", "icomm_local_water_bottom") \
        == "sensor.my_tank_low"
    # Its target is still taken by nothing else, so the mid sensor still moves.
    assert registry.async_get_entity_id("sensor", "icomm_local", "icomm_local_water_mid") \
        == P + "bottom"


async def test_an_occupied_target_blocks_the_move(hass: HomeAssistant, aioclient_mock) -> None:
    """water_bottom customised away but water_mid's target taken: no clobbering."""
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", "other", "x", suggested_object_id=
                                 "icomm_water_heater_water_tank_bottom")
    await _setup_with_registered(hass, aioclient_mock, {
        "icomm_local_water_bottom": "sensor.my_tank_low", "icomm_local_water_mid": P + "middle"})
    assert registry.async_get_entity_id("sensor", "icomm_local", "icomm_local_water_mid") \
        == P + "middle"


async def test_a_fresh_install_gets_the_new_names(hass: HomeAssistant, aioclient_mock) -> None:
    registry = await _setup_with_registered(hass, aioclient_mock, {})
    assert registry.async_get_entity_id("sensor", "icomm_local", "icomm_local_water_bottom") \
        == P + "lower"
    assert registry.async_get_entity_id("sensor", "icomm_local", "icomm_local_water_mid") \
        == P + "bottom"
    heater = hass.states.get("water_heater.icomm_water_heater")
    assert heater.attributes["water_lower"] == STATE["water_bottom"]
    assert heater.attributes["water_bottom"] == 77.0
