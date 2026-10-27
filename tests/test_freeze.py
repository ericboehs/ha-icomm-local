"""Frozen telemetry, phase 50: sooner while heating, activity unknown while
frozen, and the experimental LAN reconnect after 2 failed renewals."""

from __future__ import annotations

import json

import pytest
from icomm_relay import FROZEN_UNKNOWN, telemetry_blocks
from test_lan import _server
from test_proxy import _relay, _telemetry_body

HEATING = telemetry_blocks(_telemetry_body())                              # electric, 4500 W
IDLE = telemetry_blocks(_telemetry_body("telemetry_heat_pump_cooldown.json"))  # 0 W


async def _state(relay) -> dict:
    return json.loads((await relay.state_handler(None)).body)


def _frozen_for(relay, blocks, seconds: float) -> None:
    relay._ingest_blocks(blocks)
    relay.contents_changed -= seconds
    relay._ingest_blocks(blocks)


def test_the_fixtures_are_heating_and_idle(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay._ingest_blocks(HEATING)
    assert relay.state["watts"] > 0
    relay._ingest_blocks(IDLE)
    assert relay.state["watts"] == 0


@pytest.mark.parametrize(("blocks", "frozen_s", "stale"), [
    (HEATING, 1100, False),     # 18 min heating: still within a healthy gap
    (HEATING, 1300, True),      # 22 min heating: frozen
    (IDLE, 3600, False),        # an hour idle is normal (52 min seen)
    (IDLE, 5500, True),         # 90+ min idle
])
async def test_stale_sooner_while_heating(tmp_path, monkeypatch, blocks, frozen_s, stale):
    relay = _relay(tmp_path, monkeypatch)
    _frozen_for(relay, blocks, frozen_s)
    state = await _state(relay)
    assert state["contents_stale"] is stale
    assert state["contents_stale_after_s"] == (1200 if blocks is HEATING else 5400)


def _clock(monkeypatch) -> None:
    """Stop the clock, so `contents_unchanged_s` is exactly what a test sets."""
    now = 1_800_000_000.0
    monkeypatch.setattr("icomm_relay.time.time", lambda: now)


@pytest.mark.parametrize("env", [{}, {"CONTENTS_STALE_HEATING_AFTER": "600"},
                                 {"CONTENTS_STALE_AFTER": "3000"}])
@pytest.mark.parametrize("blocks", [HEATING, IDLE], ids=["heating", "idle"])
@pytest.mark.parametrize("offset", [-1, 0])
async def test_stale_from_exactly_the_threshold(tmp_path, monkeypatch, env, blocks, offset):
    """Stale at >= the threshold: 1200 s heating, 5400 s idle, or the env's."""
    _clock(monkeypatch)
    relay = _relay(tmp_path, monkeypatch, **env)
    idle_after = float(env.get("CONTENTS_STALE_AFTER", 5400))
    heating_after = min(float(env.get("CONTENTS_STALE_HEATING_AFTER", 1200)), idle_after)
    after = heating_after if blocks is HEATING else idle_after
    _frozen_for(relay, blocks, after + offset)
    state = await _state(relay)
    assert state["contents_stale_after_s"] == after
    assert state["contents_unchanged_s"] == after + offset
    assert state["contents_stale"] is (offset == 0)


async def test_a_lower_idle_threshold_still_wins(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch, CONTENTS_STALE_AFTER="60")
    _frozen_for(relay, HEATING, 120)
    assert (await _state(relay))["contents_stale"] is True


async def test_frozen_activity_is_unknown_and_temperatures_stay(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    _frozen_for(relay, HEATING, 1300)
    state = await _state(relay)
    for key in FROZEN_UNKNOWN:
        assert state[key] is None, key
    assert state["frozen_unknown"] == list(FROZEN_UNKNOWN)
    assert state["water_top"] is not None and state["setpoint"] is not None
    assert relay.state["watts"] == 4500          # only the reported copy is blanked

    changed = list(HEATING)
    changed[1] = bytes([changed[1][0] ^ 1]) + changed[1][1:]
    relay._ingest_blocks(changed)                # live again
    state = await _state(relay)
    assert state["watts"] == 4500 and "frozen_unknown" not in state


def _reconnecting(tmp_path, enabled: str = "1"):
    server = _server(tmp_path, FREEZE_RECONNECT=enabled)
    server.registered = True
    return server, server.relay


def _due(server, relay, blocks=HEATING) -> None:
    """Another renewal interval passes, frozen, and a report arrives."""
    relay.contents_changed -= relay.renew_after() + 1
    server.last_mask_sent -= relay.renew_after() + 1
    server.mask_pending = False
    relay._ingest_blocks(blocks)


def test_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("FREEZE_RECONNECT", raising=False)
    assert _server(tmp_path).relay.freeze_reconnect is False


@pytest.mark.parametrize("enabled", ["0", "1"])
def test_two_renewals_then_one_reconnect(tmp_path, enabled):
    server, relay = _reconnecting(tmp_path, enabled)
    relay._ingest_blocks(HEATING)
    _due(server, relay)
    assert server.mask_pending and relay.freeze_masks == 1
    _due(server, relay)
    assert server.mask_pending and relay.freeze_masks == 2
    _due(server, relay)                                    # the third attempt
    if enabled == "1":
        assert server.registered is False and server.mask_pending is False
        assert relay.freeze_reconnects == 1 and relay.last_freeze_reconnect
        server.registered = True                           # re-registered
        _due(server, relay)                                # still frozen: renewals go on
        assert server.mask_pending and relay.freeze_reconnects == 1
        _due(server, relay)
        assert relay.freeze_reconnects == 1                # once per freeze
    else:
        assert server.registered is True and server.mask_pending
        assert relay.freeze_reconnects == 0


def test_a_change_rearms_the_reconnect(tmp_path):
    server, relay = _reconnecting(tmp_path)
    relay._ingest_blocks(HEATING)
    for _ in range(3):
        _due(server, relay)
    assert relay.freeze_reconnects == 1
    server.registered = True
    relay._ingest_blocks(IDLE)                             # contents change
    assert relay.freeze_masks == 0 and relay.freeze_reconnected is False
    for _ in range(3):
        _due(server, relay, IDLE)
    assert relay.freeze_reconnects == 2


def test_no_reconnect_without_a_session(tmp_path):
    server, relay = _reconnecting(tmp_path)
    relay._ingest_blocks(HEATING)
    _due(server, relay)
    _due(server, relay)
    server.registered = False
    _due(server, relay)
    assert relay.freeze_reconnects == 0


def test_reconnect_wakes_the_registration_loop(tmp_path):
    import asyncio
    server, _ = _reconnecting(tmp_path)
    server._wake_event = asyncio.Event()
    assert server.reconnect() is True
    assert server._wake_event.is_set() and server.registered is False
    assert server.reconnect() is False


async def test_state_reports_the_option(tmp_path):
    server, relay = _reconnecting(tmp_path)
    state = await _state(relay)
    assert state["freeze_reconnect"] == {"enabled": True, "reconnects": 0, "last": None}
