"""App writes made visible, and HA writes delivered without waiting for a poll."""

import asyncio
import time

from test_lan import _server

COMMAND_REPLY = ("<commands>\n  <properties>\n    <property>\n      <base-type>string</base-type>\n"
                 "      <mime-type></mime-type>\n      <name>setBlockParameter</name>\n"
                 "      <value>BD 40 08 0B 06 3C 8E 5E</value>\n    </property>\n"
                 "  </properties>\n  <schedules/>\n  <cmds/>\n</commands>\n")


def test_app_write_on_the_cloud_path_is_recorded(tmp_path):
    relay = _server(tmp_path).relay
    assert relay._transform_commands(COMMAND_REPLY.encode(), "/devices/1/commands.xml") is None
    last = relay.last_cloud_write
    assert (last["kind"], last["value"], last["source"]) == ("setpoint", 141.0, "cloud")
    assert last["replaced_by_local"] is False
    assert relay.cloud_writes == 1


def test_reregistration_state_is_not_mistaken_for_a_write(tmp_path):
    relay = _server(tmp_path).relay
    relay._transform_commands(COMMAND_REPLY.encode(), "/devices/1/commands.xml?input=true")
    relay._transform_commands(b"<commands>\n  <properties/>\n  <cmds/>\n</commands>\n",
                              "/devices/1/commands.xml")
    assert relay.last_cloud_write is None and relay.cloud_writes == 0


def test_app_write_replaced_by_a_queued_local_write_is_flagged(tmp_path, blocks_electric):
    relay = _server(tmp_path).relay
    relay._ingest_blocks(blocks_electric)
    relay.health = lambda: {"state": "healthy"}
    relay.queue_write("setpoint", 139)
    out = relay._transform_commands(COMMAND_REPLY.encode(), "/devices/1/commands.xml")
    assert out is not None and b"3C 8E 5E" not in out
    assert relay.last_cloud_write["replaced_by_local"] is True


async def test_queued_write_wakes_the_heartbeat_at_once(tmp_path):
    lan = _server(tmp_path)
    lan._wake_event = asyncio.Event()
    lan.registered = True
    lan.relay.queue_write("setpoint", 141)
    assert lan._wake_event.is_set() and lan.wakes == 1
    started = time.monotonic()
    await lan._pause(10.0)                     # returns at once instead of after 10 s
    assert time.monotonic() - started < 0.5
    assert not lan._wake_event.is_set()


async def test_no_wake_without_a_registration(tmp_path):
    lan = _server(tmp_path)
    lan._wake_event = asyncio.Event()
    lan.registered = False
    lan.relay.queue_write("setpoint", 141)
    assert not lan._wake_event.is_set()


async def test_write_ack_wakes_for_the_confirming_read(tmp_path):
    lan = _server(tmp_path)
    lan._wake_event = asyncio.Event()
    lan.registered = True
    await lan.relay.ingest_lan_update({"data": {"id": "x", "ack_status": 200}})
    assert lan.refresh_pending and lan._wake_event.is_set()


def test_cloud_injection_survives_the_transform_without_a_key_push(tmp_path, blocks_electric):
    """Regression (0.3.1-0.4.1): the injected reply was dropped unless a lanip push
    was armed, so cloud-path writes never reached the module."""
    relay = _server(tmp_path).relay
    relay._ingest_blocks(blocks_electric)
    relay.health = lambda: {"state": "healthy"}
    relay.queue_write("setpoint", 139)
    empty = b"<commands>\n  <properties/>\n  <schedules/>\n  <cmds/>\n</commands>\n"
    out = relay._transform_commands(empty, "/devices/1/commands.xml")
    assert out is not None and b"setBlockParameter" in out
    assert relay.pending["setpoint"].delivery_path == "cloud"
