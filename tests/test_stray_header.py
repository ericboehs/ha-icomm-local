"""The module's stray "Content-Length: 0" after GET /ping.

Seen live at every reconnect (10-28 17:29, 10-29 07:39): the module sends
`GET /ping`, then a TLS record holding only `Content-Length: 0`. aiohttp took
it for the next request's method (BadHttpMethod), answered 400 and closed the
connection, losing the request queued behind it.
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

PING = b"GET /ping HTTP/1.1\r\nHost: ads-dev.aylanetworks.com\r\n\r\n"
DSNS = b"GET /dsns/AC000W019949045.xml HTTP/1.1\r\nHost: ads-dev.aylanetworks.com\r\n\r\n"


async def _app_site(relay, ssl_context=None):
    app = web.Application()

    async def ok(request):
        return web.Response(text=request.path)
    app.router.add_get("/{tail:.*}", ok)
    runner = ModuleRunner(app, relay=relay)
    assert runner.server is None                           # before setup
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_context)
    await site.start()
    return runner, site.port


async def _exchange(port, chunks, ssl_context=None) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ssl_context)
    for chunk in chunks:
        writer.write(chunk)
        await writer.drain()
        await asyncio.sleep(0.05)               # a record (and a read) of its own
    data = b""
    while data.count(b"HTTP/1.1 ") < 2:
        part = await asyncio.wait_for(reader.read(65536), 5)
        if not part:
            break
        data += part
    writer.close()
    return data


@pytest.mark.parametrize("stray", [b"Content-Length: 0\r\n", b"Content-Length: 0\r\n\r\n",
                                   b"Content-Length:0\r\n"])
async def test_the_request_behind_it_is_answered(stray):
    relay = Relay()
    runner, port = await _app_site(relay)
    try:
        data = await _exchange(port, [PING, stray, DSNS])
    finally:
        await runner.cleanup()
    assert data.count(b"HTTP/1.1 200") == 2 and b"400" not in data
    assert data.endswith(b"/dsns/AC000W019949045.xml")
    assert relay.stray_headers == 1
    state = json.loads((await relay.state_handler(None)).text)
    assert state["stray_headers"] == 1


async def test_over_tls_as_the_module_does(tmp_path, monkeypatch, openssl_available):
    server_ctx = _server_context(tmp_path, monkeypatch)
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.check_hostname, client_ctx.verify_mode = False, ssl.CERT_NONE
    client_ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    client_ctx.set_ciphers("AES128-SHA")
    relay = Relay()
    runner, port = await _app_site(relay, server_ctx)
    try:
        data = await _exchange(port, [PING, b"Content-Length: 0\r\n\r\n", DSNS], client_ctx)
    finally:
        await runner.cleanup()
    assert data.count(b"HTTP/1.1 200") == 2 and relay.stray_headers == 1


async def test_anything_else_goes_through():
    """A real request, a body, or the header with more after it: untouched."""
    relay = Relay()
    runner, port = await _app_site(relay)
    try:
        data = await _exchange(port, [PING, b"Content-Length: 0\r\nGET / HTTP/1.1\r\n\r\n"])
    finally:
        await runner.cleanup()
    assert b"400" in data and relay.stray_headers == 0     # aiohttp decides, as before


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
    guard.data_received(b"Content-Length: 0\r\n")
    guard.data_received(b"x")
    assert guard.eof_received() is True
    guard.pause_writing()
    guard.resume_writing()
    guard.connection_lost(None)
    assert calls == ["made", b"x", "eof", "pause", "resume", "lost"]
