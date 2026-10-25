"""The LAN server's lifecycle, heartbeat loop, endpoints' error paths, and key file.

Complements test_lan.py (protocol, against an independent module side) and
test_security.py (who may talk to it). Nothing here opens a socket: listeners
are faked, and the module's HTTP server is a scripted client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import icomm_lan
import pytest
from aiohttp import web
from icomm_lan import LanServer
from test_lan import EXCHANGE, SECRET, _server
from test_security import MODULE, _relay


class _Json:
    """A request whose .json() returns (or raises) what the test says."""

    def __init__(self, value) -> None:
        self.value = value

    async def json(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class _Reply:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ModuleHttp:
    """The module's local_reg.json endpoint, answering a scripted status list."""

    def __init__(self, statuses) -> None:
        self.statuses = list(statuses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, json=None):
        self.calls.append((method, url, json))
        status = self.statuses.pop(0)
        if isinstance(status, BaseException):
            raise status
        return _Reply(status)


def _stop_after(lan: LanServer, rounds: int) -> list[float]:
    """Replace the inter-heartbeat pause; record delays and stop after `rounds`."""
    delays: list[float] = []

    async def pause(delay):
        delays.append(delay)
        if len(delays) >= rounds:
            raise asyncio.CancelledError

    lan._pause = pause
    return delays


# -- lifecycle -------------------------------------------------------------------

class _Site:
    started: list = []

    def __init__(self, runner, host, port, ssl_context=None) -> None:
        self.runner, self.host, self.port = runner, host, port

    async def start(self) -> None:
        _Site.started.append(self)


async def test_start_serves_the_four_endpoints_and_stop_tears_down(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch, LAN_PORT="18888")
    lan = relay.lan
    lan.module_ip = None                       # loop idles; nothing to register with
    _Site.started = []
    monkeypatch.setattr(web, "TCPSite", _Site)
    await lan.start()
    site, = _Site.started
    assert (site.host, site.port) == ("0.0.0.0", 18888)
    paths = {r.resource.canonical for r in site.runner.app.router.routes()}
    assert paths == {"/local_lan/key_exchange.json", "/local_lan/commands.json",
                     "/local_lan/property/datapoint.json",
                     "/local_lan/property/datapoint/ack.json"}
    assert lan._task is not None and not lan._task.done()
    assert lan._wake_event is not None

    await lan.stop()
    await asyncio.sleep(0)
    assert lan._task.cancelled()
    assert lan._client.closed


async def test_disabled_lan_starts_nothing(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch, LAN_MODE="0")
    lan = relay.lan
    assert lan.key == ""                       # no key minted for a disabled server
    await lan.start()
    assert lan._task is None and lan._runner is None
    await lan.stop()                           # harmless when nothing started
    assert relay._maybe_push_lanip(b"<commands><cmds/></commands>") is None


async def test_pause_without_a_wake_event_just_sleeps(tmp_path, monkeypatch):
    lan = _server(tmp_path)
    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(icomm_lan.asyncio, "sleep", fake_sleep)
    await lan._pause(7.0)
    assert slept == [7.0]


# -- the heartbeat loop -------------------------------------------------------------

async def test_heartbeat_registers_then_puts_and_reposts_after_loss(tmp_path, caplog):
    lan = _server(tmp_path)
    lan.module_ip, lan.advertise_ip = MODULE, "10.0.1.113"
    lan._client = _ModuleHttp([202, 202, 404, 412, 202])
    delays = _stop_after(lan, 5)
    caplog.set_level(logging.WARNING, "icomm_lan")
    with pytest.raises(asyncio.CancelledError):
        await lan._registration_loop()

    assert [c[0] for c in lan._client.calls] == ["POST", "PUT", "PUT", "POST", "POST"]
    assert all(c[1] == f"http://{MODULE}/local_reg.json" for c in lan._client.calls)
    assert lan._client.calls[0][2] == {"local_reg": {
        "ip": "10.0.1.113", "port": 8888, "uri": "/local_lan", "notify": 1}}
    # 404 (LAN not armed) and 412 (no key yet) back off; 202 keeps the 10 s cadence.
    assert delays == [10.0, 10.0, 15.0, 15.0, 10.0]
    assert lan.registered is True and lan.last_registration_result == "POST 202"
    assert lan.last_heartbeat > 0
    assert caplog.text.count("registered with the module") == 2
    assert caplog.text.count("registration lost (PUT 404)") == 1   # logged once, not on 412


