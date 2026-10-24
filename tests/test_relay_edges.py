"""Edge cases of the relay core: malformed input, persistence failures, and
the guards that decide whether a write is delivered, held, or claimed.

Each test names the input that used to be unexercised and asserts what the
relay does with it, not merely that it does not crash.
"""

from __future__ import annotations

import json
import logging
import time

import icomm_frames as frames
import pytest
from aiohttp.test_utils import make_mocked_request
from icomm_peers import PeerPolicy, normalize
from test_lan import _server
from test_security import _relay

EMPTY = b"<commands>\n  <properties/>\n  <schedules/>\n  <cmds/>\n</commands>\n"


# -- malformed telemetry and acks ------------------------------------------------

def test_truncated_blocks_leave_the_last_good_state(tmp_path, blocks_electric):
    relay = _server(tmp_path).relay
    relay._ingest_blocks(blocks_electric)
    before, at = dict(relay.state), relay.last_telemetry
    relay._ingest_blocks([b"\x00"] * 5)                # five blocks, all too short
    assert relay.state == before and relay.last_telemetry == at


async def test_undecodable_lan_telemetry_is_logged_not_ingested(tmp_path, caplog):
    relay = _server(tmp_path).relay
    await relay.ingest_lan_update({"data": {"name": "superBlockDyn1", "value": "zz,yy"}})
    assert relay.state == {"online": False}
    assert "LAN telemetry undecodable" in caplog.text


async def test_small_lan_properties_are_logged_with_their_value(tmp_path, caplog):
    caplog.set_level(logging.INFO, "icomm_relay")
    relay = _server(tmp_path).relay
    await relay.ingest_lan_update({"data": {"name": "periodicParameterSetting", "value": "60"}})
    await relay.ingest_lan_update({"data": {"name": "superBlockDyn1"}})
    assert "LAN update: periodicParameterSetting = 60" in caplog.text
    assert "LAN update: superBlockDyn1 (0 chars)" in caplog.text


def test_unparsable_ack_status_reads_as_rejected(tmp_path):
    relay = _server(tmp_path).relay
    write = relay.queue_write("setpoint", 141)
    write.command_id = "abc"
    relay._record_ack("abc", "garbage")
    assert write.ack_status == -1 and write.status == "rejected"
    relay._record_ack(None, 200)                       # no id: matches nothing
    assert write.ack_status == -1


# -- persistence ----------------------------------------------------------------------

