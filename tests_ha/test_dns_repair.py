"""dns_server can't bind its ports: an error repair says which and why."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from test_integration import STATE, URL, _setup


def _issue(hass, entry):
    issue_id = f"dns_server_failed_{entry.entry_id}"
    return ir.async_get(hass).async_get_issue("icomm_local", issue_id)


async def test_port_53_taken(hass: HomeAssistant, aioclient_mock):
    dns = {"enabled": True, "error": "could not listen on port 53: Address in use"}
    entry = await _setup(hass, aioclient_mock, {**STATE, "dns": dns})
    issue = _issue(hass, entry)
    assert issue.severity is ir.IssueSeverity.ERROR
    assert "port 53" in issue.translation_placeholders["problem"]


async def test_port_443_taken_then_fixed(hass: HomeAssistant, aioclient_mock):
    dns = {"enabled": True, "error": None, "https_error": "port 443 is in use: x"}
    entry = await _setup(hass, aioclient_mock, {**STATE, "dns": dns})
    assert "443" in _issue(hass, entry).translation_placeholders["problem"]
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/state", json={**STATE, "dns": {"enabled": True}})
    await hass.data["icomm_local"][entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_off_or_older_relay_raises_nothing(hass: HomeAssistant, aioclient_mock):
    entry = await _setup(hass, aioclient_mock,
                         {**STATE, "dns": {"enabled": False, "error": "stale"}})
    assert _issue(hass, entry) is None
