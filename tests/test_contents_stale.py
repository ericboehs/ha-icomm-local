"""Reports that keep arriving with identical contents are flagged."""

from __future__ import annotations

import json

from test_proxy import _relay, _telemetry_body

from relay.icomm_relay import telemetry_blocks


async def _state(relay) -> dict:
    return json.loads((await relay.state_handler(None)).body)


async def test_identical_reports_age_and_a_change_resets(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch, CONTENTS_STALE_AFTER="60")
    blocks = telemetry_blocks(_telemetry_body())
    relay._ingest_blocks(blocks)
    relay.contents_changed -= 120                     # first seen two minutes ago
    relay._ingest_blocks(blocks)                      # same bytes, fresh report
    state = await _state(relay)
    assert state["fresh"] and state["contents_unchanged_s"] >= 120
    assert state["contents_stale"] is True

    changed = list(blocks)
    changed[1] = bytes([changed[1][0] ^ 1]) + changed[1][1:]
    relay._ingest_blocks(changed)
    state = await _state(relay)
    assert state["contents_unchanged_s"] < 5 and state["contents_stale"] is False


async def test_not_stale_when_reports_stop_or_never_came(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch, CONTENTS_STALE_AFTER="60")
    state = await _state(relay)
    assert state["contents_unchanged_s"] is None and state["contents_stale"] is False
    relay._ingest_blocks(telemetry_blocks(_telemetry_body()))
    relay.contents_changed -= 3600
    relay.last_telemetry -= 3600                      # no reports either: "not fresh"
    assert (await _state(relay))["contents_stale"] is False


async def test_default_threshold_is_ninety_minutes(tmp_path, monkeypatch):
    assert _relay(tmp_path, monkeypatch).contents_stale_after == 5400
