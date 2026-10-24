"""The relay reporting Ayla's certificate failing becomes an error repair."""

from __future__ import annotations

import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from test_integration import STATE, URL, _setup

FAILING = {"failures": 4, "last_failure": time.time(), "last_ok": time.time() - 600,
           "last_error": "unable to get local issuer certificate", "failing": True}


def _issue(hass, entry):
    issue_id = f"upstream_certificate_{entry.entry_id}"
    return ir.async_get(hass).async_get_issue("icomm_local", issue_id)


async def test_a_failing_certificate_raises_an_error_repair(hass: HomeAssistant, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, {**STATE, "upstream_tls": FAILING})
    issue = _issue(hass, entry)
    assert issue.severity is ir.IssueSeverity.ERROR and not issue.is_fixable
    assert issue.translation_placeholders == {
        "error": "unable to get local issuer certificate", "failures": "4",
        "since": "10 minutes ago"}

    aioclient_mock.clear_requests()
    recovered = {**FAILING, "failing": False}
    aioclient_mock.get(f"{URL}/state", json={**STATE, "upstream_tls": recovered})
    await hass.data["icomm_local"][entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None


async def test_never_verified_since_start(hass: HomeAssistant, aioclient_mock):
    tls = {"failing": True, "last_ok": None, "failures": 1, "last_error": None}
    entry = await _setup(hass, aioclient_mock, {**STATE, "upstream_tls": tls})
    placeholders = _issue(hass, entry).translation_placeholders
    assert placeholders["since"] == "not since the relay started"
    assert placeholders["error"] == "unknown"


async def test_an_older_relay_raises_nothing_and_unload_cleans_up(hass, aioclient_mock):
    entry = await _setup(hass, aioclient_mock)                 # no upstream_tls at all
    assert _issue(hass, entry) is None
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/state", json={**STATE, "upstream_tls": FAILING})
    await hass.data["icomm_local"][entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    assert _issue(hass, entry) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, entry) is None
