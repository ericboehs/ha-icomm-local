"""The heater's firmware: diagnostic sensors and the HA device page."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from test_integration import STATE, URL, _setup

DEVICE = {"controller_firmware": "2.14", "module_firmware": "bc 2.11.4 05/13/25",
          "module_hardware": "LBEE5PA1LD-222", "model": "iCOMMNextGenHP",
          "dsn": "AC000W019949045", "mac": "d01769f8f31c", "updated": "2026-10-25T16:14:07"}


def _device(hass, entry):
    return dr.async_get(hass).async_get_device_by_identifier(
        ("icomm_local", "water_heater"), entry.entry_id)


async def test_sensors_and_device_page(hass: HomeAssistant, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, {**STATE, "device": DEVICE})
    controller = hass.states.get("sensor.icomm_water_heater_controller_firmware")
    module = hass.states.get("sensor.icomm_water_heater_wi_fi_module_firmware")
    assert controller.state == "2.14" and module.state == "bc 2.11.4 05/13/25"
    assert module.attributes["dsn"] == "AC000W019949045"
    assert module.attributes["module_hardware"] == "LBEE5PA1LD-222"
    reg = er.async_get(hass).async_get("sensor.icomm_water_heater_controller_firmware")
    assert reg.entity_category == "diagnostic"
    device = _device(hass, entry)
    assert (device.sw_version, device.model) == ("2.14", "iCOMMNextGenHP")

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/state", json={**STATE, "device": {**DEVICE,
                                             "controller_firmware": "2.15"}})
    await hass.data["icomm_local"][entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    device = _device(hass, entry)
    assert device.sw_version == "2.15"


async def test_an_older_relay_leaves_them_unknown(hass: HomeAssistant, aioclient_mock):
    entry = await _setup(hass, aioclient_mock)
    assert hass.states.get("sensor.icomm_water_heater_controller_firmware").state == "unknown"
    device = _device(hass, entry)
    assert device.sw_version is None


async def test_an_older_home_assistant_without_the_new_lookup(hass, aioclient_mock, monkeypatch):
    """hacs.json promises 2024.1+: fall back to async_get_device there."""
    registry = dr.async_get(hass)
    found = []

    def old_lookup(identifiers):
        found.append(identifiers)
        return None

    monkeypatch.delattr(dr.DeviceRegistry, "async_get_device_by_identifier")
    monkeypatch.setattr(registry, "async_get_device", old_lookup)
    await _setup(hass, aioclient_mock, {**STATE, "device": DEVICE})
    assert found and found[0] == {("icomm_local", "water_heater")}
