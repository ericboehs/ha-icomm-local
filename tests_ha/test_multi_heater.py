"""Two water heaters behind one relay: one HA device each, and the migration (#18).

A is the primary (/state); B is the second (/heaters/B/state). An install from
before keeps its entity IDs (so its history and dashboards) while its unique IDs
and device identifier move to A's DSN.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_integration import STATE, URL

A, B = "AC000W019949045", "AC000W077777777"
STATE_A = {**STATE, "heater": {"dsn": A, "dir": f"/data/heaters/{A}"}, "heaters": [A, B],
           "device": {"dsn": A, "model_number": "HPS10-50H45DV", "serial_number": "134274434",
                      "controller_firmware": "2.14"}}
STATE_B = {**STATE, "heater": {"dsn": B, "dir": f"/data/heaters/{B}"}, "heaters": [A, B],
           "mode": "electric", "water_top": 120.0, "water_bottom": 119.0, "watts": 4500,
           "device": {"dsn": B, "model_number": "HPS10-66H45DV", "serial_number": "222",
                      "controller_firmware": "2.15"}}
WRITE_OK = {"queued": {"kind": "setpoint"}, "pending": 1, "lan_registered": True}


def _relay(aioclient_mock, a=STATE_A, b=STATE_B, b_status=200, more=None):
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/state", json=a)
    aioclient_mock.get(f"{URL}/heaters/{B}/state", json=b, status=b_status)
    for dsn, state in (more or {}).items():
        aioclient_mock.get(f"{URL}/heaters/{dsn}/state", json=state)


async def _setup(hass, aioclient_mock, entry=None, **relay) -> MockConfigEntry:
    hass.config.units = US_CUSTOMARY_SYSTEM
    _relay(aioclient_mock, **relay)
    if entry is None:
        entry = MockConfigEntry(domain="icomm_local", data={"url": URL}, title="iCOMM relay")
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _refresh(hass, entry, aioclient_mock, **relay) -> None:
    _relay(aioclient_mock, **relay)
    await hass.data["icomm_local"][entry.entry_id].async_refresh()
    await hass.async_block_till_done()


def _device(hass, entry, identifier):
    return dr.async_get(hass).async_get_device_by_identifier(
        ("icomm_local", identifier), entry.entry_id)


async def test_two_heaters_two_devices(hass: HomeAssistant, aioclient_mock):
    entry = await _setup(hass, aioclient_mock)
    a = hass.states.get("water_heater.icomm_water_heater")
    b = hass.states.get("water_heater.icomm_water_heater_2")
    assert (a.state, a.attributes["current_temperature"]) == ("heat_pump", 128.0)
    assert (b.state, b.attributes["current_temperature"]) == ("electric", 119.0)
    assert hass.states.get("sensor.icomm_water_heater_2_power").state == "4500"
    device_a, device_b = _device(hass, entry, A), _device(hass, entry, B)
    assert (device_a.name, device_a.serial_number, device_a.sw_version) == (
        "iCOMM Water Heater", "134274434", "2.14")
    assert (device_b.name, device_b.model, device_b.sw_version) == (
        "iCOMM Water Heater 2", "HPS10-66H45DV", "2.15")
    registry = er.async_get(hass)
    assert registry.async_get("water_heater.icomm_water_heater_2").unique_id == (
        f"icomm_local_{B}_water_heater")
    assert registry.async_get("sensor.icomm_water_heater_power").unique_id == (
        f"icomm_local_{A}_watts")


async def test_an_upgrade_keeps_entity_ids_and_moves_the_keys(hass, aioclient_mock):
    """The pre-#18 registry: old unique IDs, one device, a renamed entity."""
    entry = MockConfigEntry(domain="icomm_local", data={"url": URL}, title="iCOMM relay")
    entry.add_to_hass(hass)
    registry, devices = er.async_get(hass), dr.async_get(hass)
    old_device = devices.async_get_or_create(config_entry_id=entry.entry_id,
                                             identifiers={("icomm_local", "water_heater")},
                                             name="iCOMM Water Heater")
    for domain, unique, entity_id in (
            ("water_heater", "icomm_local_water_heater", "water_heater.icomm_water_heater"),
            ("sensor", "icomm_local_watts", "sensor.icomm_water_heater_power"),
            ("sensor", "icomm_local_water_top", "sensor.my_tank_top"),          # customised
            ("sensor", "icomm_local_water_bottom",                             # 0.5.2 rename
             "sensor.icomm_water_heater_water_tank_lower")):
        registry.async_get_or_create(domain, "icomm_local", unique, config_entry=entry,
                                     device_id=old_device.id,
                                     suggested_object_id=entity_id.split(".")[1])
    await _setup(hass, aioclient_mock, entry, a={**STATE_A, "heaters": [A]})
    for entity_id, key in (("water_heater.icomm_water_heater", "water_heater"),
                           ("sensor.icomm_water_heater_power", "watts"),
                           ("sensor.my_tank_top", "water_top"),
                           ("sensor.icomm_water_heater_water_tank_average", "water_bottom")):
        entity = registry.async_get(entity_id)
        assert entity is not None and entity.unique_id == f"icomm_local_{A}_{key}", entity_id
        assert hass.states.get(entity_id) is not None
    assert hass.states.get("sensor.my_tank_top").state == "129.0"
    assert not [e for e in hass.states.async_entity_ids() if e.endswith("_2")]   # no duplicates
    device = _device(hass, entry, A)
    assert device.id == old_device.id                    # same device: automations still match
    assert _device(hass, entry, "water_heater") is None


