"""The DNS server under abuse: strangers, floods, loops and odd packets."""

import asyncio
import struct

import pytest
from icomm_dns import MAX_IN_FLIGHT, DnsServer, allowed_client, is_this_host, notimp
from test_dns import HEATER_NAME, free_port, query, server, upstream


# -- #2: who gets answers, and how many forwards at once -------------------------
@pytest.mark.parametrize("client, ok", [
    ("10.0.1.50", True), ("192.168.1.2", True), ("172.16.0.9", True),
    ("127.0.0.1", True), ("169.254.3.4", True), ("fe80::1", True), ("fd00::5", True),
    ("8.8.8.8", False), ("2001:4860::1", False), ("not-an-ip", False)])
def test_only_lan_sources_are_answered(client, ok):
    assert allowed_client(client) is ok


async def test_a_public_source_is_dropped_before_forwarding(monkeypatch):
    dns = server(monkeypatch, DNS_ANSWER="10.0.1.113")
    assert await dns.resolve(query(HEATER_NAME), "8.8.8.8") == b""
    assert dns.status()["dropped"]["source"] == 1 and dns.rewritten == 0


async def test_forwards_beyond_the_cap_are_dropped(monkeypatch):
    monkeypatch.setattr("icomm_dns.CLIENT_RATE", 10**6)
    dns = server(monkeypatch, DNS_UPSTREAM="10.0.1.1")
    release = asyncio.Event()

    async def slow(packet):
        await release.wait()
        return b"reply"

    monkeypatch.setattr(dns, "forward", slow)
    tasks = [asyncio.create_task(dns.resolve(query(f"h{n}.example", txid=n), "10.0.1.50"))
             for n in range(MAX_IN_FLIGHT + 3)]
    await asyncio.sleep(0)
    assert dns.in_flight == MAX_IN_FLIGHT
    release.set()
    results = await asyncio.gather(*tasks)
    assert results.count(b"reply") == MAX_IN_FLIGHT and results.count(b"") == 3
    assert dns.status()["dropped"]["busy"] == 3 and dns.in_flight == 0


async def test_a_duplicate_in_flight_query_is_dropped(monkeypatch):
    dns = server(monkeypatch, DNS_UPSTREAM="10.0.1.1")
    release = asyncio.Event()

    async def slow(packet):
        await release.wait()
        return b"reply"

    monkeypatch.setattr(dns, "forward", slow)
    q = query("a.example")
    first = asyncio.create_task(dns.resolve(q, "10.0.1.50"))
    await asyncio.sleep(0)
    assert await dns.resolve(q, "10.0.1.50") == b""                # the retransmit
    other = asyncio.create_task(dns.resolve(q, "10.0.1.51"))        # another client: fine
    await asyncio.sleep(0)
    release.set()
    assert await first == b"reply" and await other == b"reply"
    assert dns.dropped["duplicate"] == 1
    assert await dns.resolve(q, "10.0.1.50") == b"reply"           # done: asked again


async def test_one_client_is_rate_limited_per_second(monkeypatch):
    monkeypatch.setattr("icomm_dns.CLIENT_RATE", 3)
    clock = [1000.0]
    monkeypatch.setattr("icomm_dns.time.monotonic", lambda: clock[0])
    dns = server(monkeypatch, DNS_ANSWER="10.0.1.113")
    replies = [await dns.resolve(query(HEATER_NAME), "10.0.1.50") for _ in range(5)]
    assert [bool(r) for r in replies] == [True, True, True, False, False]
    assert await dns.resolve(query(HEATER_NAME), "10.0.1.51")      # someone else
    clock[0] += 1.0
    assert await dns.resolve(query(HEATER_NAME), "10.0.1.50")      # a new second
    assert dns.dropped["rate"] == 2


async def test_udp_strangers_get_no_task_and_tasks_are_tracked(monkeypatch):
    port = free_port()
    dns = server(monkeypatch, DNS_SERVER="1", DNS_PORT=str(port), DNS_ANSWER="10.0.1.113")
    await dns.start()
    loop = asyncio.get_running_loop()
    try:
        monkeypatch.setattr("icomm_dns.allowed_client", lambda client: False)
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=("127.0.0.1", port))
        transport.sendto(query(HEATER_NAME))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if dns.dropped["source"]:
                break
        transport.close()
        assert dns.dropped["source"] == 1 and not dns._tasks
        # A forward still running at stop is cancelled, not leaked.
        monkeypatch.setattr("icomm_dns.allowed_client", lambda client: True)
        hang = asyncio.Event()

        async def stuck(packet, client):
            await hang.wait()

        monkeypatch.setattr(dns, "resolve", stuck)
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=("127.0.0.1", port))
        transport.sendto(query("x.example"))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if dns._tasks:
                break
        transport.close()
        assert len(dns._tasks) == 1
    finally:
        await dns.stop()
    assert not dns._tasks


