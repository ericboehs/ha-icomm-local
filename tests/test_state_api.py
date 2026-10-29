"""The HA-facing state API and the TLS recipe the appliance actually accepts."""

from __future__ import annotations

import ssl
import subprocess
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from icomm_relay import Relay, decode


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch):
    """The state API alone -- no TLS, no upstream, no ports to collide with."""
    relay = Relay()
    app = __import__("aiohttp").web.Application()
    app.router.add_get("/state", relay.state_handler)
    app.router.add_get("/health", relay.health_handler)
    async with TestClient(TestServer(app)) as test_client:
        yield relay, test_client


async def test_state_before_any_telemetry(client) -> None:
    """A relay that has never been dialed must not look like a healthy tank."""
    _, http = client
    body = await (await http.get("/state")).json()
    assert body["online"] is False
    assert body["fresh"] is False
    assert body["last_telemetry_age"] is None


async def test_state_after_telemetry(client, blocks_electric: list[bytes]) -> None:
    relay, http = client
    relay.state = decode(blocks_electric)
    relay.state["online"] = True
    relay.last_telemetry = 1_000_000.0

    body = await (await http.get("/state")).json()
    assert body["online"] is True
    assert body["mode"] == "electric"
    assert body["watts"] == 4500
    assert "last_telemetry_age" in body


async def test_freshness_expires(client, blocks_electric: list[bytes], monkeypatch) -> None:
    """A quiet module is stale, not broken: freshness is a separate signal.

    The appliance is event-driven and goes silent for long stretches when the tank
    is satisfied, so `online` (it has been seen) and `fresh` (recently) differ --
    the integration uses `fresh` for availability.
    """
    relay, http = client
    relay.state = decode(blocks_electric)
    relay.state["online"] = True

    relay.last_telemetry = 1000.0
    monkeypatch.setattr("time.time", lambda: 1000.0 + 60)
    assert (await (await http.get("/state")).json())["fresh"] is True

    monkeypatch.setattr("time.time", lambda: 1000.0 + 601)
    assert (await (await http.get("/state")).json())["fresh"] is False


async def test_health_reports_upstream(client) -> None:
    _, http = client
    body = await (await http.get("/health")).json()
    assert body["ok"] is True
    assert body["upstream"].endswith(".aylanetworks.com")


def _leaf(tmp_path) -> tuple[str, str]:
    key, cert = tmp_path / "leaf.key", tmp_path / "leaf.crt"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "req", "-new", "-x509", "-sha256", "-days", "1",
                    "-key", str(key), "-subj", "/CN=whatever", "-out", str(cert)],
                   check=True, capture_output=True)
    return str(cert), str(key)


def _server_context(tmp_path, monkeypatch) -> ssl.SSLContext:
    cert, key = _leaf(tmp_path)
    monkeypatch.setenv("RELAY_CERT", cert)
    monkeypatch.setenv("RELAY_KEY", key)
    # Real 2048-bit parameters (public, not secret) -- generating them per test
    # would cost ~30 s, and DHE suites need them to complete a handshake.
    monkeypatch.setenv("RELAY_DHPARAMS", str(Path(__file__).parent / "fixtures" / "dhparams.pem"))
    return Relay().tls_context()


def test_tls_recipe_is_what_the_module_accepts(
        tmp_path, monkeypatch, openssl_available: None) -> None:
    """The handshake recipe, asserted: RSA-2048/SHA-256 leaf, TLS 1.2 max, CBC suites.

    These are measured constraints, not preferences. An ECDSA/P-384 leaf makes the
    module's handshake deadlock silently for 30 s instead of failing, and TLS 1.3
    (which the module does not speak) must not be offered.
    """
    ctx = _server_context(tmp_path, monkeypatch)
    assert ctx.maximum_version == ssl.TLSVersion.TLSv1_2
    enabled = {cipher["name"] for cipher in ctx.get_ciphers()}
    # The module offers four CBC suites; these three are the usable set.
    assert {"DHE-RSA-AES256-SHA", "AES128-SHA", "AES256-SHA"} <= enabled


