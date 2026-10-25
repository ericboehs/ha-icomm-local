"""The built-in DNS server (dns_server): steer the heater's name, forward the rest."""

from __future__ import annotations

import asyncio
import json
import socket
import struct

import icomm_dns
import pytest
from aiohttp import web
from icomm_dns import DnsServer, answer, parse_question, servfail
from test_proxy import FakeSite, _relay

HEATER_NAME = "iCOMMNextGenHP-71538015-device.aylanetworks.com"


def query(name: str, qtype: int = 1, txid: int = 0x1234, flags: int = 0x0100) -> bytes:
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split("."))
    return struct.pack(">HHHHHH", txid, flags, 1, 0, 0, 0) + labels + b"\0" + struct.pack(
        ">HH", qtype, 1)


def answers(reply: bytes) -> tuple[int, int, str | None]:
    """(rcode, answer count, the A record's address if any)."""
    rcode, ancount = reply[3] & 0x0F, struct.unpack(">H", reply[6:8])[0]
    address = socket.inet_ntoa(reply[-4:]) if ancount else None
    return rcode, ancount, address


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeUpstream(asyncio.DatagramProtocol):
    """Answers every query with 203.0.113.9, or stays silent."""

    def __init__(self, silent=False):
        self.silent, self.seen = silent, []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.seen.append(data)
        if not self.silent:
            _, _, _, end = parse_question(data)
            self.transport.sendto(answer(data, end, "203.0.113.9"), addr)


async def upstream(silent=False):
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(
        lambda: FakeUpstream(silent), local_addr=("127.0.0.1", 0))
    return transport, proto, transport.get_extra_info("sockname")[1]


