"""The key-repair command: Ayla's own PUT lanip.json, delivered in <cmds>.

libada's client_lanip_json_put parses the same lanip JSON and saves it, so this
is the network-reachable way to replace a key the module already holds (the
situation the lan.json fetch cannot fix, since that only happens keyless).
XML shapes are pinned to libada's parsers: <resource> has no leading slash,
<uri> keeps it, <data> is raw JSON, one command per poll.
"""

import json

from icomm_relay import Relay

XML = '<commands>\n  <properties/>\n  <schedules/>\n  <cmds/>\n</commands>\n'


def _relay(tmp_path, monkeypatch) -> Relay:
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setenv("LANIP_KEY", "b" * 28)
    monkeypatch.setenv("LANIP_PUSH", "1")
    return Relay()


def test_push_inserts_a_lanip_put_with_our_key(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    out = relay._transform_commands(XML.encode()).decode()
    assert "<method>PUT</method>" in out
    assert "<resource>lanip.json</resource>" in out      # agent prepends "/"
    assert "<uri>/lanip.json</uri>" in out               # agent strips first char
    assert "<id>1</id>" in out
    body = out.split("<data>", 1)[1].split("</data>", 1)[0]
    assert json.loads(body) == {"lanip": {"status": "enable", "lanip_key_id": 1,
                                          "lanip_key": "b" * 28,
                                          "keep_alive": 30, "auto_sync": 1}}
    # second poll: a fresh command id, so the agent does not dedupe it
    assert "<id>2</id>" in relay._transform_commands(XML.encode()).decode()


def test_push_stops_once_the_module_acks(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.lanip_push_acked = True
    assert relay._transform_commands(XML.encode()) is None


def test_push_appends_to_a_nonempty_cmds_block(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    xml = "<commands><cmds><cmd><id>9</id></cmd></cmds></commands>"
    out = relay._transform_commands(xml.encode()).decode()
    assert "<id>9</id>" in out and "lanip.json" in out
    assert out.index("<id>9</id>") < out.index("lanip.json")


def test_push_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setenv("LANIP_KEY", "b" * 28)
    relay = Relay()
    assert relay._transform_commands(XML.encode()) is None


def test_foreign_key_detected_and_push_armed_automatically(tmp_path, monkeypatch):
    """No option needed: three key exchanges with no update between them is
    the signature of a module holding a key we did not issue."""
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setenv("LANIP_KEY", "b" * 28)
    relay = Relay()                                   # LANIP_PUSH not set
    assert relay._transform_commands(XML.encode()) is None
    for _ in range(3):
        relay.lan.exchanges_since_update += 1
    relay.lan.needs_key_push = True
    out = relay._transform_commands(XML.encode())
    assert out is not None and b"lanip.json" in out
    assert relay.lan.status()["needs_key_push"] is True


async def test_lanip_ack_is_answered_locally_and_disarms(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    relay = _relay(tmp_path, monkeypatch)
    relay.lan.needs_key_push = True
    request = make_mocked_request(
        "PUT", "/devices/29689890/lanip.json?cmd_id=1&status=200",
        app=web.Application())
    response = await relay._route(request)
    assert response.status == 200                     # not forwarded to Ayla
    assert relay.lanip_push_acked is True
    assert relay.lan.needs_key_push is False