def test_missing_queue_file_is_a_clean_start(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay._load_queue()
    assert relay.pending == {} and relay.queue_restored == 0 and relay.last_write is None


def test_restore_skips_expired_and_frameless_entries_but_keeps_last_write(
        tmp_path, monkeypatch, caplog):
    relay = _relay(tmp_path, monkeypatch)
    now = time.time()
    (tmp_path / "pending.json").write_text(json.dumps({
        "pending": {
            "setpoint": {"kind": "setpoint", "value": 140.0, "frame": "BD 40",
                         "queued": now - relay.queue_max_age_s - 5},
            "mode": {"kind": "mode", "value": "hybrid", "frame": "", "queued": now},
            "other": {"value": 1, "frame": "BD 41"},          # no kind/queued: defaults
        },
        "last_write": {"kind": "setpoint", "outcome": "confirmed"}}))
    relay._load_queue()
    assert set(relay.pending) == {"other"}
    other = relay.pending["other"]
    assert other.kind == "other" and other.restored and other.attempts == 0
    assert abs(other.queued - now) < 5                 # missing timestamp = now
    assert relay.queue_restored == 1
    assert relay.last_write == {"kind": "setpoint", "outcome": "confirmed"}
    assert "dropping expired setpoint write" in caplog.text


def test_unwritable_queue_is_warned_about_not_raised(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    relay = _relay(tmp_path, monkeypatch, QUEUE_FILE=str(blocker / "pending.json"))
    write = relay.queue_write("setpoint", 140)         # still queued in memory
    assert relay.pending["setpoint"] is write
    assert "could not persist the write queue" in caplog.text


# -- late confirmation ----------------------------------------------------------------

def test_late_confirmation_yields_to_a_newer_write(tmp_path):
    relay = _server(tmp_path).relay
    relay.last_write = {"kind": "setpoint", "value": 138.0, "outcome": "not_applied",
                        "delivered": time.time() - 60}
    relay.queue_write("setpoint", 145)
    relay.state["setpoint"] = 138.0
    relay._confirm_pending()
    assert relay.last_write["outcome"] == "not_applied"     # the new write owns it


def test_late_mode_confirmation_compares_the_mode(tmp_path):
    relay = _server(tmp_path).relay
    relay.last_write = {"kind": "mode", "value": "hybrid", "outcome": "unconfirmed",
                        "delivered": time.time() - 60}
    relay.state["mode"] = "electric"
    relay._confirm_pending()
    assert relay.last_write["outcome"] == "unconfirmed"
    relay.state["mode"] = "hybrid"
    relay._confirm_pending()
    assert relay.last_write["outcome"] == "confirmed"
    assert relay.last_write["earlier_outcome"] == "unconfirmed"


def test_confirmed_write_is_not_reclaimed(tmp_path):
    relay = _server(tmp_path).relay
    relay.last_write = {"kind": "mode", "value": "hybrid", "outcome": "confirmed",
                        "delivered": time.time()}
    relay.state["mode"] = "hybrid"
    assert relay._late_confirmation() is False


# -- injection guards -------------------------------------------------------------------

def test_degraded_appliance_holds_cloud_injection(tmp_path, blocks_electric):
    relay = _server(tmp_path).relay
    relay._ingest_blocks(blocks_electric)
    relay.queue_write("setpoint", 140)
    relay.health = lambda: {"state": "degraded"}
    assert relay._inject(EMPTY) is None
    assert relay.injections_held == 1
    assert relay.pending["setpoint"].attempts == 0


def test_nothing_deliverable_means_no_rewrite(tmp_path, blocks_electric):
    relay = _server(tmp_path).relay
    relay._ingest_blocks(blocks_electric)
    relay.health = lambda: {"state": "healthy"}
    write = relay.queue_write("setpoint", 140)
    write.attempts = 3
    assert relay._inject(EMPTY) is None and relay.injections_held == 0


def test_unreadable_cloud_frame_is_recorded_raw(tmp_path):
    relay = _server(tmp_path).relay
    reply = (b"<commands><properties><property><name>setBlockParameter</name>"
             b"<value>NOT HEX</value></property></properties></commands>")
    relay._note_cloud_write(reply, "/devices/1/commands.xml")
    assert relay.cloud_writes == 1
    assert relay.last_cloud_write["kind"] == "frame"
    assert relay.last_cloud_write["value"] == "NOT HEX"


def test_empty_cloud_frame_is_not_a_write(tmp_path):
    relay = _server(tmp_path).relay
    reply = (b"<commands><properties><property><name>setBlockParameter</name>"
             b"<value> </value></property></properties></commands>")
    relay._note_cloud_write(reply, "/devices/1/commands.xml")
    assert relay.cloud_writes == 0


@pytest.mark.parametrize(("payload", "ids_used"), [
    (b"<device/>", 0),                                # not a commands reply
    (b"<commands><properties/></commands>", 1),       # no <cmds> to carry it
])
def test_key_push_only_rides_a_commands_reply_with_cmds(tmp_path, monkeypatch, payload,
                                                        ids_used):
    relay = _relay(tmp_path, monkeypatch, LANIP_PUSH="1")
    assert relay._maybe_push_lanip(payload) is None
    # A reply without <cmds> burns a command id (harmless: ids only need to be fresh).
    assert relay.lanip_cmd_id == ids_used


def test_calling_for_heat_needs_both_temperatures(tmp_path):
    relay = _server(tmp_path).relay
    relay.state = {"setpoint": 140.0}
    assert relay._calling_for_heat() is False
    relay.state = {"setpoint": 140.0, "water_top": 137.0}
    assert relay._calling_for_heat() is True
    relay.state = {"setpoint": 140.0, "water_top": 139.0}
    assert relay._calling_for_heat() is False


# -- frames and peers --------------------------------------------------------------------

def test_unencodable_setpoint_is_refused():
    with pytest.raises(ValueError, match="encodable"):
        frames.setpoint_frame(-1000.0)


def test_mode_frame_with_an_uncaptured_payload_decodes_as_unknown():
    frame = frames.as_text(frames.encode(frames.BLOCK_MODE, frames.OFFSET_MODE, b"\x00\x09"))
    decoded = frames.decode_frame(frame)
    assert decoded["mode"] == "unknown" and decoded["crc_ok"] is True


def test_rewrite_adds_to_a_non_empty_properties_block():
    xml = ("<commands>\n  <properties>\n    <property><name>other</name>"
           "<value>1</value></property>\n  </properties>\n</commands>\n")
    out, replaced = frames.rewrite_commands(xml, "BD 40 08 0B 06 3C 00 6E")
    assert replaced is False
    assert out.index("setBlockParameter") < out.index("<name>other</name>")
    assert "<value>BD 40 08 0B 06 3C 00 6E</value>" in out and "<value>1</value>" in out


def test_normalize_handles_missing_and_mapped_addresses():
    assert normalize(None) == "" and normalize("") == ""
    assert normalize("::ffff:10.0.1.1") == "10.0.1.1"


def test_untrusted_peer_bookkeeping_is_bounded(monkeypatch):
    monkeypatch.delenv("TRUSTED_PEERS", raising=False)
    policy = PeerPolicy(lambda: None)
    policy._warned = {f"10.9.0.{i}": time.time() for i in range(257)}
    policy.rejected = {f"10.8.0.{i}": 1 for i in range(64)}
    transport = __import__("unittest").mock.Mock()
    transport.get_extra_info.side_effect = (
        lambda key, default=None: ("10.7.0.1", 1) if key == "peername" else default)
    request = make_mocked_request("GET", "/x", transport=transport)
    policy.note_untrusted(request, ":8444", "refused")
    assert policy._warned.keys() == {"10.7.0.1"}       # cleared, then this one noted
    assert "10.7.0.1" not in policy.rejected and len(policy.rejected) == 64