async def test_each_heaters_card_writes_to_its_own_heater(hass, aioclient_mock):
    await _setup(hass, aioclient_mock)
    aioclient_mock.post(f"{URL}/heaters/{B}/write", json=WRITE_OK)
    aioclient_mock.post(f"{URL}/heaters/{A}/write", json=WRITE_OK)
    for entity_id in ("water_heater.icomm_water_heater_2", "water_heater.icomm_water_heater"):
        await hass.services.async_call("water_heater", "set_temperature",
                                       {"entity_id": entity_id, "temperature": 139},
                                       blocking=True)
    posts = [str(url) for method, url, *_ in aioclient_mock.mock_calls if method == "POST"]
    assert posts == [f"{URL}/heaters/{B}/write", f"{URL}/heaters/{A}/write"]


async def test_services_say_which_heater(hass: HomeAssistant, aioclient_mock):
    entry = await _setup(hass, aioclient_mock)
    aioclient_mock.post(f"{URL}/heaters/{B}/write", json=WRITE_OK)
    aioclient_mock.post(f"{URL}/heaters/{A}/write", json=WRITE_OK)
    aioclient_mock.post(f"{URL}/heaters/{B}/write/cancel", json={"cancelled": {"mode": {}}})
    with pytest.raises(HomeAssistantError, match="several water heaters"):
        await hass.services.async_call("icomm_local", "set_setpoint", {"temperature": 140},
                                       blocking=True, return_response=True)
    one = await hass.services.async_call(
        "icomm_local", "set_setpoint",
        {"entity_id": "water_heater.icomm_water_heater_2", "temperature": 140},
        blocking=True, return_response=True)
    assert one == {"queued": {"kind": "setpoint"}}
    both = await hass.services.async_call(
        "icomm_local", "set_mode",
        {"entity_id": ["water_heater.icomm_water_heater", "water_heater.icomm_water_heater_2"],
         "mode": "vacation", "days": 3}, blocking=True, return_response=True)
    assert set(both["heaters"]) == {A, B}
    by_device = await hass.services.async_call(
        "icomm_local", "cancel_writes", {"device_id": _device(hass, entry, B).id},
        blocking=True, return_response=True)
    assert by_device == {"cancelled": {"mode": {}}}
    hass.states.async_set("water_heater.someone_else", "electric")
    with pytest.raises(HomeAssistantError, match="no iCOMM water heater"):
        await hass.services.async_call(
            "icomm_local", "set_setpoint",
            {"entity_id": "water_heater.someone_else", "temperature": 140}, blocking=True)


