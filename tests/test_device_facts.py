"""Firmware and identity, from the config reply and the module's boot reports."""

from __future__ import annotations

import json

from icomm_device import DeviceFacts
from test_dsn import DSN
from test_proxy import FakeClient, _relay, _request

REAL_DSN = DSN.replace("<registered>0</registered>", """<registered>0</registered>
  <sw-version>bc 2.11.4 05/13/25 03:34:57 ID 0520f59</sw-version>
  <oem-model>iCOMMNextGenHP</oem-model>
  <mac>d01769f8f31c</mac>
  <manuf-model>LBEE5PA1LD-222</manuf-model>
  <ans-cipher-key>SECRETSECRET</ans-cipher-key>
  <key>12345</key>""")


async def _state(relay) -> dict:
    return json.loads((await relay.state_handler(None)).body)["device"]


async def test_the_config_reply_and_a_lan_report(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, REAL_DSN.encode())
    await relay._route(_request("GET", "/dsns/AC000W019949045.xml"))
    await relay.ingest_lan_update({"data": {"name": "active_image_version", "value": "2.14"}})
    await relay.ingest_lan_update({"data": {"name": "inactive_image_version", "value": "None"}})
    device = await _state(relay)
    assert device["controller_firmware"] == "2.14"
    assert "controller_firmware_standby" not in device         # "None": not a version
    assert device["module_firmware"].startswith("bc 2.11.4")
    assert (device["module_hardware"], device["model"]) == ("LBEE5PA1LD-222", "iCOMMNextGenHP")
    assert device["dsn"] == "AC000W019949045" and device["mac"] == "d01769f8f31c"
    saved = (tmp_path / "device.json").read_text()
    assert "SECRET" not in saved and "12345" not in saved       # never the keys


async def test_a_cloud_upload_and_static_blocks(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, b"")
    body = b"<datapoint><value>2.15</value></datapoint>"
    await relay._route(_request("POST", "/devices/1/properties/active_image_version/datapoints.xml",
                                body))
    assert relay.client.calls[-1]["data"] == body                # still forwarded intact
    await relay.ingest_lan_update({"data": {"name": "superBlockStatic1", "value": "00ab,cd"}})
    assert (await _state(relay))["controller_firmware"] == "2.15"
    raw = json.loads((await relay.raw_handler(None)).body) if relay.last_raw else None
    assert raw is None and relay.device.static == {"superBlockStatic1": "00ab,cd"}


def test_facts_survive_a_restart_and_junk_is_dropped(tmp_path):
    path = tmp_path / "device.json"
    facts = DeviceFacts(path)
    facts.note_property("active_image_version", "2.14")
    facts.note_property("active_image_version", "2.14")          # unchanged: no rewrite
    facts.note_property("superBlockStatic2", "zz-not-hex")
    facts.note_property("faultCode", "0")                        # not tracked
    facts.note_dsn_id("bad\nvalue")
    again = DeviceFacts(path)
    assert again.status()["controller_firmware"] == "2.14" and again.static == {}
    path.write_text(json.dumps({"facts": {"x": "\u0000bad", "y": 3}, "static": {"nope": "00"}}))
    assert DeviceFacts(path).facts == {} and DeviceFacts(path).static == {}
    path.write_text("not json")
    assert DeviceFacts(path).status() == {"updated": None}


def test_an_unwritable_store_is_only_a_warning(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("")
    facts = DeviceFacts(blocker / "device.json")
    facts.note_property("active_image_version", "2.14")
    assert facts.status()["controller_firmware"] == "2.14"
    assert "could not save device facts" in caplog.text


async def test_raw_includes_the_static_blocks(tmp_path, monkeypatch):
    from test_proxy import _telemetry_body
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, b"")
    await relay._route(_request("POST", "/devices/1/properties/superBlockDyn1/datapoints.xml",
                                _telemetry_body()))
    relay.device.note_property("superBlockStatic3", "0102")
    raw = json.loads((await relay.raw_handler(None)).body)
    assert raw["static"] == {"superBlockStatic3": "0102"} and raw["blocks"]


def test_identity_is_seeded_from_the_saved_config_reply(tmp_path, monkeypatch):
    (tmp_path / "dsn-original.xml").write_text(REAL_DSN)
    relay = _relay(tmp_path, monkeypatch, DEVICE_DSN="AC000W019949045")
    facts = relay.device.status()
    assert facts["module_hardware"] == "LBEE5PA1LD-222" and facts["dsn"] == "AC000W019949045"


async def test_the_firmware_is_read_once_over_lan_when_unknown(tmp_path):
    import base64

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    from test_lan import EXCHANGE, SECRET, _server, module_directions
    server = _server(tmp_path)
    relay = server.relay
    await relay.ingest_lan_update({"data": {"name": "faultCode", "value": "0"}})
    await relay.ingest_lan_update({"data": {"name": "faultCode", "value": "0"}})
    assert server.read_queue == ["active_image_version"]          # asked once
    poll = make_mocked_request("GET", "/local_lan/commands.json", app=web.Application())
    _, rx = module_directions(SECRET.encode(), EXCHANGE, server.session.random_2,
                              server.session.time_2)
    reply = json.loads(rx["cipher"].decrypt(base64.b64decode(json.loads(
        (await server._commands(poll)).body)["enc"])).rstrip(bytes([0])).decode())
    cmd = reply["data"]["cmds"][0]["cmd"]
    assert cmd["method"] == "GET" and cmd["resource"] == "property.json?name=active_image_version"
    assert server.read_queue == []
    await relay.ingest_lan_update({"data": {"name": "active_image_version", "value": "2.14"}})
    assert relay.device.status()["controller_firmware"] == "2.14"