async def test_heartbeat_waits_for_a_module_address(tmp_path):
    lan = _server(tmp_path)
    lan.module_ip = None
    lan._client = _ModuleHttp([])
    delays = _stop_after(lan, 2)
    with pytest.raises(asyncio.CancelledError):
        await lan._registration_loop()
    assert delays == [5.0, 5.0] and lan._client.calls == []


async def test_heartbeat_cancellation_is_not_swallowed(tmp_path):
    lan = _server(tmp_path)
    lan.module_ip, lan.advertise_ip = MODULE, "10.0.1.113"
    lan._client = _ModuleHttp([asyncio.CancelledError()])
    _stop_after(lan, 99)
    with pytest.raises(asyncio.CancelledError):
        await lan._registration_loop()
    assert lan.registration_failures == 0


async def test_heartbeat_failure_logged_once_per_error_kind(tmp_path, caplog):
    lan = _server(tmp_path)
    lan.module_ip, lan.advertise_ip = MODULE, "10.0.1.113"
    lan._client = _ModuleHttp([OSError("no route"), OSError("no route"), TimeoutError()])
    delays = _stop_after(lan, 3)
    caplog.set_level(logging.WARNING, "icomm_lan")
    with pytest.raises(asyncio.CancelledError):
        await lan._registration_loop()
    assert delays == [15.0, 15.0, 15.0]
    assert lan.registration_failures == 3
    assert caplog.text.count("heartbeat failed: OSError") == 1
    assert caplog.text.count("heartbeat failed: TimeoutError") == 1


def test_registration_body_detects_our_address_on_the_modules_subnet(tmp_path, monkeypatch):
    lan = _server(tmp_path)
    lan.module_ip = MODULE
    sockets = []

    class FakeSocket:
        def __init__(self, family, kind) -> None:
            self.family, self.kind, self.closed = family, kind, False
            sockets.append(self)

        def connect(self, address) -> None:
            self.connected = address

        def getsockname(self):
            return ("10.0.1.113", 50000)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(icomm_lan.socket, "socket", FakeSocket)
    assert lan._registration_body()["local_reg"]["ip"] == "10.0.1.113"
    sock, = sockets
    assert sock.connected == (MODULE, 80)      # UDP connect: routing only, no packet
    assert sock.kind == icomm_lan.socket.SOCK_DGRAM and sock.closed
    lan._registration_body()
    assert len(sockets) == 1                   # learned once


def test_registration_body_respects_relay_ip(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch, RELAY_IP="192.168.9.9")
    monkeypatch.setattr(icomm_lan.socket, "socket", None)   # must not be needed
    assert relay.lan._registration_body()["local_reg"]["ip"] == "192.168.9.9"


# -- endpoint error paths -------------------------------------------------------------

@pytest.mark.parametrize("body", [
    ValueError("not json"),          # unparsable
    [1, 2],                          # not an object
    {"key_exchange": "x"},           # not an object inside
    {"key_exchange": {**EXCHANGE, "random_1": None}},     # wrong type
    {"key_exchange": {k: v for k, v in EXCHANGE.items() if k != "random_1"}},
    {"key_exchange": {k: v for k, v in EXCHANGE.items() if k != "time_1"}},
])
async def test_malformed_key_exchange_is_400_and_keeps_the_session(tmp_path, body):
    lan = _server(tmp_path)
    live = lan.session
    response = await lan._key_exchange(_Json(body))
    assert response.status == 400
    assert lan.session is live and lan.session_verified is True
    assert lan.exchanges_since_update == 0


async def test_key_exchange_for_a_foreign_key_id_is_404(tmp_path, caplog):
    lan = _server(tmp_path)
    live = lan.session
    response = await lan._key_exchange(_Json({"key_exchange": {**EXCHANGE, "key_id": 7}}))
    assert response.status == 404
    assert lan.session is live
    assert "unknown key id 7" in caplog.text


async def test_key_exchange_starts_an_unverified_session_and_asks_for_telemetry(tmp_path):
    lan = _server(tmp_path)
    old = lan.session
    response = await lan._key_exchange(_Json({"key_exchange": EXCHANGE}))
    body = json.loads(response.body)
    assert lan.session is not old
    assert body == {"random_2": lan.session.random_2, "time_2": lan.session.time_2}
    assert lan.session_verified is False       # until an authentic update arrives
    assert lan.refresh_pending is True and lan.last_poll > 0