async def test_handshake_succeeds_with_the_modules_own_capabilities(
        tmp_path, monkeypatch, openssl_available: None) -> None:
    """A real handshake: TLS 1.2, one DHE-RSA CBC suite, no SNI, no verification.

    This is precisely what the appliance offers -- a 2013-era ClientHello -- and it
    is the only proof that the recipe works rather than merely looks configured.
    """
    server_ctx = _server_context(tmp_path, monkeypatch)

    async def serve(reader, writer):
        writer.write(b"ok")
        await writer.drain()
        writer.close()

    server = await __import__("asyncio").start_server(serve, "127.0.0.1", 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_NONE
    client_ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    client_ctx.set_ciphers("DHE-RSA-AES256-SHA")
    try:
        reader, writer = await __import__("asyncio").open_connection(
            "127.0.0.1", port, ssl=client_ctx, server_hostname=None)
        assert await reader.readexactly(2) == b"ok"
        assert writer.get_extra_info("cipher")[0] == "DHE-RSA-AES256-SHA"
        assert writer.get_extra_info("ssl_object").version() == "TLSv1.2"
        writer.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_tls13_is_refused(tmp_path, monkeypatch, openssl_available: None) -> None:
    """TLS 1.3 must fail: the module cannot speak it, and a silent mismatch is worse."""
    server_ctx = _server_context(tmp_path, monkeypatch)

    async def serve(reader, writer):
        writer.close()

    server = await __import__("asyncio").start_server(serve, "127.0.0.1", 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_NONE
    client_ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    try:
        # Either a clean protocol alert or an immediate reset is acceptable; what
        # must not happen is a TLS 1.3 session.
        with pytest.raises((ssl.SSLError, ConnectionResetError)):
            await __import__("asyncio").open_connection("127.0.0.1", port, ssl=client_ctx)
    finally:
        server.close()
        await server.wait_closed()


def test_missing_certificates_fail_loudly(monkeypatch, tmp_path) -> None:
    """A missing key must abort at startup, not serve a broken handshake."""
    monkeypatch.setenv("RELAY_CERT", str(tmp_path / "absent.crt"))
    monkeypatch.setenv("RELAY_KEY", str(tmp_path / "absent.key"))
    with pytest.raises(SystemExit):
        Relay().tls_context()


@pytest.mark.parametrize("setting", ["1", "custom"])
def test_tls_keylog_writes_session_keys(tmp_path, monkeypatch, openssl_available, setting,
                                        caplog) -> None:
    """debug_tls_keylog: the keys of a real handshake land in a 0600 file."""
    import socket
    import threading
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    path = tmp_path / ("tls-keylog.txt" if setting == "1" else "elsewhere.txt")
    monkeypatch.setenv("TLS_KEYLOG", setting if setting == "1" else str(path))
    server_ctx = _server_context(tmp_path, monkeypatch)
    assert "TLS key log ON" in caplog.text
    assert path.exists() and path.stat().st_mode & 0o777 == 0o600
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.check_hostname, client_ctx.verify_mode = False, ssl.CERT_NONE
    client_ctx.minimum_version = client_ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    client_ctx.set_ciphers("AES128-SHA")
    left, right = socket.socketpair()

    def serve():
        with server_ctx.wrap_socket(right, server_side=True) as tls:
            tls.recv(1)
    thread = threading.Thread(target=serve)
    thread.start()
    with client_ctx.wrap_socket(left) as tls:
        tls.sendall(b"x")
    thread.join(5)
    assert "\nCLIENT_RANDOM " in path.read_text()


def test_tls_keylog_off_and_unwritable(tmp_path, monkeypatch, openssl_available, caplog):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TLS_KEYLOG", "0")
    assert _server_context(tmp_path, monkeypatch).keylog_filename is None
    monkeypatch.setenv("TLS_KEYLOG", str(tmp_path / "missing" / "keys.txt"))
    assert _server_context(tmp_path, monkeypatch).keylog_filename is None
    assert "TLS key log: can't write" in caplog.text
