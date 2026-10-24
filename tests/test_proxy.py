"""The upstream proxy, the module-facing router, and the HA-facing handlers.

Ayla is replaced by a fake client that records what it was asked and answers
with a canned status/body/headers, or fails the way a real outage does. These
pin the forwarding contract: the cloud sees the module's request verbatim, the
module sees Ayla's reply verbatim (minus hop-by-hop headers), and an outage
never becomes the module's problem.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest import mock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from icomm_peers import TRUSTED
from test_dsn import DSN
from test_lan import SECRET

from relay.icomm_relay import Relay

FIXTURES = Path(__file__).parent / "fixtures"
EMPTY_COMMANDS = "<commands>\n  <properties/>\n  <schedules/>\n  <cmds/>\n</commands>\n"
CONFIG_REPLY = ("<commands><properties><property><name>superBlockDynDataUpdateRate</name>"
                "<value>21601</value></property></properties></commands>")
TELEMETRY = "/devices/1/properties/superBlockDyn1/datapoints.xml"


class FakeUpstream:
    """What `async with client.request(...)` yields: status, headers, read()."""

    def __init__(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeClient:
    """Records each upstream request; answers or raises as configured."""

    def __init__(self, status: int = 200, body: bytes = b"", headers=None,
                 error: Exception | None = None) -> None:
        self.status, self.body, self.headers, self.error = status, body, headers, error
        self.calls: list[dict] = []

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        if self.error is not None:
            raise self.error
        return FakeUpstream(self.status, self.body, self.headers)


def _raw(data: bytes) -> aiohttp.StreamReader:
    reader = aiohttp.StreamReader(mock.Mock(_reading_paused=False), 2**16,
                                  loop=asyncio.get_running_loop())
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def _request(method: str, path: str, body: bytes = b"", headers=None, trusted=True):
    request = make_mocked_request(method, path, headers=headers or {},
                                  payload=_raw(body), app=web.Application())
    if not trusted:
        request[TRUSTED] = False
    return request


def _relay(tmp_path, monkeypatch, **env) -> Relay:
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setenv("LANIP_KEY", SECRET)
    monkeypatch.setenv("UPSTREAM", "ayla.test")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Relay()


def _telemetry_body(name: str = "telemetry_electric.json") -> bytes:
    blocks = json.loads((FIXTURES / name).read_text())["blocks"]
    return (f"<datapoint><value>{','.join(blocks)}</value></datapoint>").encode()


# -- forwarding, verbatim both ways ---------------------------------------------

async def test_request_forwarded_verbatim_minus_hop_by_hop_headers(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, b"<ok/>", headers={
        "Content-Type": "application/xml", "X-Ayla-Trace": "abc",
        "Content-Length": "5", "Transfer-Encoding": "chunked",
        "Connection": "keep-alive", "Content-Encoding": "gzip"})
    request = _request("PUT", "/devices/1/thing.xml?a=1", b"<body/>", headers={
        "Host": "ads-field.aylanetworks.com", "Content-Length": "7",
        "Connection": "close", "Transfer-Encoding": "chunked",
        "Authorization": "auth_token xyz", "Content-Type": "application/xml"})
    response = await relay.proxy(request)

    call, = relay.client.calls
    assert call["method"] == "PUT"
    assert call["url"] == "https://ayla.test/devices/1/thing.xml?a=1"
    assert call["data"] == b"<body/>"
    assert call["allow_redirects"] is False
    assert call["ssl"] is relay.upstream_ssl
    assert call["headers"] == {"Authorization": "auth_token xyz",
                               "Content-Type": "application/xml"}

    assert response.status == 200
    assert response.body == b"<ok/>"
    assert response.headers["X-Ayla-Trace"] == "abc"
    assert response.headers["Content-Type"] == "application/xml"
    # aiohttp recomputes framing; a stale gzip label would corrupt the body.
    assert "Content-Encoding" not in response.headers
    assert "Transfer-Encoding" not in response.headers
    assert relay.upstream_errors == 0


async def test_error_status_is_passed_through_without_transform(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(404, b"not found")
    transform = mock.Mock(return_value=b"rewritten")
    response = await relay.proxy(_request("GET", "/devices/1/commands.xml"), transform)
    assert response.status == 404 and response.body == b"not found"
    transform.assert_not_called()


async def test_transform_applies_on_200_and_none_means_unchanged(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, b"original")
    response = await relay.proxy(_request("GET", "/x"), lambda payload: payload + b"+ours")
    assert response.body == b"original+ours"
    response = await relay.proxy(_request("GET", "/x"), lambda payload: None)
    assert response.body == b"original"


async def test_reporting_interval_learned_from_a_trusted_config_reply(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, CONFIG_REPLY.encode())
    await relay.proxy(_request("GET", "/devices/1/commands.xml?input=true", trusted=False))
    assert relay.reporting_interval_s is None          # a stranger cannot set it
    await relay.proxy(_request("GET", "/devices/1/other.xml"))
    assert relay.reporting_interval_s is None          # only command replies count
    relay.client.status = 500
    await relay.proxy(_request("GET", "/devices/1/commands.xml"))
    assert relay.reporting_interval_s is None          # only successful ones
    relay.client.status = 200
    await relay.proxy(_request("GET", "/devices/1/commands.xml?input=true"))
    assert relay.reporting_interval_s == 21601


# -- Ayla down: synthetic replies keep the module's conversation alive ----------

async def test_outage_post_is_answered_200_empty(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(error=aiohttp.ClientConnectionError("down"))
    response = await relay.proxy(_request("POST", TELEMETRY, b"<datapoint/>"))
    assert response.status == 200 and response.body == b""
    assert relay.upstream_errors == 1


async def test_outage_other_get_is_503(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(error=TimeoutError())
    response = await relay.proxy(_request("GET", "/devices/1/thing.xml"))
    assert response.status == 503
    assert relay.upstream_errors == 1


# Regression (fixed 0.4.6): the synthetic reply passed `text_type=` to
# web.Response, a TypeError, so every poll during an Ayla outage got a 500 and
# the queued write it should have carried was never delivered.


async def test_outage_poll_gets_a_synthetic_empty_commands_reply(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = None                                 # not started: same as an outage
    response = await relay.proxy(_request("GET", "/devices/1/commands.xml"))
    assert response.status == 200
    assert response.content_type == "application/xml"
    assert response.text == EMPTY_COMMANDS
    response = await relay.proxy(_request("GET", "/devices/1/commands.xml"),
                                 transform=lambda payload: None)
    assert response.text == EMPTY_COMMANDS
    assert relay.upstream_errors == 2


async def test_outage_poll_still_carries_a_queued_write(tmp_path, monkeypatch, blocks_electric):
    """Ayla down is exactly when the synthetic reply is the only carrier."""
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(error=aiohttp.ClientConnectionError("down"))
    relay._ingest_blocks(blocks_electric)
    write = relay.queue_write("setpoint", 140)
    response = await relay._route(_request("GET", "/devices/1/commands.xml"))
    assert response.status == 200
    assert "<name>setBlockParameter</name>" in response.text
    assert write.frame in response.text
    assert write.attempts == 1 and write.delivery_path == "cloud"
    assert relay.upstream_errors == 1


# -- the module-facing router ------------------------------------------------------

async def test_telemetry_is_decoded_then_forwarded_unchanged(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch, DEVICE_DSN="AC000W000000001")
    relay.client = FakeClient(201, b"")
    body = _telemetry_body()
    response = await relay._route(_request("POST", TELEMETRY, body))
    assert response.status == 201
    assert relay.client.calls[0]["data"] == body
    assert relay.state["online"] is True
    assert relay.state["watts"] == 4500 and relay.state["mode"] == "electric"
    assert relay.state["dsn"] == "AC000W000000001"
    assert relay.telemetry_times and relay._dial["requests"] == 1


async def test_telemetry_nudge_answers_401_without_forwarding(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, b"")
    relay.nudges_left, relay.nudge_on_telemetry = 1, True
    response = await relay._route(_request("POST", TELEMETRY, _telemetry_body()))
    assert response.status == 401
    assert relay.client.calls == [] and relay.nudges_left == 0
    assert relay.state == {"online": False}            # nudged, not decoded
    response = await relay._route(_request("POST", TELEMETRY, _telemetry_body()))
    assert response.status == 200 and len(relay.client.calls) == 1


async def test_get_nudge_401s_once_and_never_touches_posts(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, b"<ok/>")
    relay.nudges_left = 1
    assert (await relay._route(_request("POST", "/devices/1/x.xml"))).status == 200
    assert relay.nudges_left == 1
    assert (await relay._route(_request("GET", "/devices/1/x.xml"))).status == 401
    assert relay.nudges_left == 0
    assert (await relay._route(_request("GET", "/devices/1/x.xml"))).status == 200
    assert len(relay.client.calls) == 2


async def test_dsn_reply_rewritten_through_the_router(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, DSN.encode())
    response = await relay._route(_request("GET", "/dsns/AC000W000000001.xml"))
    assert b"<lan-enabled>true</lan-enabled>" in response.body
    assert b"<local-access-enabled>true</local-access-enabled>" in response.body
    assert relay.lan.module_ip == "10.0.1.187"


async def test_dsn_reply_untouched_with_lan_mode_off(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch, LAN_MODE="0")
    relay.client = FakeClient(200, DSN.encode())
    response = await relay._route(_request("GET", "/dsns/AC000W000000001.xml"))
    assert response.body == DSN.encode()
    # ...and lan.json is not answered locally either: there is no key to give.
    relay.client.body = b"upstream"
    response = await relay._route(_request("GET", "/devices/1/lan.json"))
    assert response.body == b"upstream"


async def test_dsn_rewrite_flag_works_without_lan_mode(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch, LAN_MODE="0", DSN_LAN_REWRITE="1")
    relay.client = FakeClient(200, DSN.encode())
    response = await relay._route(_request("GET", "/dsns/AC000W000000001.xml"))
    assert b"<lan-enabled>true</lan-enabled>" in response.body


async def test_failed_lanip_command_response_does_not_disarm(tmp_path, monkeypatch, caplog):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, b"")
    relay.lan.needs_key_push = True
    response = await relay._route(
        _request("PUT", "/devices/1/lanip.json?cmd_id=3&status=500"))
    assert response.status == 200
    assert relay.client.calls == []                     # our command, never forwarded
    assert relay.lan.needs_key_push is True
    assert relay.lanip_push_acked is False
    assert "lanip command response: cmd_id=3&status=500" in caplog.text


async def test_untrusted_peer_is_not_counted_as_a_dial(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, b"")
    await relay._route(_request("GET", "/devices/1/commands.xml", trusted=False))
    assert relay._dial is None and relay.client.calls[0]["method"] == "GET"


async def test_debug_requests_prints_each_path(tmp_path, monkeypatch, capsys):
    relay = _relay(tmp_path, monkeypatch, DEBUG_REQUESTS="1")
    relay._note_request("/devices/1/commands.xml")
    assert "/devices/1/commands.xml" in capsys.readouterr().out


# -- HA-facing handlers --------------------------------------------------------------

def _json_post(path: str, raw: bytes):
    return make_mocked_request("POST", path, payload=_raw(raw), app=web.Application())


async def test_nudge_handler_defaults_and_options(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    for raw in (b"not json", b"[1, 2]", b"{}"):
        body = json.loads((await relay.nudge_handler(_json_post("/nudge", raw))).body)
        assert body == {"nudges_left": 1, "on_telemetry": False}
    raw = json.dumps({"attempts": 2, "on_telemetry": True}).encode()
    body = json.loads((await relay.nudge_handler(_json_post("/nudge", raw))).body)
    assert body == {"nudges_left": 2, "on_telemetry": True}
    assert relay.nudges_left == 2 and relay.nudge_on_telemetry is True
    raw = json.dumps({"attempts": -5}).encode()
    body = json.loads((await relay.nudge_handler(_json_post("/nudge", raw))).body)
    assert body["nudges_left"] == 0


async def test_write_handler_queues_and_reports(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    response = await relay.write_handler(_json_post("/write", b'{"setpoint": 140}'))
    body = json.loads(response.body)
    assert response.status == 200
    assert body["queued"]["kind"] == "setpoint" and body["queued"]["value"] == 140.0
    assert body["queued"]["status"] == "queued"
    assert body["pending"] == 1 and body["lan_registered"] is False
    response = await relay.write_handler(_json_post("/write", b'{"mode": "hybrid"}'))
    assert json.loads(response.body)["pending"] == 2


async def test_write_handler_rejects_bad_bodies(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    response = await relay.write_handler(_json_post("/write", b"{nope"))
    assert response.status == 400 and json.loads(response.body)["error"] == "expected JSON"
    response = await relay.write_handler(_json_post("/write", b'{"volume": 11}'))
    assert response.status == 400
    assert json.loads(response.body)["error"] == "give setpoint or mode"
    response = await relay.write_handler(_json_post("/write", b'{"setpoint": 200}'))
    assert response.status == 400 and "outside" in json.loads(response.body)["error"]
    assert relay.pending == {}


async def test_cancel_drops_everything_and_persists(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.queue_write("setpoint", 140)
    relay.queue_write("mode", "hybrid")
    body = json.loads((await relay.write_cancel_handler(None)).body)
    assert set(body["cancelled"]) == {"setpoint", "mode"}
    assert body["cancelled"]["setpoint"]["value"] == 140.0
    assert relay.pending == {}
    assert json.loads((tmp_path / "pending.json").read_text())["pending"] == {}


async def test_state_handler_reports_everything_the_integration_reads(
        tmp_path, monkeypatch, blocks_electric):
    relay = _relay(tmp_path, monkeypatch)
    relay._ingest_blocks(blocks_electric)
    relay.upstream_errors = 4
    relay.queue_write("setpoint", 140)
    body = json.loads((await relay.state_handler(None)).body)
    assert body["online"] is True and body["fresh"] is True
    assert body["watts"] == 4500
    assert 0 <= body["last_telemetry_age"] < 5
    assert body["upstream_errors"] == 4
    assert body["pending"]["setpoint"]["status"] == "queued"
    assert body["health"]["state"] == "unknown"           # too few dials yet
    assert body["queue_file"] == str(tmp_path / "pending.json")
    assert body["queue_restored"] == 0
    assert body["writes_delivered"] == 0 and body["writes_confirmed"] == 0
    assert body["injections_held"] == 0 and body["cloud_writes"] == 0
    assert body["last_cloud_write"] is None and body["last_write"] is None
    assert body["lan"]["enabled"] is True and body["lan"]["registered"] is False
    assert set(body["peers"]) == {"gateways", "extra", "allow_all", "rejected"}

    relay.last_telemetry = time.time() - 601
    body = json.loads((await relay.state_handler(None)).body)
    assert body["fresh"] is False


# -- startup --------------------------------------------------------------------------

class FakeSite:
    started: list = []

    def __init__(self, runner, host, port, ssl_context=None) -> None:
        self.runner, self.host, self.port, self.ssl_context = runner, host, port, ssl_context

    async def start(self) -> None:
        FakeSite.started.append(self)


async def test_run_binds_all_listeners_restores_the_queue_and_cleans_up(
        tmp_path, monkeypatch, capsys):
    relay = _relay(tmp_path, monkeypatch, STATE_PORT="18124", LISTEN_PORT="18444",
                   LAN_PORT="18888")
    (tmp_path / "pending.json").write_text(json.dumps({"pending": {"setpoint": {
        "kind": "setpoint", "value": 140.0, "frame": "BD 40 08 0B 06 3C 00 6E",
        "queued": time.time()}}, "last_write": None}))
    FakeSite.started = []
    monkeypatch.setattr(web, "TCPSite", FakeSite)
    tls = object()
    relay.tls_context = lambda: tls

    task = asyncio.create_task(relay.run())
    for _ in range(200):
        await asyncio.sleep(0)
        if relay.client is not None:
            break
    assert relay.client is not None and not task.done()

    by_port = {site.port: site for site in FakeSite.started}
    assert set(by_port) == {18124, 18444, 18888}
    assert by_port[18124].host == "127.0.0.1" and by_port[18124].ssl_context is None
    assert by_port[18444].host == "0.0.0.0" and by_port[18444].ssl_context is tls
    state_routes = {r.resource.canonical for r in by_port[18124].runner.app.router.routes()}
    assert state_routes == {"/state", "/health", "/nudge", "/write", "/write/cancel"}
    assert relay.pending["setpoint"].restored is True
    out = capsys.readouterr().out
    assert "module on :18444 (TLS)" in out and "LAN on :18888" in out

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert relay.client.closed
    await asyncio.sleep(0)
    assert relay.lan._task.done()


async def test_run_says_lan_off(tmp_path, monkeypatch, capsys):
    relay = _relay(tmp_path, monkeypatch, LAN_MODE="0")
    FakeSite.started = []
    monkeypatch.setattr(web, "TCPSite", FakeSite)
    relay.tls_context = lambda: None
    task = asyncio.create_task(relay.run())
    for _ in range(200):
        await asyncio.sleep(0)
        if relay.client is not None:
            break
    assert "LAN off" in capsys.readouterr().out
    assert len(FakeSite.started) == 2                   # no LAN listener
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert relay.client.closed

