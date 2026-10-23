"""The DSN rewrite and the lan.json answer: how the module gets armed.

The relay rewrites exactly two booleans in the module's registration reply --
<lan-enabled> and <local-access-enabled> -- and, when the agent then asks
GET /devices/<key>/lan.json (per libada's client.c), answers with our own key.
These tests pin the rewrite to byte-exactness and the answer to the shape the
agent's C parser requires.
"""

import json

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from relay.icomm_relay import Relay

DSN = """<?xml version="1.0" encoding="UTF-8"?>
<device>
  <product-name>NextGenHP</product-name>
  <lan-ip>10.0.1.187</lan-ip>
  <regtoken>abc123</regtoken>
  <lan-enabled>false</lan-enabled>
  <local-access-enabled>false</local-access-enabled>
  <registered>0</registered>
</device>
"""


def _relay(tmp_path, monkeypatch) -> Relay:
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setenv("LANIP_KEY", "a" * 28)
    return Relay()


def test_both_switches_flipped_and_nothing_else(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    out = relay._rewrite_dsn(DSN.encode())
    assert out is not None
    text = out.decode()
    assert "<lan-enabled>true</lan-enabled>" in text
    assert "<local-access-enabled>true</local-access-enabled>" in text
    # every other byte identical, in order
    assert text.replace("lan-enabled>true", "lan-enabled>false") \
               .replace("local-access-enabled>true", "local-access-enabled>false") == DSN
    assert "registered>0" in text                      # untouched
    assert (tmp_path / "dsn-original.xml").read_bytes() == DSN.encode()
    assert (tmp_path / "dsn-rewritten.xml").exists()
    assert relay.lan.module_ip == "10.0.1.187"         # learned for the callback


def test_reply_without_switches_passes_through(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    assert relay._rewrite_dsn(b"<device><registered>0</registered></device>") is None
    assert not (tmp_path / "dsn-original.xml").exists()


def test_one_switch_still_rewrites(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    out = relay._rewrite_dsn(b"<device><lan-enabled>false</lan-enabled></device>")
    assert out == b"<device><lan-enabled>true</lan-enabled></device>"


async def test_lan_json_answered_locally_with_our_key(tmp_path, monkeypatch):
    """libada's client_get_lanip_key asks GET /devices/<key>/lan.json when
    armed; the relay answers with a source-conformant payload (status exactly
    "enable", u16 id, 30 s keep-alive), never forwarding it upstream."""
    relay = _relay(tmp_path, monkeypatch)
    request = make_mocked_request("GET", "/devices/29689890/lan.json",
                                  app=web.Application())
    response = await relay._route(request)
    assert response.status == 200
    assert json.loads(response.body) == {"lanip": {
        "status": "enable", "lanip_key_id": 1, "lanip_key": "a" * 28,
        "keep_alive": 30, "auto_sync": 1}}
