"""Pre-publish review fixes (0.3.8): peer allowlist, CBC chain, queue, loop.

Each test is named for the attack or failure it pins down.
"""

import asyncio
import base64
import hmac
import json
import time
from unittest import mock

import aiohttp
import icomm_lan
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from icomm_lan import Session
from icomm_peers import PeerPolicy, default_gateways
from test_lan import EXCHANGE, SECRET, _server, module_directions

from relay.icomm_relay import MAX_ATTEMPTS, RETIRE_AFTER_S, Relay

GATEWAY, MODULE, STRANGER = "10.0.1.1", "10.0.1.187", "10.0.1.66"


def _body(data) -> aiohttp.StreamReader:
    reader = aiohttp.StreamReader(mock.Mock(_reading_paused=False), 2**16,
                                  loop=asyncio.get_running_loop())
    reader.feed_data(json.dumps(data).encode())
    reader.feed_eof()
    return reader


def _from(peer: str, method: str, path: str, **kw):
    transport = mock.Mock()
    transport.get_extra_info.side_effect = (
        lambda key, default=None: (peer, 40000) if key == "peername" else default)
    return make_mocked_request(method, path, transport=transport, **kw)


def _relay(tmp_path, monkeypatch, **env) -> Relay:
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setenv("LANIP_KEY", SECRET)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    relay = Relay()
    relay.peers.gateways = {GATEWAY}
    relay.lan.module_ip = MODULE
    return relay


# -- S1/S2: the impersonated cloud answers only the module -------------------

async def test_lan_json_key_is_never_given_to_a_lan_stranger(tmp_path, monkeypatch):
    """A stranger on :8444 gets plain proxying to Ayla, never our key."""
    relay = _relay(tmp_path, monkeypatch)
    proxied = []

    async def fake_proxy(request, transform=None):
        proxied.append((request.path, transform))
        return web.Response(status=401)

    relay.proxy = fake_proxy
    check = relay.peers.middleware(relay.peers.cloud_allowed, ":8444", refuse=False)
    response = await check(_from(STRANGER, "GET", "/devices/x/lan.json"), relay._route)
    assert response.status == 401                       # Ayla's answer, not ours
    assert proxied == [("/devices/x/lan.json", None)]   # no local answer, no transform
    assert relay.lan.key_served_at == 0.0
    assert relay.peers.rejected == {STRANGER: 1}


async def test_stranger_telemetry_and_polls_get_no_local_behavior(tmp_path, monkeypatch,
                                                                  blocks_electric):
    relay = _relay(tmp_path, monkeypatch)
    relay.queue_write("setpoint", 140)
    transforms = []

    async def fake_proxy(request, transform=None):
        transforms.append(transform)
        return web.Response()

    relay.proxy = fake_proxy
    check = relay.peers.middleware(relay.peers.cloud_allowed, ":8444", refuse=False)
    for path in ("/devices/x/commands.xml", "/devices/x/properties/superBlockDyn1/datapoints.xml",
                 "/dsns/x.xml"):
        await check(_from(STRANGER, "GET", path), relay._route)
    assert transforms == [None, None, None]
    assert relay.state == {"online": False}
    assert relay.pending["setpoint"].attempts == 0


