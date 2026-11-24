"""The freeze nudge: a cloud-free copy of the only cure the A.O. Smith app was
seen to apply (phase 42): setBlockParameter = the current setpoint, then the
reporting mask. One ladder step per renewal interval while the data stays
frozen: the mask, then that pair, then (once per freeze) the pair with the
controller's own superBlockDynDataUpdateRate written back before the mask."""

from __future__ import annotations

import json
import logging

import pytest
from icomm_lan import APP_REPORTING_MASK
from test_freeze import HEATING, IDLE, _due
from test_lan import _server
from test_reporting_mask import POLL, _decrypt

SETPOINT_150 = "BD 40 08 0B 06 41 8E 10"       # what a user write of 150 F sends


def _nudging(tmp_path, **env):
    server = _server(tmp_path, **env)
    server.registered = True
    server.relay.reporting_interval_s = 21601          # learned from the config reply
    server.relay._ingest_blocks(HEATING)
    return server, server.relay


async def _poll(server) -> dict | None:
    data = _decrypt(server, await server._commands(POLL))["data"]
    return data["properties"][0]["property"] if data.get("properties") else None


async def _drain(server) -> list[tuple[str, object]]:
    """Every property the next polls carry, until a poll carries none."""
    out = []
    while (prop := await _poll(server)) is not None:
        out.append((prop["name"], prop["value"]))
    return out


def _step(server, relay, blocks=HEATING) -> None:
    """Another frozen interval: the previous step's mask went, a report arrives."""
    relay.contents_changed -= relay.renew_after() + 1
    server.last_mask_sent -= relay.renew_after() + 1
    relay._ingest_blocks(blocks)


async def test_the_ladder_mask_then_pair_then_rate_once_then_pair(tmp_path):
    server, relay = _nudging(tmp_path)
    MASK = ("periodicParameterSetting", APP_REPORTING_MASK)
    SET = ("setBlockParameter", SETPOINT_150)
    RATE = ("superBlockDynDataUpdateRate", 21601)
    seen = []
    for _ in range(5):
        _step(server, relay)
        seen.append(await _drain(server))
    assert seen == [[MASK], [SET, MASK], [SET, RATE, MASK], [SET, MASK], [SET, MASK]]
    assert relay.freeze_masks == 5
    assert relay.nudge_steps_sent == {"mask": 1, "pair": 3, "rate": 1}
    assert relay.nudge_last_step == "pair" and relay.nudge_last_step_at


async def test_the_pair_rides_consecutive_polls_setpoint_first(tmp_path):
    server, relay = _nudging(tmp_path)
    _step(server, relay)
    await _drain(server)
    _step(server, relay)
    first, second = await _poll(server), await _poll(server)
    assert (first["name"], first["value"], first["base_type"]) == (
        "setBlockParameter", SETPOINT_150, "string")
    assert second["name"] == "periodicParameterSetting" and second["id"] == server.mask_id
    assert first["id"] in server.nudge_ids and len(first["id"]) == 8
    assert server.nudges_sent == 1 and server.status()["nudges_sent"] == 1


async def test_the_nudge_setpoint_is_the_user_write_frame(tmp_path):
    server, relay = _nudging(tmp_path)
    assert relay._nudge_setpoint_frame() == SETPOINT_150
    assert relay.queue_write("setpoint", 150).frame == SETPOINT_150


async def test_a_user_write_goes_first_and_keeps_its_state(tmp_path):
    server, relay = _nudging(tmp_path)
    _step(server, relay)
    await _drain(server)
    _step(server, relay)                                   # pair queued
    relay.queue_write("mode", "heat_pump")
    first = await _poll(server)
    assert first["name"] == "setBlockParameter" and first["value"] == "BD 40 08 0B 0F 00 05 4A"
    assert [n for n, _ in await _drain(server)] == ["setBlockParameter",
                                                   "periodicParameterSetting"]
    assert relay.pending["mode"].attempts == 1 and relay.writes_delivered == 1
    assert server.writes_delivered == 1


async def test_a_setpoint_write_cancels_the_nudge_setpoint(tmp_path):
    server, relay = _nudging(tmp_path)
    _step(server, relay)
    await _drain(server)
    _step(server, relay)                                   # pair queued
    relay.queue_write("setpoint", 140)
    relay.pending["setpoint"].delivered = __import__("time").time()   # in its retry window
    relay.pending["setpoint"].attempts = 1
    assert await _drain(server) == [("periodicParameterSetting", APP_REPORTING_MASK)]
    _step(server, relay)                                   # still pending: mask only
    assert server.nudge_queue == []