def server(monkeypatch, **env) -> DnsServer:
    for key in ("DNS_SERVER", "DNS_PORT", "DNS_UPSTREAM", "DNS_ANSWER", "DNS_REWRITE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return DnsServer()


# -- packets ------------------------------------------------------------------------
def test_parse_question_and_rejects():
    name, qtype, qclass, end = parse_question(query(HEATER_NAME))
    assert (name, qtype, qclass) == (HEATER_NAME.lower(), 1, 1)
    assert end == len(query(HEATER_NAME))
    good = query("a.example")
    assert parse_question(good[:11]) is None                         # short header
    assert parse_question(query("a.example", flags=0x8100)) is None  # a response
    assert parse_question(good[:4] + b"\0\0" + good[6:]) is None      # no question
    assert parse_question(good[:12] + b"\xc0\x0c" + good[14:]) is None  # pointer
    assert parse_question(good[:12] + b"\x3f") is None                # label overruns
    assert parse_question(good[:12] + b"\x01a") is None                # no terminator
    assert parse_question(good[:-2]) is None                          # no qclass


def test_answer_and_servfail_shapes():
    q = query(HEATER_NAME)
    end = parse_question(q)[3]
    rcode, count, address = answers(answer(q, end, "10.0.1.113"))
    assert (rcode, count, address) == (0, 1, "10.0.1.113")
    assert answer(q, end, None)[2] & 0x80 and answers(answer(q, end, None))[:2] == (0, 0)
    assert servfail(q)[3] & 0x0F == 2 and servfail(q)[:2] == q[:2]
    assert servfail(b"x") == b""


# -- resolve --------------------------------------------------------------------
async def test_the_heater_name_is_steered_case_insensitively(monkeypatch, caplog):
    dns = server(monkeypatch, DNS_ANSWER="10.0.1.113")
    reply = await dns.resolve(query(HEATER_NAME), "10.0.20.187")
    assert answers(reply) == (0, 1, "10.0.1.113")
    await dns.resolve(query(HEATER_NAME.lower()), "10.0.20.187")
    assert dns.rewritten == 2 and dns.last_rewrite["client"] == "10.0.20.187"
    assert caplog.text.count("DNS: steering") <= 2


async def test_the_answer_defaults_to_our_address_toward_the_client(monkeypatch):
    dns = server(monkeypatch)
    reply = await dns.resolve(query(HEATER_NAME), "127.0.0.1")
    assert answers(reply) == (0, 1, "127.0.0.1")


async def test_ipv6_for_the_heater_name_gets_no_records(monkeypatch):
    dns = server(monkeypatch, DNS_ANSWER="10.0.1.113")
    assert answers(await dns.resolve(query(HEATER_NAME, qtype=28), "10.0.20.187"))[:2] == (0, 0)


async def test_everything_else_is_forwarded_byte_for_byte(monkeypatch):
    transport, proto, port = await upstream()
    try:
        dns = server(monkeypatch, DNS_UPSTREAM=f"127.0.0.1:{port}")
        for name, qtype in (("ans-field.aylanetworks.com", 1),
                            ("proxy-ingress-usfield.aylanetworks.com", 1),
                            ("othermodel-12345678-device.aylanetworks.com", 1),
                            (HEATER_NAME, 16)):                    # TXT: not ours
            q = query(name, qtype)
            assert answers(await dns.resolve(q, "10.0.1.50"))[2] == "203.0.113.9"
            assert proto.seen[-1] == q
        assert dns.forwarded == 4 and dns.rewritten == 0
    finally:
        transport.close()


async def test_malformed_queries_are_forwarded_too(monkeypatch):
    transport, proto, port = await upstream(silent=True)
    try:
        dns = server(monkeypatch, DNS_UPSTREAM=f"127.0.0.1:{port}")
        monkeypatch.setattr(icomm_dns, "FORWARD_TIMEOUT", 0.05)
        reply = await dns.resolve(b"\x12\x34" + b"\0" * 10, "10.0.1.50")
        assert reply[3] & 0x0F == 2 and dns.failures == 1        # upstream silent: SERVFAIL
        assert "TimeoutError" in dns.last_error
    finally:
        transport.close()


async def test_no_upstream_at_all_is_servfail(monkeypatch):
    dns = server(monkeypatch)
    monkeypatch.setattr(icomm_dns, "default_gateways", lambda: set())
    reply = await dns.resolve(query("example.com"), "10.0.1.50")
    assert reply[3] & 0x0F == 2 and "no upstream" in dns.last_error
    assert dns.status()["upstream"] is None


def test_upstream_defaults_to_the_gateway_and_accepts_a_port(monkeypatch):
    monkeypatch.setattr(icomm_dns, "default_gateways", lambda: {"10.0.1.1"})
    assert server(monkeypatch)._upstream_endpoint() == ("10.0.1.1", 53)
    assert server(monkeypatch, DNS_UPSTREAM="9.9.9.9")._upstream_endpoint() == ("9.9.9.9", 53)
    assert server(monkeypatch, DNS_UPSTREAM="9.9.9.9:5353")._upstream_endpoint() == (
        "9.9.9.9", 5353)
    assert server(monkeypatch, DNS_UPSTREAM="9.9.9.9:x")._upstream_endpoint() == (
        "9.9.9.9:x", 53)


def test_an_unreachable_client_gets_no_answer_address(monkeypatch):
    assert icomm_dns.local_address_for("not-an-ip") is None


async def test_rewrite_needs_an_address(monkeypatch):
    transport, proto, port = await upstream()
    try:
        dns = server(monkeypatch, DNS_UPSTREAM=f"127.0.0.1:{port}")
        monkeypatch.setattr(icomm_dns, "local_address_for", lambda peer: None)
        reply = await dns.resolve(query(HEATER_NAME), "10.0.20.187")
        assert answers(reply)[2] == "203.0.113.9"                # fell through to forwarding
    finally:
        transport.close()


# -- listeners --------------------------------------------------------------------
async def test_serves_udp_and_tcp_then_stops(monkeypatch):
    port = free_port()
    dns = server(monkeypatch, DNS_SERVER="1", DNS_PORT=str(port), DNS_ANSWER="10.0.1.113")
    await dns.start()
    try:
        loop = asyncio.get_running_loop()
        got: asyncio.Future = loop.create_future()

        class Client(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                got.set_result(data)

        transport, _ = await loop.create_datagram_endpoint(
            Client, remote_addr=("127.0.0.1", port))
        transport.sendto(query(HEATER_NAME))
        assert answers(await asyncio.wait_for(got, 2))[2] == "10.0.1.113"
        transport.close()

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        q = query(HEATER_NAME)
        writer.write(struct.pack(">H", len(q)) + q)
        size = struct.unpack(">H", await reader.readexactly(2))[0]
        assert answers(await reader.readexactly(size))[2] == "10.0.1.113"
        writer.close()
        assert dns.status()["running"] is True and dns.queries == 2
    finally:
        await dns.stop()
    assert dns.status()["running"] is False
    await dns.stop()                                             # idempotent


async def test_a_taken_port_is_reported_not_fatal(monkeypatch, caplog):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy:
        busy.bind(("0.0.0.0", 0))
        dns = server(monkeypatch, DNS_SERVER="1", DNS_PORT=str(busy.getsockname()[1]))
        await dns.start()
    assert dns.status()["running"] is False and "could not listen" in dns.error
    assert "AdGuard" in caplog.text


async def test_disabled_starts_nothing(monkeypatch):
    dns = server(monkeypatch)
    await dns.start()
    assert dns.status()["enabled"] is False and dns.status()["running"] is False


# -- the relay ---------------------------------------------------------------------
class FailingOn443(FakeSite):
    async def start(self) -> None:
        if self.port == 443:
            raise OSError(98, "Address in use")
        await super().start()


@pytest.mark.parametrize("site, error", [(FakeSite, None), (FailingOn443, "port 443 is in use")])
async def test_the_relay_adds_443_and_runs_dns(tmp_path, monkeypatch, site, error):
    port = free_port()
    relay = _relay(tmp_path, monkeypatch, STATE_PORT="18125", LISTEN_PORT="18445",
                   LAN_PORT="18889", DNS_SERVER="1", DNS_PORT=str(port))
    FakeSite.started = []
    monkeypatch.setattr(web, "TCPSite", site)
    relay.tls_context = lambda: object()
    task = asyncio.create_task(relay.run())
    for _ in range(200):
        await asyncio.sleep(0)
        if relay.client is not None:
            break
    ports = {s.port for s in FakeSite.started}
    assert (443 in ports) is (error is None)
    state = json.loads((await relay.state_handler(None)).body)["dns"]
    assert state["running"] is True and state["port"] == port
    assert (state["https_error"] or None) == (error and state["https_error"])
    assert (error is None) or error in state["https_error"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert relay.dns.status()["running"] is False


async def test_a_refusing_upstream_is_servfail(monkeypatch):
    dns = server(monkeypatch, DNS_UPSTREAM=f"127.0.0.1:{free_port()}")   # nothing listening
    monkeypatch.setattr(icomm_dns, "FORWARD_TIMEOUT", 0.5)
    reply = await dns.resolve(query("example.com"), "10.0.1.50")
    assert reply[3] & 0x0F == 2 and dns.failures == 1