async def test_lan_json_still_served_to_the_module_through_the_gateway(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    check = relay.peers.middleware(relay.peers.cloud_allowed, ":8444", refuse=False)
    for peer in (GATEWAY, MODULE):
        response = await check(_from(peer, "GET", "/devices/x/lan.json"), relay._route)
        assert response.status == 200
        assert json.loads(response.body)["lanip"]["lanip_key"] == SECRET


async def test_stranger_cannot_fake_the_lanip_ack(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.lan.needs_key_push = True

    async def fake_proxy(request, transform=None):
        return web.Response(status=404)

    relay.proxy = fake_proxy
    check = relay.peers.middleware(relay.peers.cloud_allowed, ":8444", refuse=False)
    await check(_from(STRANGER, "PUT", "/devices/x/lanip.json?cmd_id=1&status=204"),
                relay._route)
    assert relay.lan.needs_key_push is True
    assert relay.lanip_push_acked is False


def test_trusted_peers_extends_and_star_disables(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch, TRUSTED_PEERS="10.0.5.9, 10.0.5.10")
    assert relay.peers.cloud_allowed("10.0.5.9") and relay.peers.lan_allowed("10.0.5.10")
    assert not relay.peers.cloud_allowed(STRANGER)
    assert relay.peers.cloud_allowed("::ffff:10.0.1.1")       # IPv4-mapped gateway
    monkeypatch.setenv("TRUSTED_PEERS", "*")
    assert PeerPolicy(lambda: None).cloud_allowed(STRANGER)


def test_default_gateway_read_from_the_kernel_table(tmp_path):
    table = tmp_path / "route"
    table.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "enp0s18\t00000000\t0101000A\t0003\t0\t0\t100\t00000000\n"
        "enp0s18\t0000000A\t00000000\t0001\t0\t0\t100\t00FFFFFF\n")
    assert default_gateways(str(table)) == {"10.0.1.1"}
    assert default_gateways(str(tmp_path / "missing")) == set()


# -- S3/S4: Local Connect answers only the module ----------------------------

async def test_stranger_cannot_reset_the_session_or_drain_writes(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    lan = relay.lan
    lan.session = live = Session(SECRET.encode(), EXCHANGE)
    relay.queue_write("setpoint", 140)
    check = relay.peers.middleware(relay.peers.lan_allowed, ":8888")
    exchange = _from(STRANGER, "POST", "/local_lan/key_exchange.json",
                     payload=_body({"key_exchange": EXCHANGE}))
    assert (await check(exchange, lan._key_exchange)).status == 403
    poll = _from(STRANGER, "GET", "/local_lan/commands.json")
    assert (await check(poll, lan._commands)).status == 403
    assert lan.session is live
    assert relay.pending["setpoint"].attempts == 0
    # The gateway is not a Local Connect peer: the module calls back directly.
    assert not relay.peers.lan_allowed(GATEWAY)
    assert relay.peers.lan_allowed(MODULE)


# -- S5: authenticate before advancing the CBC chain -------------------------

def _module_message(tx, payload: dict) -> dict:
    text = json.dumps(payload).encode()
    padded = text.ljust((len(text) + 15) // 16 * 16, bytes([0]))
    return {"enc": base64.b64encode(tx["cipher"].encrypt(padded)).decode(),
            "sign": base64.b64encode(hmac.digest(tx["sign"], text, "sha256")).decode()}


def test_garbage_message_does_not_desync_the_session():
    session = Session(SECRET.encode(), EXCHANGE)
    tx, _ = module_directions(SECRET.encode(), EXCHANGE, session.random_2, session.time_2)
    garbage = {"enc": base64.b64encode(bytes(range(32))).decode(), "sign": "AAAA"}
    for bad in (garbage, {"enc": "AAAA", "sign": "x"}):
        with pytest.raises(ValueError):
            session.dev.decrypt_validate(bad)
    # The module's real messages, chained from the session IV, still verify.
    for seq in (1, 2, 3):
        payload = {"seq_no": seq, "data": {"name": "superBlockDyn1", "value": "x" * seq}}
        assert session.dev.decrypt_validate(_module_message(tx, payload)) == payload


def test_chain_continues_across_our_own_replies():
    session = Session(SECRET.encode(), EXCHANGE)
    _, rx = module_directions(SECRET.encode(), EXCHANGE, session.random_2, session.time_2)
    for seq in (1, 2, 3):
        ours = session.app.encrypt_sign({"seq_no": seq, "data": {}})
        plain = rx["cipher"].decrypt(base64.b64decode(ours["enc"])).rstrip(bytes([0]))
        assert json.loads(plain) == {"seq_no": seq, "data": {}}


# -- S6/S7: queue order, caps, retirement ------------------------------------

def test_stuck_mode_write_does_not_block_a_setpoint(tmp_path):
    relay = _server(tmp_path).relay
    mode = relay.queue_write("mode", "hybrid")
    mode.attempts = MAX_ATTEMPTS
    mode.delivered = time.time()
    setpoint = relay.queue_write("setpoint", 140)
    assert relay.take_pending_for_lan() is setpoint


def test_oldest_write_goes_first_not_alphabetical(tmp_path):
    relay = _server(tmp_path).relay
    setpoint = relay.queue_write("setpoint", 140)
    setpoint.queued -= 10
    relay.queue_write("mode", "hybrid")
    assert relay.take_pending_for_lan() is setpoint


def test_cloud_injection_is_capped(tmp_path, blocks_electric):
    relay = _server(tmp_path).relay
    relay._ingest_blocks(blocks_electric)
    relay.health = lambda: {"state": "healthy"}
    relay.queue_write("setpoint", 140)
    reply = b"<commands>\n  <properties/>\n  <schedules/>\n  <cmds/>\n</commands>\n"
    write = relay.pending["setpoint"]
    assert relay._inject(reply) is not None
    assert relay._inject(reply) is None                 # inside the 60 s retry window
    injected = []
    for _ in range(MAX_ATTEMPTS + 1):
        write.delivered = 0.0                           # pretend the window passed
        injected.append(relay._inject(reply) is not None)
    assert injected == [True] * (MAX_ATTEMPTS - 1) + [False, False]


def test_exhausted_write_is_retired_after_the_window(tmp_path):
    relay = _server(tmp_path).relay
    relay.state["setpoint"] = 135.0
    write = relay.queue_write("setpoint", 138)
    write.attempts, write.acked, write.ack_status, write.reports_after_ack = 3, 1.0, 200, 2
    write.delivered = time.time() - RETIRE_AFTER_S + 60
    relay._expire_pending()
    assert "setpoint" in relay.pending                      # still visible, can still confirm
    write.delivered = time.time() - RETIRE_AFTER_S - 1
    relay._expire_pending()
    assert relay.pending == {}
    assert relay.last_write["outcome"] == "not_applied"


def test_cloud_only_exhaustion_retires_as_unconfirmed(tmp_path):
    relay = _server(tmp_path).relay
    write = relay.queue_write("setpoint", 141)
    write.attempts, write.delivered = 3, time.time() - RETIRE_AFTER_S - 1
    relay._expire_pending()
    assert relay.last_write["outcome"] == "unconfirmed"


def test_old_write_expires_in_memory(tmp_path):
    relay = _server(tmp_path).relay
    write = relay.queue_write("setpoint", 141)
    write.queued -= relay.queue_max_age_s + 1
    assert relay.take_pending_for_lan() is None
    assert relay.last_write["outcome"] == "expired"


def test_error_ack_reads_as_rejected(tmp_path):
    relay = _server(tmp_path).relay
    write = relay.queue_write("setpoint", 141)
    write.command_id = "abc"
    relay._record_ack("abc", 400)
    assert write.status == "rejected"


# -- S8: the heartbeat loop survives any exception ---------------------------

async def test_registration_loop_survives_a_non_oserror(tmp_path, monkeypatch):
    lan = _server(tmp_path)
    lan.module_ip = MODULE
    lan.advertise_ip = "10.0.1.113"
    lan._client = mock.Mock()
    lan._client.request.side_effect = aiohttp.ServerDisconnectedError()
    sleeps = 0

    async def fake_sleep(_delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(icomm_lan.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await lan._registration_loop()
    assert lan.registration_failures == 3
    assert lan.last_registration_result == "ServerDisconnectedError"


# -- input validation ----------------------------------------------------------

async def test_nudge_is_clamped_and_bad_bodies_are_400(tmp_path):
    relay = _server(tmp_path).relay

    def post(path, body):
        return make_mocked_request("POST", path, app=web.Application(), payload=_body(body))

    response = await relay.nudge_handler(post("/nudge", {"attempts": 10**9}))
    assert json.loads(response.body)["nudges_left"] == 3
    assert (await relay.nudge_handler(post("/nudge", {"attempts": "x"}))).status == 400
    assert (await relay.write_handler(post("/write", [140]))).status == 400
    assert (await relay.write_handler(post("/write", {"setpoint": None}))).status == 400


# -- second review ---------------------------------------------------------------

def test_foreign_key_session_gets_no_lan_writes_so_cloud_keeps_its_attempts(tmp_path,
                                                                           blocks_electric):
    """A module holding a foreign key completes exchanges but never sends an
    authentic update. LAN copies into it vanish, so none are sent."""
    lan = _server(tmp_path)
    relay = lan.relay
    lan.session_verified = False
    relay.queue_write("setpoint", 140)
    assert relay.take_pending_for_lan() is None
    assert relay.pending["setpoint"].attempts == 0
    relay._ingest_blocks(blocks_electric)
    relay.health = lambda: {"state": "healthy"}
    reply = b"<commands>\n  <properties/>\n  <schedules/>\n  <cmds/>\n</commands>\n"
    assert relay._inject(reply) is not None             # cloud fallback still delivers


async def test_authentic_update_verifies_the_session(tmp_path):
    lan = _server(tmp_path)
    lan.session_verified = False
    tx, _ = module_directions(SECRET.encode(), EXCHANGE, lan.session.random_2,
                              lan.session.time_2)
    message = _module_message(tx, {"seq_no": 1, "data": {"name": "x", "value": "1"}})
    request = _from(MODULE, "POST", "/local_lan/property/datapoint.json", payload=_body(message))
    assert (await lan._datapoint(request)).status == 200
    assert lan.session_verified is True


def test_rejected_write_is_not_retried(tmp_path):
    relay = _server(tmp_path).relay
    write = relay.queue_write("setpoint", 141)
    write.command_id = "abc"
    write.attempts, write.delivered = 1, 0.0
    relay._record_ack("abc", "400")                     # strings normalised
    assert write.ack_status == 400 and write.status == "rejected"
    assert relay.take_pending_for_lan() is None


def test_retired_write_confirmed_late_is_recorded(tmp_path):
    relay = _server(tmp_path).relay
    relay.last_write = {"kind": "setpoint", "value": 138.0, "outcome": "not_applied"}
    relay.state["setpoint"] = 138.0
    relay._confirm_pending()
    assert relay.last_write["outcome"] == "confirmed"
    assert relay.last_write["earlier_outcome"] == "not_applied"


def test_gateways_reread_when_an_unknown_peer_arrives(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.peers.gateways = set()
    relay.peers._gateways_read = 0.0
    monkeypatch.setattr("icomm_peers.default_gateways", lambda: {GATEWAY})
    assert relay.peers.cloud_allowed(GATEWAY)