async def test_the_nudge_leaves_the_user_write_state_alone(tmp_path):
    server, relay = _nudging(tmp_path)
    queue = relay.queue_file
    before = queue.read_text() if queue.exists() else None
    for _ in range(3):
        _step(server, relay)
        props = []
        while (prop := await _poll(server)) is not None:
            props.append(prop)
        for prop in props:
            await relay.ingest_lan_update({"data": {"ack_status": 200, "id": prop["id"]}})
    assert server.nudge_acks == 3                           # 2 setpoints and a rate
    assert relay.last_write is None and relay.pending == {}
    assert relay.writes_delivered == 0 and relay.writes_confirmed == 0
    assert server.writes_delivered == 0
    assert (queue.read_text() if queue.exists() else None) == before


async def test_a_nudge_ack_is_not_matched_to_a_pending_write(tmp_path):
    server, relay = _nudging(tmp_path)
    relay.queue_write("mode", "heat_pump")
    write = relay.pending["mode"]
    write.command_id = "userwrit"
    server.nudge_ids.append("nudge123")
    await relay.ingest_lan_update({"data": {"ack_status": 200, "id": "nudge123"}})
    assert write.acked is None and server.nudge_acks == 1


async def test_rate_skipped_when_unknown(tmp_path):
    server, relay = _nudging(tmp_path)
    relay.reporting_interval_s = None
    names = []
    for _ in range(4):
        _step(server, relay)
        names.append([n for n, _ in await _drain(server)])
    assert "superBlockDynDataUpdateRate" not in sum(names, [])
    assert relay.nudge_steps_sent["rate"] == 0 and relay.nudge_steps_sent["pair"] == 3


@pytest.mark.parametrize("setup", ["no_setpoint", "vacation", "recent_write", "cloud_write",
                                   "out_of_range"])
async def test_no_safe_setpoint_falls_back_to_the_mask(tmp_path, setup):
    server, relay = _nudging(tmp_path)
    _step(server, relay)
    await _drain(server)
    now = __import__("time").time()
    if setup == "no_setpoint":
        relay.state.pop("setpoint")
    elif setup == "vacation":
        relay.state["active_setpoint"] = 50.0
    elif setup == "recent_write":
        relay.last_write = {"kind": "setpoint", "value": 140.0, "delivered": now,
                            "outcome": "unconfirmed"}
    elif setup == "cloud_write":
        relay.last_cloud_write = {"kind": "setpoint", "value": 140.0, "at": now}
    else:
        relay.state["setpoint"] = relay.state["active_setpoint"] = 170.0
    for _ in range(2):
        # The ladder runs before the report is decoded; call it on the state
        # the setup left.
        relay.contents_changed -= relay.renew_after() + 1
        server.last_mask_sent -= relay.renew_after() + 1
        relay._maybe_renew_mask()
        assert await _drain(server) == [("periodicParameterSetting", APP_REPORTING_MASK)]
    assert relay.nudge_steps_sent == {"mask": 3, "pair": 0, "rate": 0}


async def test_items_dropped_when_the_contents_change(tmp_path):
    server, relay = _nudging(tmp_path)
    _step(server, relay)
    await _drain(server)
    _step(server, relay)                                   # pair queued
    assert server.nudge_queue
    relay._ingest_blocks(IDLE)                             # live again
    assert server.nudge_queue == [] and relay._nudge_waiting is None
    assert [n for n, _ in await _drain(server)] == ["periodicParameterSetting"]
    assert relay.nudge_steps_sent == {"mask": 1, "pair": 0, "rate": 0}
    _step(server, relay, IDLE)                             # a new freeze starts over
    assert await _drain(server) == [("periodicParameterSetting", APP_REPORTING_MASK)]


async def test_items_dropped_when_the_session_resets(tmp_path):
    server, relay = _nudging(tmp_path)
    server.request_nudge([("setBlockParameter", "string", SETPOINT_150)])
    assert server.reconnect() is True and server.nudge_queue == []
    server.registered = True
    server.request_nudge([("setBlockParameter", "string", SETPOINT_150)])
    reply = await server._key_exchange(_exchange_request())
    assert reply.status == 200 and server.nudge_queue == []


def _exchange_request():
    from unittest.mock import AsyncMock, MagicMock

    from test_lan import EXCHANGE
    request = MagicMock()
    request.json = AsyncMock(return_value={"key_exchange": {**EXCHANGE, "key_id": 1}})
    return request


def test_no_session_no_nudge(tmp_path):
    server, _ = _nudging(tmp_path)
    server.session = None
    assert server.request_nudge([("x", "string", "y")]) is False and server.nudge_queue == []


