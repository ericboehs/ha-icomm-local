"""Tank sensors relabelled as their meaning was measured; entity IDs migrated.

0.4.13: the sensor first called "middle" (relay key water_mid) is the bottom
thermistor, so the one called "bottom" (water_bottom) became "lower". 0.5.2:
"lower" is the controller's blend of top and bottom, so it is the "average".
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
        == P + "average"                                # 0.4.12 -> straight through
    assert registry.async_get_entity_id("sensor", "icomm_local", "icomm_local_water_mid") \
        == P + "bottom"
    assert hass.states.get(P + "middle") is None and hass.states.get(P + "lower") is None
    bottom = hass.states.get(P + "bottom")
    assert bottom.state == "77.0"                       # the bottom-most sensor's value
    assert bottom.attributes["friendly_name"] == "iCOMM Water Heater Water, tank bottom"
    assert hass.states.get(P + "average").attributes["friendly_name"].endswith("tank average")


async def test_a_0_4_13_install_moves_lower_to_average(hass: HomeAssistant, aioclient_mock):
    registry = await _setup_with_registered(hass, aioclient_mock, {
        "icomm_local_water_bottom": P + "lower", "icomm_local_water_mid": P + "bottom"})
    assert registry.async_get_entity_id("sensor", "icomm_local", "icomm_local_water_bottom") \
        == P + "average"
    assert registry.async_get_entity_id("sensor", "icomm_local", "icomm_local_water_mid") \
        == P + "bottom"


async def test_the_average_explains_its_weighting(hass: HomeAssistant, aioclient_mock):
    await _setup_with_registered(hass, aioclient_mock, {})
    attrs = hass.states.get(P + "average").attributes
    assert attrs["computed"] is True and "heat pump" in attrs["weighting"]
    top, blend = STATE["water_top"], STATE["water_bottom"]
    assert attrs["weight_on_top"] == round((blend - 77.0) / (top - 77.0), 2)
    assert "stale" in attrs                              # the base attributes stay


async def test_no_weight_when_top_and_bottom_agree(hass: HomeAssistant, aioclient_mock):
    hass.config.units = US_CUSTOMARY_SYSTEM
    from test_integration import _setup
    await _setup(hass, aioclient_mock, {**STATE, "water_top": 140.0, "water_mid": 138.0,
                                        "water_bottom": 139.0})
    assert hass.states.get(P + "average").attributes["weight_on_top"] is None


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
        == P + "average"
    assert registry.async_get_entity_id("sensor", "icomm_local", "icomm_local_water_mid") \
        == P + "bottom"
    heater = hass.states.get("water_heater.icomm_water_heater")
    assert heater.attributes["water_average"] == STATE["water_bottom"]
    assert heater.attributes["water_bottom"] == 77.0