# -- #3: a loop back to ourselves -----------------------------------------------
@pytest.mark.parametrize("upstream_value", ["127.0.0.1:{port}", "0.0.0.0:{port}"])
async def test_an_upstream_that_is_this_server_refuses_to_start(monkeypatch, upstream_value,
                                                                 caplog):
    port = free_port()
    dns = server(monkeypatch, DNS_SERVER="1", DNS_PORT=str(port),
                 DNS_UPSTREAM=upstream_value.format(port=port))
    await dns.start()
    status = dns.status()
    assert status["running"] is False and "points back at this DNS server" in status["error"]
    assert "not started" in caplog.text


async def test_this_host_on_another_port_is_fine(monkeypatch):
    port = free_port()
    dns = server(monkeypatch, DNS_SERVER="1", DNS_PORT=str(port),
                 DNS_UPSTREAM=f"127.0.0.1:{free_port()}")
    await dns.start()
    try:
        assert dns.status()["running"] is True and dns.error is None
    finally:
        await dns.stop()


def test_is_this_host():
    assert is_this_host("127.0.0.1") and is_this_host("::1")
    assert not is_this_host("router.lan")              # names aren't resolved here
    assert not is_this_host("192.0.2.200")             # TEST-NET: never ours
    assert not is_this_host("2001:db8::99")


def test_a_local_non_loopback_address_counts_as_this_host(monkeypatch):
    class Bindable:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def connect(self, addr):
            self.peer = addr[0]

        def getsockname(self):
            return (self.peer, 50000)              # routed to itself: ours

    monkeypatch.setattr("icomm_dns.socket.socket", Bindable)
    assert is_this_host("10.0.1.5")


# -- #13: a bad port, and the TCP handler always closes --------------------------
@pytest.mark.parametrize("value", ["10.0.1.1:70000", "10.0.1.1:0"])
async def test_an_out_of_range_upstream_port_is_an_error_status(monkeypatch, value):
    dns = server(monkeypatch, DNS_SERVER="1", DNS_PORT=str(free_port()), DNS_UPSTREAM=value)
    assert dns._upstream_endpoint() is None
    reply = await dns.resolve(query("a.example"), "10.0.1.50")       # SERVFAIL, no crash
    assert reply[3] & 0x0F == 2
    await dns.start()
    assert dns.status()["running"] is False and "out of range" in dns.error


async def test_the_tcp_writer_is_closed_even_when_resolve_fails(monkeypatch):
    port = free_port()
    dns = server(monkeypatch, DNS_SERVER="1", DNS_PORT=str(port), DNS_ANSWER="10.0.1.113")
    await dns.start()
    closed = asyncio.Event()

    async def boom(packet, client, tcp=False):
        try:
            raise RuntimeError("bug")
        finally:
            closed.set()

    monkeypatch.setattr(dns, "resolve", boom)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        q = query(HEATER_NAME)
        writer.write(struct.pack(">H", len(q)) + q)
        await asyncio.wait_for(closed.wait(), 2)
        assert await asyncio.wait_for(reader.read(), 2) == b""         # closed on us
        writer.close()
    finally:
        await dns.stop()


async def test_a_dropped_tcp_query_gets_no_reply_bytes(monkeypatch):
    port = free_port()
    dns = server(monkeypatch, DNS_SERVER="1", DNS_PORT=str(port))
    await dns.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(struct.pack(">H", 4) + b"\x00\x01\x80\x00")        # short: dropped
        assert await asyncio.wait_for(reader.read(), 2) == b""
        writer.close()
    finally:
        await dns.stop()