async def test_a_heater_that_appears_later_gets_its_entities(hass, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, a={**STATE_A, "heaters": [A]})
    assert hass.states.get("water_heater.icomm_water_heater_2") is None
    await _refresh(hass, entry, aioclient_mock)
    assert hass.states.get("water_heater.icomm_water_heater_2").state == "electric"
    assert hass.states.get("water_heater.icomm_water_heater").state == "heat_pump"


async def test_an_older_relay_then_its_dsn_moves_the_keys(hass, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, a=STATE)                 # no DSN at all
    registry = er.async_get(hass)
    assert registry.async_get("sensor.icomm_water_heater_power").unique_id == "icomm_local_watts"
    await _refresh(hass, entry, aioclient_mock, a={**STATE_A, "heaters": [A]})
    await hass.async_block_till_done()                                 # the reload
    assert registry.async_get("sensor.icomm_water_heater_power").unique_id == (
        f"icomm_local_{A}_watts")
    assert hass.states.get("sensor.icomm_water_heater_power").state == "356"


async def test_one_heater_failing_leaves_the_other(hass, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, b_status=500)
    assert hass.states.get("water_heater.icomm_water_heater").state == "heat_pump"
    assert hass.states.get("water_heater.icomm_water_heater_2").state == "unavailable"
    await _refresh(hass, entry, aioclient_mock, b_status=200)
    assert hass.states.get("water_heater.icomm_water_heater_2").state == "electric"


async def test_repairs_are_per_heater(hass: HomeAssistant, aioclient_mock):
    entry = await _setup(hass, aioclient_mock)
    stale_b = {**STATE_B, "fresh": False, "last_telemetry_age": 1800,
               "contents_unchanged_s": 6000}
    frozen_b = {**STATE_B, "contents_unchanged_s": 6000, "contents_stale": True}
    issues = ir.async_get(hass)
    await _refresh(hass, entry, aioclient_mock, b=stale_b)
    issue = issues.async_get_issue("icomm_local", f"stale_telemetry_{entry.entry_id}_{B}")
    assert issue.translation_placeholders["heater"] == "iCOMM Water Heater 2"
    assert issues.async_get_issue("icomm_local", f"stale_telemetry_{entry.entry_id}") is None
    await _refresh(hass, entry, aioclient_mock, b=frozen_b)
    assert issues.async_get_issue("icomm_local", f"stale_telemetry_{entry.entry_id}_{B}") is None
    assert issues.async_get_issue("icomm_local", f"telemetry_frozen_{entry.entry_id}_{B}")
    # B leaves the relay: its issues go with it.
    await _refresh(hass, entry, aioclient_mock, a={**STATE_A, "heaters": [A]})
    assert issues.async_get_issue("icomm_local", f"telemetry_frozen_{entry.entry_id}_{B}") is None
    await _refresh(hass, entry, aioclient_mock, b=frozen_b)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert issues.async_get_issue("icomm_local", f"telemetry_frozen_{entry.entry_id}_{B}") is None


async def test_a_half_done_migration_and_a_network_error(hass, aioclient_mock):
    """Both keys present (an interrupted earlier run): the old entity is left
    alone rather than colliding; a foreign unique ID is never touched; a heater
    whose state can't be fetched is unavailable, not an error."""
    entry = MockConfigEntry(domain="icomm_local", data={"url": URL}, title="iCOMM relay")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", "icomm_local", "icomm_local_watts", config_entry=entry,
                                 suggested_object_id="old_power")
    registry.async_get_or_create("sensor", "icomm_local", f"icomm_local_{A}_watts",
                                 config_entry=entry, suggested_object_id="icomm_water_heater_power")
    registry.async_get_or_create("sensor", "icomm_local", "custom_thing", config_entry=entry,
                                 suggested_object_id="custom_thing")
    from aiohttp import ClientError
    hass.config.units = US_CUSTOMARY_SYSTEM
    aioclient_mock.get(f"{URL}/state", json=STATE_A)
    aioclient_mock.get(f"{URL}/heaters/{B}/state", exc=ClientError("refused"))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get("sensor.old_power").unique_id == "icomm_local_watts"
    assert registry.async_get("sensor.custom_thing").unique_id == "custom_thing"
    assert hass.states.get("sensor.icomm_water_heater_power").state == "356"
    assert hass.states.get("water_heater.icomm_water_heater_2").state == "unavailable"


