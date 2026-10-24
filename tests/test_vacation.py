"""Vacation mode: captured once from the app, then encoded, decoded and queued.

2026-10-24 00:12, the iCOMM app set "Vacation, 1 day". The relay logged the
frame below, and the tank's next telemetry echoed `01 02` as its mode pair.
"""

from __future__ import annotations

import json

import icomm_frames as frames
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from icomm_relay import Relay, decode
from test_security import _body

CAPTURED_VACATION_1_DAY = "BD 40 08 0B 0F 01 02 1C"
CAPTURED_VACATION_3_DAYS = "BD 40 08 0B 0F 03 02 66"       # 2026-10-24 09:56, app


def _with_mode(blocks: list[bytes], pair: tuple[int, int]) -> list[bytes]:
    b3 = bytearray(blocks[3])
    b3[30], b3[31] = pair
    return [*blocks[:3], bytes(b3), *blocks[4:]]


@pytest.fixture
def relay(tmp_path, monkeypatch, blocks_heat_pump) -> Relay:
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    relay = Relay()
    relay._ingest_blocks(blocks_heat_pump)
    return relay


def _post(body) -> web.Request:
    return make_mocked_request("POST", "/write", app=web.Application(), payload=_body(body))


# -- frames --------------------------------------------------------------------

def test_vacation_default_is_the_captured_frame_byte_for_byte():
    assert frames.as_text(frames.mode_frame("vacation")) == CAPTURED_VACATION_1_DAY
    assert frames.as_text(frames.mode_frame("vacation", 1)) == CAPTURED_VACATION_1_DAY


def test_vacation_days_go_in_the_first_byte():
    # Predicted before the app's 3-day capture, then matched byte for byte by it.
    assert frames.as_text(frames.mode_frame("vacation", 3)) == "BD 40 08 0B 0F 03 02 66"


@pytest.mark.parametrize(("frame", "days"), [(CAPTURED_VACATION_1_DAY, 1),
                                              (CAPTURED_VACATION_3_DAYS, 3)])
def test_captured_frames_decode_as_vacation_with_their_days(frame, days):
    decoded = frames.decode_frame(frame)
    assert decoded["crc_ok"]
    assert decoded["mode"] == "vacation" and decoded["vacation_days"] == days
    assert frames.as_text(frames.mode_frame("vacation", days)) == frame


@pytest.mark.parametrize("days", [0, 100, -1, "3", 1.5, True])
def test_bad_vacation_lengths_are_refused(days):
    with pytest.raises(ValueError):
        frames.mode_frame("vacation", days)


def test_days_only_apply_to_vacation():
    with pytest.raises(ValueError, match="only to vacation"):
        frames.mode_frame("heat_pump", 2)


def test_unknown_pair_with_another_second_byte_stays_unknown():
    frame = frames.as_text(frames.encode(frames.BLOCK_MODE, frames.OFFSET_MODE, b"\x01\x03"))
    assert frames.decode_frame(frame)["mode"] == "unknown"


# -- telemetry -----------------------------------------------------------------

def test_telemetry_pair_1_2_is_vacation_with_days(blocks_heat_pump):
    decoded = decode(_with_mode(blocks_heat_pump, (1, 2)))
    assert decoded["mode"] == "vacation"
    assert decoded["mode_raw"] == "1/2"
    assert decoded["vacation_days"] == 1


def test_other_modes_carry_no_vacation_days(blocks_heat_pump):
    decoded = decode(blocks_heat_pump)
    assert decoded["mode"] == "heat_pump" and decoded["vacation_days"] is None


# -- the write path --------------------------------------------------------------

async def test_write_api_queues_vacation_with_days(relay):
    response = await relay.write_handler(_post({"mode": "vacation", "days": 3}))
    assert response.status == 200
    queued = json.loads(response.body)["queued"]
    assert queued["value"] == "vacation" and queued["days"] == 3
    assert queued["frame"] == "BD 40 08 0B 0F 03 02 66"


async def test_write_api_vacation_without_days_is_one_day(relay):
    response = await relay.write_handler(_post({"mode": "vacation"}))
    queued = json.loads(response.body)["queued"]
    assert queued["frame"] == CAPTURED_VACATION_1_DAY and queued["days"] == 1


@pytest.mark.parametrize("body", [{"mode": "heat_pump", "days": 2},
                                  {"setpoint": 140, "days": 2},
                                  {"mode": "vacation", "days": 0},
                                  {"mode": "vacation", "days": "a week"}])
async def test_write_api_refuses_bad_days(relay, body):
    response = await relay.write_handler(_post(body))
    assert response.status == 400
    assert relay.pending == {}


def test_vacation_write_confirms_on_the_tank_reporting_vacation(relay, blocks_heat_pump):
    relay.queue_write("mode", "vacation", days=1)
    relay._ingest_blocks(blocks_heat_pump)                       # still heat pump
    assert "mode" in relay.pending
    relay._ingest_blocks(_with_mode(blocks_heat_pump, (1, 2)))   # tank applied it
    assert "mode" not in relay.pending
    assert relay.last_write["outcome"] == "confirmed"
    assert relay.last_write["days"] == 1


def test_vacation_write_survives_a_restart(relay, tmp_path):
    relay.queue_write("mode", "vacation", days=3)
    restored = Relay()                   # a new process reading the same file
    restored._load_queue()
    assert restored.pending["mode"].days == 3
    assert restored.pending["mode"].frame == "BD 40 08 0B 0F 03 02 66"


def test_app_vacation_write_is_logged_with_its_days(relay):
    reply = (b"<commands><properties><property><name>setBlockParameter</name>"
             b"<value>" + CAPTURED_VACATION_1_DAY.encode() + b"</value>"
             b"</property></properties></commands>")
    relay._note_cloud_write(reply, "/devices/1/commands.xml")
    assert relay.last_cloud_write["kind"] == "mode"
    assert relay.last_cloud_write["value"] == "vacation"
    assert relay.last_cloud_write["days"] == 1