async def test_three_exchanges_arm_the_push_and_an_update_disarms_it(tmp_path):
    lan = _server(tmp_path)
    for _ in range(3):
        await lan._key_exchange(_Json({"key_exchange": EXCHANGE}))
    assert lan.needs_key_push is True
    reply = lan.relay._transform_commands(b"<commands><cmds/></commands>")
    assert reply is not None and b"<resource>lanip.json</resource>" in reply
    await lan._key_exchange(_Json({"key_exchange": EXCHANGE}))       # stays armed
    assert lan.needs_key_push is True and lan.exchanges_since_update == 4


async def test_no_session_means_400_for_polls_and_updates(tmp_path):
    lan = _server(tmp_path)
    lan.session = None
    lan.relay.queue_write("setpoint", 140)
    assert (await lan._commands(_Json({}))).status == 400
    assert (await lan._datapoint(_Json({"enc": "", "sign": ""}))).status == 400
    assert lan.relay.pending["setpoint"].attempts == 0      # nothing drained


@pytest.mark.parametrize("body", [
    {"enc": "AAAAAAAAAAAAAAAAAAAAAA==", "sign": "x"},     # one block, wrong signature
    {"sign": "x"},                                        # no ciphertext
    {"enc": "", "sign": "x"},                             # empty
    ValueError("not json"),
])
async def test_undecryptable_update_is_400_and_changes_nothing(tmp_path, body, caplog):
    lan = _server(tmp_path)
    lan.session_verified = False
    iv = lan.session.dev.iv
    response = await lan._datapoint(_Json(body))
    assert response.status == 400
    assert lan.updates == 0 and lan.session_verified is False
    assert lan.session.dev.iv == iv
    assert "undecryptable update" in caplog.text


def test_keepalive_reads_can_be_disabled(tmp_path):
    lan = _server(tmp_path)
    lan.refresh_interval = 0
    lan.last_update = 0.0
    assert lan._refresh_due() is False


# -- the key file ------------------------------------------------------------------------

def test_unparsable_key_file_is_replaced_with_a_fresh_key(tmp_path, monkeypatch):
    (tmp_path / "lanip.json").write_text("{not json")
    relay = _relay(tmp_path, monkeypatch)
    monkeypatch.delenv("LANIP_KEY")
    lan = LanServer(relay)
    saved = json.loads(lan.key_file.read_text())
    assert saved == {"lanip_key": lan.key, "lanip_key_id": 1}
    assert len(lan.key) == 28


def test_key_file_without_a_key_mints_a_fresh_one(tmp_path, monkeypatch):
    (tmp_path / "lanip.json").write_text(json.dumps({"lanip_key": "", "lanip_key_id": 9}))
    relay = _relay(tmp_path, monkeypatch)
    monkeypatch.delenv("LANIP_KEY")
    lan = LanServer(relay)
    assert lan.key and lan.key_id == 1          # the empty record's id is not trusted
    assert json.loads(lan.key_file.read_text())["lanip_key"] == lan.key


def test_saved_key_id_and_module_address_are_restored(tmp_path, monkeypatch):
    (tmp_path / "lanip.json").write_text(json.dumps(
        {"lanip_key": "k" * 28, "lanip_key_id": 5, "module_ip": "10.0.1.50"}))
    relay = _relay(tmp_path, monkeypatch)
    monkeypatch.delenv("LANIP_KEY")
    lan = LanServer(relay)
    assert (lan.key, lan.key_id, lan.module_ip) == ("k" * 28, 5, "10.0.1.50")


def test_dev_key_override_is_never_written(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)            # LANIP_KEY set
    lan = relay.lan
    assert lan.key == SECRET
    lan.learn_module_ip("<lan-ip>10.0.1.187</lan-ip>")
    assert lan.module_ip == "10.0.1.187"
    assert not lan.key_file.exists()


def test_unwritable_key_file_still_yields_a_working_key(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory should be")
    relay = _relay(tmp_path, monkeypatch)
    monkeypatch.delenv("LANIP_KEY")
    monkeypatch.setenv("LANIP_FILE", str(blocker / "lanip.json"))
    lan = LanServer(relay)
    assert len(lan.key) == 28
    assert "key not persisted" in caplog.text
    lan.learn_module_ip("<lan-ip>10.0.1.187</lan-ip>")       # _persist fails softly too
    assert lan.module_ip == "10.0.1.187"
    assert "could not persist module address" in caplog.text


def test_key_file_is_owner_only(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    monkeypatch.delenv("LANIP_KEY")
    lan = LanServer(relay)
    lan.learn_module_ip("<lan-ip>10.0.1.9</lan-ip>")
    path = lan.key_file
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["module_ip"] == "10.0.1.9"
