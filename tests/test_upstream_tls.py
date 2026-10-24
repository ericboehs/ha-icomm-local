"""Ayla's certificate failing verification is reported, not just counted."""

from __future__ import annotations

import json
import logging
import ssl

import aiohttp
from test_proxy import FakeClient, _relay, _request


def _cert_error(message: str = "unable to get local issuer certificate"):
    cause = ssl.SSLCertVerificationError(1, "certificate verify failed")
    cause.verify_message = message
    return aiohttp.ClientConnectorCertificateError(None, cause)


async def _state(relay) -> dict:
    return json.loads((await relay.state_handler(None)).body)["upstream_tls"]


async def test_a_certificate_failure_is_reported(tmp_path, monkeypatch, caplog):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(error=_cert_error())
    caplog.set_level(logging.WARNING)
    path = "/devices/1/properties/superBlockDyn1/datapoints.xml"
    reply = await relay._route(_request("POST", path, b"<datapoint/>"))
    assert reply.status == 200                   # the module still gets its synthetic reply
    tls = await _state(relay)
    assert tls["failing"] is True and tls["failures"] == 1
    assert tls["last_error"] == "unable to get local issuer certificate"
    assert "cloud sync is down" in caplog.text
    assert relay.upstream_errors == 1


async def test_the_warning_is_rate_limited(tmp_path, monkeypatch, caplog):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(error=_cert_error())
    caplog.set_level(logging.WARNING)
    for _ in range(3):
        await relay._route(_request("GET", "/devices/1/commands.xml"))
    assert caplog.text.count("cloud sync is down") == 1
    assert (await _state(relay))["failures"] == 3


async def test_a_verified_reply_clears_it(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(error=_cert_error())
    await relay._route(_request("GET", "/devices/1/commands.xml"))
    relay.client = FakeClient(200, b"<commands><cmds/></commands>")
    await relay._route(_request("GET", "/devices/1/commands.xml"))
    tls = await _state(relay)
    assert tls["failing"] is False and tls["failures"] == 1 and tls["last_ok"]


async def test_other_ssl_errors_count_and_plain_outages_do_not(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(error=ssl.SSLError(1, "wrong version number"))
    await relay._route(_request("GET", "/devices/1/commands.xml"))
    assert (await _state(relay))["last_error"] == "SSLError"
    relay.client = FakeClient(error=aiohttp.ClientConnectionError("down"))
    await relay._route(_request("GET", "/devices/1/commands.xml"))
    assert (await _state(relay))["failures"] == 1           # an outage is not a TLS failure


async def test_nothing_yet(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    assert (await _state(relay)) == {"failures": 0, "last_failure": None, "last_error": None,
                                     "last_ok": None, "failing": False}