async def test_turned_off_it_is_the_mask_only(tmp_path, monkeypatch):
    monkeypatch.delenv("FREEZE_NUDGE", raising=False)
    assert _server(tmp_path / "default").relay.freeze_nudge is True
    server, relay = _nudging(tmp_path, FREEZE_NUDGE="0")
    for _ in range(4):
        _step(server, relay)
        assert await _drain(server) == [("periodicParameterSetting", APP_REPORTING_MASK)]
    assert relay.freeze_masks == 4 and server.nudges_sent == 0
    relay._ingest_blocks(IDLE)
    assert relay.nudge_steps_sent == {"mask": 0, "pair": 0, "rate": 0}
    assert relay.freezes_ended_after == {"mask": 0, "pair": 0, "rate": 0, "other": 0}


def test_turned_off_the_reconnect_ladder_is_unchanged(tmp_path):
    """test_freeze's 2 renewals then one reconnect, with the nudge both ways."""
    for enabled in ("0", "1"):
        server = _server(tmp_path / enabled, FREEZE_RECONNECT="1", FREEZE_NUDGE=enabled)
        server.registered = True
        relay = server.relay
        relay._ingest_blocks(HEATING)
        _due(server, relay)
        _due(server, relay)
        _due(server, relay)
        assert relay.freeze_reconnects == 1 and server.nudge_queue == []


async def test_state_reports_the_nudge(tmp_path):
    server, relay = _nudging(tmp_path)
    state = json.loads((await relay.state_handler(None)).body)
    assert state["freeze_nudge"] == {
        "enabled": True, "steps_sent": {"mask": 0, "pair": 0, "rate": 0},
        "last_step": None, "last_step_at": None,
        "freezes_ended_after": {"mask": 0, "pair": 0, "rate": 0, "other": 0}}
    _step(server, relay)
    await _drain(server)
    _step(server, relay)
    await _drain(server)
    relay._ingest_blocks(IDLE)
    nudge = json.loads((await relay.state_handler(None)).body)["freeze_nudge"]
    assert nudge["steps_sent"] == {"mask": 1, "pair": 1, "rate": 0}
    assert nudge["last_step"] == "pair" and nudge["last_step_at"] == relay.nudge_last_step_at
    assert nudge["freezes_ended_after"] == {"mask": 0, "pair": 1, "rate": 0, "other": 0}


@pytest.mark.parametrize(("steps", "ago", "reconnect", "cause"), [
    (1, 60, False, "mask"),
    (2, 60, False, "pair"),
    (3, 599, False, "rate"),
    (2, 601, False, "other"),          # more than 10 min after the step
    (2, 60, True, "other"),            # a reconnect came after it
    (0, 0, False, None),               # never frozen long enough for a step
])
async def test_ended_after_attribution(tmp_path, caplog, steps, ago, reconnect, cause):
    server, relay = _nudging(tmp_path)
    for _ in range(steps):
        _step(server, relay)
        await _drain(server)
    if steps:
        relay._freeze_delivered = (relay._freeze_delivered[0],
                                   relay._freeze_delivered[1] - ago)
    if reconnect:
        relay.last_freeze_reconnect = __import__("time").time() - 1
    caplog.set_level(logging.INFO, logger="icomm_relay")
    relay._ingest_blocks(IDLE)
    expected = dict.fromkeys(("mask", "pair", "rate", "other"), 0)
    if cause:
        expected[cause] = 1
        line = [r for r in caplog.records if "telemetry changed again" in r.getMessage()]
        assert len(line) == 1 and line[0].levelno == logging.INFO
        assert (f"{cause} step" in line[0].getMessage()) is (cause != "other")
    assert relay.freezes_ended_after == expected
    assert relay._freeze_steps == 0 and relay._freeze_delivered is None


async def test_an_undelivered_step_ends_as_other(tmp_path):
    server, relay = _nudging(tmp_path)
    _step(server, relay)                                   # queued, never polled
    relay._ingest_blocks(IDLE)
    assert relay.freezes_ended_after["other"] == 1


async def test_one_warning_per_step(tmp_path, caplog):
    server, relay = _nudging(tmp_path)
    caplog.set_level(logging.WARNING, logger="icomm_relay")
    for _ in range(3):
        _step(server, relay)
        await _drain(server)
    lines = [r.getMessage() for r in caplog.records
             if r.name == "icomm_relay" and r.levelno == logging.WARNING]
    assert len(lines) == 3
    assert "renewing the reporting mask" in lines[0]
    assert "step 2" in lines[1] and "150.0 F setpoint, then the reporting mask" in lines[1]
    assert "step 3" in lines[2] and "update rate 21601 s" in lines[2]


def test_a_probe_or_api_mask_is_not_a_step(tmp_path):
    server, relay = _nudging(tmp_path)
    relay.note_mask_delivered()
    assert relay.nudge_steps_sent == {"mask": 0, "pair": 0, "rate": 0}