# -- #14: responses, short packets and other opcodes ----------------------------
async def test_responses_and_short_packets_are_dropped_not_forwarded(monkeypatch):
    transport, proto, port = await upstream()
    try:
        dns = server(monkeypatch, DNS_UPSTREAM=f"127.0.0.1:{port}")
        assert await dns.resolve(query("a.example", flags=0x8180), "10.0.1.50") == b""
        assert await dns.resolve(b"\x12\x34\x01", "10.0.1.50") == b""
        assert proto.seen == [] and dns.dropped["malformed"] == 2
    finally:
        transport.close()


@pytest.mark.parametrize("opcode", [1, 2, 4, 5])
async def test_other_opcodes_get_notimp(monkeypatch, opcode):
    transport, proto, port = await upstream()
    try:
        dns = server(monkeypatch, DNS_UPSTREAM=f"127.0.0.1:{port}")
        q = query(HEATER_NAME, flags=(opcode << 11) | 0x0100)
        reply = await dns.resolve(q, "10.0.1.50")
        assert reply[:2] == q[:2] and reply[2] & 0x80                  # a response...
        assert (reply[2] >> 3) & 0x0F == opcode and reply[2] & 0x01    # ...same opcode, RD
        assert reply[3] & 0x0F == 4 and proto.seen == []               # NOTIMP, not forwarded
        assert dns.rewritten == 0
    finally:
        transport.close()


def test_notimp_shape():
    q = query("a.example", flags=(4 << 11))
    assert notimp(q)[3] == 0x84 and notimp(q)[12:] == q[12:]


# -- TCP clients get the whole answer; the listening address is configurable --------
async def _tcp_upstream(reply_id_ok=True):
    """A TCP-only upstream answering with a full (TC=0) reply."""
    from icomm_dns import answer, parse_question

    async def handle(reader, writer):
        size = struct.unpack(">H", await reader.readexactly(2))[0]
        data = await reader.readexactly(size)
        _, _, _, end = parse_question(data)
        out = answer(data, end, "203.0.113.7")
        if not reply_id_ok:
            out = bytes([out[0] ^ 0xFF]) + out[1:]
        writer.write(struct.pack(">H", len(out)) + out)
        await writer.drain()
        writer.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


def _truncated(packet):
    return packet[:2] + b"\x83\x80" + packet[4:]           # QR, TC, RD; RA


@pytest.mark.parametrize("tcp, id_ok, want_tc", [
    (True, True, False),       # TCP client: re-asked over TCP, full answer
    (False, True, True),       # UDP client: TC=1 tells it to retry over TCP itself
    (True, False, True)])      # upstream's TCP answer under another id: keep TC=1
async def test_a_truncated_reply_is_re_asked_over_tcp_for_tcp_clients(
        monkeypatch, tcp, id_ok, want_tc):
    srv, port = await _tcp_upstream(id_ok)
    try:
        dns = server(monkeypatch, DNS_UPSTREAM=f"127.0.0.1:{port}")

        async def udp_truncated(packet):
            return _truncated(packet)

        monkeypatch.setattr(dns, "forward", udp_truncated)
        reply = await dns.resolve(query("big.example"), "10.0.1.50", tcp=tcp)
        assert bool(reply[2] & 0x02) is want_tc
    finally:
        srv.close()
        await srv.wait_closed()


async def test_tcp_upstream_down_keeps_the_truncated_reply(monkeypatch):
    port = free_port()                                  # nothing listens on TCP here
    dns = server(monkeypatch, DNS_UPSTREAM=f"127.0.0.1:{port}")

    async def udp_truncated(packet):
        return _truncated(packet)

    monkeypatch.setattr(dns, "forward", udp_truncated)
    reply = await dns.resolve(query("big.example"), "10.0.1.50", tcp=True)
    assert reply[2] & 0x02 and "over TCP" in dns.last_error


async def test_dns_host_narrows_the_listening_address(monkeypatch):
    dns = server(monkeypatch, DNS_SERVER="1", DNS_PORT="0", DNS_HOST="127.0.0.1")
    await dns.start()
    try:
        assert dns._transport.get_extra_info("sockname")[0] == "127.0.0.1"
        assert dns._tcp.sockets[0].getsockname()[0] == "127.0.0.1"
    finally:
        await dns.stop()
    monkeypatch.delenv("DNS_HOST")
    assert DnsServer().host == "0.0.0.0"                # the add-on's default


async def test_tcp_retry_without_an_upstream_is_a_no_op(monkeypatch):
    monkeypatch.setattr("icomm_dns.default_gateways", set)
    assert await server(monkeypatch).forward_tcp(query("big.example")) == b""