# -- review findings (#18 phase 3) ----------------------------------------------

async def test_the_relay_forgetting_the_dsn_creates_no_duplicates(hass, aioclient_mock):
    """Migrated, then the relay's data is reset: it names no DSN for a while."""
    entry = await _setup(hass, aioclient_mock, a={**STATE_A, "heaters": [A]})
    before = set(hass.states.async_entity_ids())
    await hass.config_entries.async_reload(entry.entry_id)               # a restart, say
    _relay(aioclient_mock, a={**STATE, "heaters": []})                   # 0.7 relay, no DSN
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert set(hass.states.async_entity_ids()) == before
    assert not [e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
                if e.unique_id.startswith("icomm_local_") and A not in e.unique_id]
    aioclient_mock.post(f"{URL}/write", json=WRITE_OK)                   # not /heaters/A/write
    await hass.services.async_call("water_heater", "set_temperature",
                                   {"entity_id": "water_heater.icomm_water_heater",
                                    "temperature": 139}, blocking=True)
    assert entry.data["dsn"] == A


async def test_the_dsn_arriving_reloads_once(hass, aioclient_mock, monkeypatch):
    from homeassistant import config_entries
    monkeypatch.delattr(config_entries.ConfigEntries, "async_schedule_reload")   # HA 2024.1
    reloads = []
    real_reload = config_entries.ConfigEntries.async_reload

    async def counting_reload(self, entry_id):
        reloads.append(entry_id)
        return await real_reload(self, entry_id)

    monkeypatch.setattr(config_entries.ConfigEntries, "async_reload", counting_reload)
    entry = await _setup(hass, aioclient_mock, a=STATE)
    coordinator = hass.data["icomm_local"][entry.entry_id]
    _relay(aioclient_mock, a={**STATE_A, "heaters": [A]})
    await coordinator.async_refresh()
    await coordinator.async_refresh()                                    # before it lands
    await hass.async_block_till_done()
    assert len(reloads) == 1
    assert er.async_get(hass).async_get("sensor.icomm_water_heater_power").unique_id == (
        f"icomm_local_{A}_watts")


