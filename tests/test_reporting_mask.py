"""Renewing the controller's periodic reporting (research phase 42).

The controller's live reporting lapses some hours after the last A.O. Smith app
session; the module then re-sends one frozen snapshot. The app's session
re-arms it with a periodicParameterSetting mask. The relay can send the same
mask over the LAN session, by hand (POST /lan/reporting-mask) or automatically
when the contents freeze (REPORTING_MASK_RENEW=1).
"""

from __future__ import annotations

import base64
import json

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from icomm_lan import APP_REPORTING_MASK, MASK_RE
from icomm_relay import telemetry_blocks
from test_lan import EXCHANGE, SECRET, _server, module_directions
from test_proxy import _telemetry_body

POLL = make_mocked_request("GET", "/local_lan/commands.json", app=web.Application())


def _decrypt(server, response) -> dict:
    """One module-side cipher per session: CBC state runs across replies."""
    if not hasattr(server, "_test_rx"):
        server._test_rx = module_directions(SECRET.encode(), EXCHANGE,
                                            server.session.random_2, server.session.time_2)[1]
    rx = server._test_rx
    return json.loads(rx["cipher"].decrypt(base64.b64decode(
        json.loads(response.body)["enc"])).rstrip(bytes([0])).decode())


def test_the_captured_mask_has_the_documented_shape():
    assert MASK_RE.match(APP_REPORTING_MASK)
    assert [len(p) for p in APP_REPORTING_MASK.split(",")] == [73, 105, 26, 26]


async def test_a_requested_mask_rides_the_next_poll_once(tmp_path):
    server = _server(tmp_path)
    assert server.request_mask() is True
    prop = _decrypt(server, await server._commands(POLL))["data"]["properties"][0]["property"]
    assert prop["name"] == "periodicParameterSetting"
    assert prop["value"] == APP_REPORTING_MASK and prop["base_type"] == "string"
    assert server.masks_sent == 1 and server.status()["last_mask_sent_age"] is not None
    assert _decrypt(server, await server._commands(POLL))["data"] != {"properties": [prop]}


async def test_a_queued_write_goes_first(tmp_path):
    server = _server(tmp_path)
    server.relay.queue_write("setpoint", 140)
    server.request_mask()
    first = _decrypt(server, await server._commands(POLL))["data"]["properties"][0]["property"]
    second = _decrypt(server, await server._commands(POLL))["data"]["properties"][0]["property"]
    assert (first["name"], second["name"]) == ("setBlockParameter", "periodicParameterSetting")


def test_no_session_no_mask(tmp_path):
    server = _server(tmp_path)
    server.session = None
    assert server.request_mask() is False and server.mask_pending is False


async def test_the_state_api_trigger(tmp_path):
    server = _server(tmp_path)
    reply = await server.relay.reporting_mask_handler(None)
    assert json.loads(reply.body) == {"queued": True} and server.mask_pending
    server.session = None
    reply = await server.relay.reporting_mask_handler(None)
    assert reply.status == 409


async def test_a_newer_mask_from_the_app_is_learned(tmp_path):
    server = _server(tmp_path)
    newer = "1" + APP_REPORTING_MASK[1:]
    for value in (newer, "garbage", ""):
        await server.relay.ingest_lan_update(
            {"data": {"name": "periodicParameterSetting", "value": value}})
    assert server.reporting_mask == newer


def _renewing_relay(tmp_path, enabled="1"):
    return _server(tmp_path, REPORTING_MASK_RENEW=enabled)


def test_frozen_contents_renew_the_mask_once_per_interval(tmp_path):
    server = _renewing_relay(tmp_path)
    relay = server.relay
    blocks = telemetry_blocks(_telemetry_body())
    relay._ingest_blocks(blocks)
    assert server.mask_pending is False                   # fresh contents
    relay.contents_changed -= 1900
    relay._ingest_blocks(blocks)                          # frozen for 31+ min
    assert server.mask_pending is True
    server.mask_pending, server.last_mask_sent = False, __import__("time").time()
    relay._ingest_blocks(blocks)                          # just sent: wait an interval
    assert server.mask_pending is False


def test_renewal_is_on_by_default_and_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.delenv("REPORTING_MASK_RENEW", raising=False)
    assert _server(tmp_path).relay.mask_renew is True
    server = _renewing_relay(tmp_path / "off", enabled="0")
    relay = server.relay
    relay._ingest_blocks(telemetry_blocks(_telemetry_body()))
    relay.contents_changed -= 7200
    relay._ingest_blocks(telemetry_blocks(_telemetry_body()))
    assert server.mask_pending is False
