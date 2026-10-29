"""The module's stray "Content-Length: 0" after GET /ping.

From a decrypted capture (10-29 11:26): libada sends every request's headers
in two TLS records, the headers, then the last header line and the blank line
on their own. For GET /dsns and commands.xml that second record,
"Content-Length: 0\r\n\r\n", *completes* the request; for POSTs it is
"Transfer-Encoding: chunked\r\n\r\n" plus the body. Only GET /ping ends its
first record with a blank line already, so its second record is a stray:
aiohttp took it for the next request (BadHttpMethod, 400) and closed the
connection, and the module retried 16 s later.

0.7.10 dropped that record always, cutting the end off every normal request
(the heater's cloud side stayed down; reverted in 0.7.11). These tests replay
the real record sequences, so that mistake fails here.
"""

from __future__ import annotations

import asyncio
import json
import ssl

import pytest
from aiohttp import web
from icomm_hub import ModuleRunner, StrayHeaderGuard
from icomm_relay import Relay
from test_state_api import _server_context

HOST = b"Host: iCOMMNextGenHP-71538015-device.aylanetworks.com\r\n"
KEY = b"x-Ayla-auth-key: not-a-real-key\r\n"
STRAY = b"Content-Length: 0\r\n\r\n"
# The records exactly as the module sent them, auth key replaced.
PING = [b"GET /ping HTTP/1.1\r\n" + HOST + b"\r\n", STRAY]
DSNS = [b"GET /dsns/AC000W019949045.xml HTTP/1.1\r\n" + HOST + KEY, STRAY]
POLL = [b"GET /devices/29689890/commands.xml?signal=-45 HTTP/1.1\r\n" + HOST + KEY, STRAY]
POST = [b"POST /devices/29689890/properties/firmwareUpdateConsent/datapoints.xml HTTP/1.1\r\n"
        + HOST + KEY,
        b"Transfer-Encoding: chunked\r\n\r\n27\r\n<datapoint><value>1</value></datapoint>",
        b"\r\n0\r\n\r\n"]


async def _app_site(relay, ssl_context=None):
    app = web.Application()

    async def answer(request):
        body = await request.read()
        return web.Response(text=f"{request.method} {request.path} {len(body)}")
    app.router.add_route("*", "/{tail:.*}", answer)
    runner = ModuleRunner(app, relay=relay)
    assert runner.server is None                           # before setup
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_context)
    await site.start()
    return runner, site.port


async def _conversation(port, requests, ssl_context=None, split=True) -> list[bytes]:
    """Send each request's records (each its own write, or all at once), wait
    for its answer, then the next: as the module does."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ssl_context)
    answers = []
    try:
        for records in requests:
            for chunk in (records if split else [b"".join(records)]):
                writer.write(chunk)
                await writer.drain()
                await asyncio.sleep(0.03)               # a record (and a read) of its own
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            length = int(head.lower().split(b"content-length: ")[1].split(b"\r\n")[0])
            answers.append(head.split(b"\r\n")[0] + b" " + await reader.readexactly(length))
    except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
        answers.append(b"closed")
    finally:
        writer.close()
    return answers


@pytest.mark.parametrize("split", [True, False], ids=["records", "coalesced"])
async def test_the_reconnect_conversation_is_answered(split):
    relay = Relay()
    runner, port = await _app_site(relay)
    try:
        answers = await _conversation(port, [PING, DSNS, POLL, POST], split=split)
    finally:
        await runner.cleanup()
    assert answers == [
        b"HTTP/1.1 200 OK GET /ping 0",
        b"HTTP/1.1 200 OK GET /dsns/AC000W019949045.xml 0",
        b"HTTP/1.1 200 OK GET /devices/29689890/commands.xml 0",
        b"HTTP/1.1 200 OK POST /devices/29689890/properties/firmwareUpdateConsent/"
        b"datapoints.xml 39"]
    assert relay.stray_headers == 1
    state = json.loads((await relay.state_handler(None)).text)
    assert state["stray_headers"] == 1


async def test_normal_requests_keep_their_last_record():
    """The 0.7.10 bug: without /ping first, nothing is dropped."""
    relay = Relay()
    runner, port = await _app_site(relay)
    try:
        answers = await _conversation(port, [DSNS, POLL, DSNS])
    finally:
        await runner.cleanup()
    assert all(a.startswith(b"HTTP/1.1 200") for a in answers) and len(answers) == 3
    assert relay.stray_headers == 0


async def test_over_tls_as_the_module_does(tmp_path, monkeypatch, openssl_available):
    server_ctx = _server_context(tmp_path, monkeypatch)
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.check_hostname, client_ctx.verify_mode = False, ssl.CERT_NONE
    client_ctx.minimum_version = client_ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    client_ctx.set_ciphers("AES128-SHA")
    relay = Relay()
    runner, port = await _app_site(relay, server_ctx)
    try:
        answers = await _conversation(port, [PING, DSNS], client_ctx)
    finally:
        await runner.cleanup()
    assert [a.split(b" ")[1] for a in answers] == [b"200", b"200"]
    assert relay.stray_headers == 1


def test_the_guard_delegates_the_rest():
    calls = []

    class Inner(asyncio.Protocol):
        def connection_made(self, transport):
            calls.append("made")

        def data_received(self, data):
            calls.append(data)

        def eof_received(self):
            calls.append("eof")
            return True

        def connection_lost(self, exc):
            calls.append("lost")

        def pause_writing(self):
            calls.append("pause")

        def resume_writing(self):
            calls.append("resume")

    guard = StrayHeaderGuard(Inner(), Relay())
    guard.connection_made(None)
    guard.data_received(STRAY)                       # nothing before it: passed on
    guard.data_received(b"x\r\n\r\n")
    guard.data_received(STRAY)                       # after a request's end: dropped
    assert guard.eof_received() is True
    guard.pause_writing()
    guard.resume_writing()
    guard.connection_lost(None)
    assert calls == ["made", STRAY, b"x\r\n\r\n", "eof", "pause", "resume", "lost"]