async def test_heater_numbers_stay_put(hass, aioclient_mock):
    c, d = "AC000W066666666", "AC000W055555555"
    state_c = {**STATE_B, "heater": {"dsn": c}, "device": {"dsn": c}}
    state_d = {**STATE_B, "heater": {"dsn": d}, "device": {"dsn": d}}
    entry = await _setup(hass, aioclient_mock, a={**STATE_A, "heaters": [A, B, c]},
                         more={c: state_c})
    assert entry.data["heater_numbers"] == {A: 1, B: 2, c: 3}
    _relay(aioclient_mock, a={**STATE_A, "heaters": [A, c]}, more={c: state_c})   # B leaves
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _device(hass, entry, c).name == "iCOMM Water Heater 3"                  # not "2"
    _relay(aioclient_mock, a={**STATE_A, "heaters": [A, c, d]}, more={c: state_c, d: state_d})
    await hass.data["icomm_local"][entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    assert entry.data["heater_numbers"][d] == 4          # B keeps 2 in case it comes back
    assert _device(hass, entry, d).name == "iCOMM Water Heater 4"


async def test_odd_dsns_from_the_relay_are_ignored(hass, aioclient_mock):
    await _setup(hass, aioclient_mock,
                 a={**STATE_A, "heaters": [A, "ac000w0777777", "../x", 42, B]})
    heaters = [e for e in hass.states.async_entity_ids("water_heater")]
    assert sorted(heaters) == ["water_heater.icomm_water_heater",
                               "water_heater.icomm_water_heater_2"]
    calls = [str(url) for method, url, *_ in aioclient_mock.mock_calls if method == "GET"]
    assert not [c for c in calls if "x/state" in c or "ac000w" in c]


async def test_a_replaced_heater_takes_over_the_old_ones_entities(hass, aioclient_mock):
    """Review pass 2: X1 is replaced by X2 (X1 gone from the relay). X2 keeps the
    entity IDs, device and number; no second device beside a dead one."""
    x2 = "AC000W044444444"
    entry = await _setup(hass, aioclient_mock, a={**STATE_A, "heaters": [A]})
    device = _device(hass, entry, A)
    replaced = {**STATE_A, "heater": {"dsn": x2}, "heaters": [x2],
                "device": {"dsn": x2, "serial_number": "999"},
                "water_top": 101.0, "water_bottom": 100.0}
    _relay(aioclient_mock, a=replaced)                      # at runtime: one reload
    await hass.data["icomm_local"][entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get("sensor.icomm_water_heater_power").unique_id == (
        f"icomm_local_{x2}_watts")
    assert hass.states.get("water_heater.icomm_water_heater").attributes[
        "current_temperature"] == 100.0
    assert _device(hass, entry, x2).id == device.id and _device(hass, entry, A) is None
    assert not [e for e in hass.states.async_entity_ids() if e.endswith("_2")]
    assert entry.data["dsn"] == x2 and entry.data["heater_numbers"] == {x2: 1}


async def test_the_primary_unnamed_but_others_listed(hass, aioclient_mock):
    """Review pass 2: right after a relay restart it may list B before naming
    the primary; the primary's entities stay (single-heater keys) and B is not
    added under them."""
    entry = await _setup(hass, aioclient_mock, a={**STATE, "heaters": [B]})
    assert hass.states.get("water_heater.icomm_water_heater").state == "heat_pump"
    assert hass.data["icomm_local"][entry.entry_id].order == [None, B]
    assert entry.data["heater_numbers"] == {B: 2}


async def test_rekey_leaves_others_and_never_collides(hass):
    from custom_components.icomm_local.heaters import rekey
    entry = MockConfigEntry(domain="icomm_local", data={"url": URL})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for unique in (f"icomm_local_{A}_watts", f"icomm_local_{A}_mode",
                   f"icomm_local_{B}_watts", f"icomm_local_{B}_mode_x"):
        registry.async_get_or_create("sensor", "icomm_local", unique, config_entry=entry)
    registry.async_get_or_create("sensor", "icomm_local", f"icomm_local_{B}_mode",
                                 config_entry=entry, suggested_object_id="already_there")
    assert rekey(hass, entry, A, B) == 0                    # watts and mode both taken
    assert registry.async_get_entity_id("sensor", "icomm_local", f"icomm_local_{A}_watts")


async def test_a_promoted_second_heater_is_not_a_replacement(hass, aioclient_mock):
    """Final review: A removed from a two-heater relay, B becomes primary. B
    keeps its own number, device and entities; A's are not moved onto B."""
    entry = await _setup(hass, aioclient_mock)
    device_b = _device(hass, entry, B)
    _relay(aioclient_mock, a={**STATE_B, "heaters": [B]})
    await hass.data["icomm_local"][entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get("sensor.icomm_water_heater_power").unique_id == (
        f"icomm_local_{A}_watts")                                        # A's, untouched
    assert registry.async_get("sensor.icomm_water_heater_2_power").unique_id == (
        f"icomm_local_{B}_watts")
    assert _device(hass, entry, B).id == device_b.id
    assert entry.data["heater_numbers"] == {A: 1, B: 2} and entry.data["dsn"] == B
    assert hass.states.get("water_heater.icomm_water_heater_2").state == "electric"
