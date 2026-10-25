"""DNS steering without module_ip: the module's own address is pinned.

With NAT steering the module reaches the relay from the gateway, which is
trusted. With DNS steering it arrives from its own address, which the relay
may not know yet. That peer is pinned only when Ayla accepts its config fetch
(GET /dsns/<DSN>.xml -> 200), because Ayla checks the device's credentials
and the relay forwards them untouched. Until then it gets plain proxying: no
lan.json key, no rewrite.
"""

from __future__ import annotations

import json

from test_dsn import DSN
from test_proxy import FakeClient, _relay, _request

from relay.icomm_relay import Relay

HEATER = "10.0.20.187"
DSN_PATH = "/dsns/AC000W019949045.xml"


def _from(request, remote: str):
    return request.clone(remote=remote)


async def _fetch_dsn(relay, remote=HEATER, status=200, path=DSN_PATH):
    relay.client = FakeClient(status, DSN.encode())
    return await relay._route(_from(_request("GET", path, trusted=False), remote))


async def test_an_accepted_config_fetch_pins_the_module(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    reply = await _fetch_dsn(relay)
    assert b"<lan-enabled>true</lan-enabled>" in reply.body       # rewritten
    assert relay.lan.module_ip == HEATER
    assert relay.lan.status()["module_ip_source"] == "observed"
    assert relay.peers.cloud_allowed(HEATER) and relay.peers.lan_allowed(HEATER)


async def test_ayla_refusing_it_pins_nothing(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    reply = await _fetch_dsn(relay, status=401)
    assert reply.status == 401 and relay.lan.module_ip is None
    assert not relay.peers.cloud_allowed(HEATER)


async def test_only_private_non_gateway_ipv4_peers(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.peers.gateways = {"10.0.1.1"}
    for remote in ("8.8.8.8", "10.0.1.1", "fd00::5", "127.0.0.1"):
        reply = await _fetch_dsn(relay, remote=remote)
        assert b"<lan-enabled>false</lan-enabled>" in reply.body, remote
    assert relay.lan.module_ip is None


async def test_other_paths_and_methods_never_pin(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, b"{}")
    await relay._route(_from(_request("GET", "/devices/1/lan.json", trusted=False), HEATER))
    await relay._route(_from(_request("POST", DSN_PATH, trusted=False), HEATER))
    await _fetch_dsn(relay, path="/dsns/../../x.xml")
    assert relay.lan.module_ip is None


async def test_a_configured_module_ip_is_never_overridden(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch, MODULE_IP="10.0.1.50")
    reply = await _fetch_dsn(relay)
    assert b"<lan-enabled>false</lan-enabled>" in reply.body
    assert relay.lan.module_ip == "10.0.1.50"


async def test_a_different_device_cannot_take_over(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    await _fetch_dsn(relay)
    reply = await _fetch_dsn(relay, remote="10.0.20.66", path="/dsns/AC000W000000001.xml")
    assert b"<lan-enabled>false</lan-enabled>" in reply.body
    assert relay.lan.module_ip == HEATER


async def test_the_same_device_moving_is_followed(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    await _fetch_dsn(relay)
    await _fetch_dsn(relay, remote="10.0.20.150", path=DSN_PATH.lower())
    assert relay.lan.module_ip == "10.0.20.150"
    assert not relay.peers.cloud_allowed(HEATER)


async def test_device_dsn_option_is_enforced_from_the_start(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch, DEVICE_DSN="AC000W000000001")
    await _fetch_dsn(relay)
    assert relay.lan.module_ip is None


async def test_ayla_stale_lan_ip_does_not_override_the_observed_one(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    await _fetch_dsn(relay)               # DSN says <lan-ip>10.0.1.187</lan-ip>
    assert relay.lan.module_ip == HEATER


async def test_lan_mode_off_never_pins(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch, LAN_MODE="0")
    await _fetch_dsn(relay)
    assert relay.lan.module_ip is None


async def test_the_pin_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setenv("UPSTREAM", "ayla.test")
    monkeypatch.delenv("LANIP_KEY", raising=False)
    relay = Relay()                                        # mints and saves a key
    await _fetch_dsn(relay)
    saved = json.loads((tmp_path / "lanip.json").read_text())
    assert saved["module_ip"] == HEATER and saved["module_ip_observed"] is True
    assert saved["module_dsn"] == "AC000W019949045"
    again = Relay()
    assert again.lan.module_ip == HEATER and again.lan.module_ip_observed
    assert again.peers.cloud_allowed(HEATER)


async def test_refetching_from_the_pinned_address_changes_nothing(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    await _fetch_dsn(relay)
    relay.lan._persist = lambda: (_ for _ in ()).throw(AssertionError("rewrote the pin"))
    reply = await _fetch_dsn(relay)
    assert b"<lan-enabled>true</lan-enabled>" in reply.body


async def test_no_peer_address_never_pins(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, DSN.encode())
    await relay._route(_request("GET", DSN_PATH, trusted=False))   # remote unknown
    assert relay.lan.module_ip is None


def test_pin_module_refuses_when_configured(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch, MODULE_IP="10.0.1.50")
    assert relay.lan.pin_module(HEATER, "AC000W019949045") is False


class _Transport:
    def __init__(self, port):
        self.port = port

    def get_extra_info(self, name, default=None):
        if name == "sockname":
            return ("10.0.1.113", self.port) if self.port else None
        return default

    def is_closing(self):
        return False


def _arriving(request, remote, port):
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    clone = make_mocked_request(request.method, request.path, app=web.Application(),
                                transport=_Transport(port))
    return clone.clone(remote=remote)


async def test_the_first_config_fetch_is_awaited_not_warned(tmp_path, monkeypatch, caplog):
    relay = _relay(tmp_path, monkeypatch)
    caplog.set_level("INFO")
    request = _arriving(_request("GET", DSN_PATH), HEATER, 443)
    relay.peers.note_untrusted(request, ":8444", "plain-proxying")
    assert "waiting for Ayla to confirm the module" in caplog.text and "on :443" in caplog.text
    assert relay.peers.awaiting_pin == 1 and relay.peers.rejected == {}
    assert "WARNING" not in caplog.text


async def test_other_untrusted_requests_warn_with_the_real_port(tmp_path, monkeypatch, caplog):
    relay = _relay(tmp_path, monkeypatch)
    request = _arriving(_request("GET", "/devices/1/commands.xml"), HEATER, 443)
    relay.peers.note_untrusted(request, ":8444", "plain-proxying")
    assert "on :443" in caplog.text and "power-cycle it so Ayla can confirm it" in caplog.text
    assert relay.peers.rejected == {HEATER: 1}
    no_socket = _arriving(_request("GET", "/devices/1/commands.xml"), "10.0.20.9", None)
    relay.peers.note_untrusted(no_socket, ":8444", "plain-proxying")
    assert "on :8444" in caplog.text                     # no socket info: the label
