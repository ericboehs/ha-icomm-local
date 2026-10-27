"""A small DNS server that steers the water heater to the relay (dns_server).

For networks whose router can't do the NAT steering (most mesh and ISP
routers). Point the router's DHCP DNS server at the relay host. This server:

  * answers the A.O. Smith module's cloud hostname with the relay host's own
    address, so the module connects to the relay on 443 by itself;
  * answers AAAA for that name with no records, so nothing goes to Ayla over
    IPv6;
  * forwards every other query, byte for byte, to the upstream resolver: by
    default the host's default gateway, which nearly every router serves DNS
    on. Not the host's own resolver, which once the router hands out this
    host as DNS would point back here and loop.

Only names matching DNS_REWRITE are rewritten, by default the A.O. Smith OEM's
device hosts (`<model>-71538015-device.aylanetworks.com`), so other Ayla
appliances in the house keep talking to Ayla directly. The module's own
connection is then trusted by the relay's pinning (icomm_relay._pinnable_dsn).

Research phase 40 proved the path with AdGuard: the module follows the DNS
server DHCP gives it, and the device name is a CNAME for the relay's upstream,
so the relay's own lookups must never use this server.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import re
import socket
import struct
import time

from icomm_peers import default_gateways

_LOGGER = logging.getLogger("icomm_dns")

DEFAULT_REWRITE = r"[a-z0-9-]+-71538015-device\.aylanetworks\.com"
TYPE_A, TYPE_AAAA, CLASS_IN = 1, 28, 1
FORWARD_TIMEOUT = 3.0
REWRITE_TTL = 60
# Forwards in flight at once (each holds a socket for up to FORWARD_TIMEOUT);
# beyond it a query is dropped and the client retries. And queries one client
# may send per second. Generous: a house's worth of devices may use this server.
MAX_IN_FLIGHT = 128
CLIENT_RATE = 100
OPCODE_QUERY, RCODE_NOTIMP = 0, 4


def parse_question(packet: bytes) -> tuple[str, int, int, int] | None:
    """(name, qtype, qclass, end offset) of the first question, or None.

    Queries only: compression pointers never appear in a question a client
    sends, so a pointer (or any malformed label) means "not ours: forward".
    """
    if len(packet) < 12 or packet[2] & 0x80 or struct.unpack(">H", packet[4:6])[0] < 1:
        return None
    labels, pos = [], 12
    while True:
        if pos >= len(packet):
            return None
        length = packet[pos]
        pos += 1
        if length == 0:
            break
        if length & 0xC0 or pos + length > len(packet):
            return None
        labels.append(packet[pos:pos + length].decode("ascii", "replace"))
        pos += length
    if pos + 4 > len(packet):
        return None
    qtype, qclass = struct.unpack(">HH", packet[pos:pos + 4])
    return ".".join(labels).lower(), qtype, qclass, pos + 4


def answer(packet: bytes, end: int, address: str | None) -> bytes:
    """Our reply: one A record for `address`, or no records (NODATA) if None."""
    header = bytearray(packet[:12])
    header[2] = 0x84 | (packet[2] & 0x01)          # response, authoritative, keep RD
    header[3] = 0x80                                # RA, NOERROR
    header[4:12] = struct.pack(">HHHH", 1, 1 if address else 0, 0, 0)
    reply = bytes(header) + packet[12:end]
    if address:
        reply += (b"\xc0\x0c" + struct.pack(">HHIH", TYPE_A, CLASS_IN, REWRITE_TTL, 4)
                  + socket.inet_aton(address))
    return reply


def servfail(packet: bytes) -> bytes:
    if len(packet) < 12:
        return b""
    header = bytearray(packet[:12])
    header[2] = 0x80 | (packet[2] & 0x01)
    header[3] = 0x82                                # RA, SERVFAIL
    return bytes(header) + packet[12:]


def notimp(packet: bytes) -> bytes:
    """NOTIMP for an opcode we don't serve (NOTIFY, UPDATE...)."""
    header = bytearray(packet[:12])
    header[2] = 0x80 | (packet[2] & 0x79)          # response; keep opcode and RD
    header[3] = 0x80 | RCODE_NOTIMP
    return bytes(header) + packet[12:]


def allowed_client(client: str) -> bool:
    """Only the LAN: private, link-local or loopback sources get answers."""
    try:
        address = ipaddress.ip_address(client)
    except ValueError:
        return False
    return address.is_private or address.is_link_local or address.is_loopback


def is_this_host(host: str) -> bool:
    """True for an IP literal that is one of this host's own addresses (bindable)."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False                    # a name: not resolved here (it would block)
    if address.is_loopback or address.is_unspecified:
        return True
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as probe:
        try:
            probe.bind((host, 0))
        except OSError:
            return False
    return True


def local_address_for(peer: str) -> str | None:
    """The address this host uses to reach `peer`: the one it can reach us on."""
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((peer, 9))
        return s.getsockname()[0]
    return None


class DnsServer:
    def __init__(self) -> None:
        self.enabled = os.environ.get("DNS_SERVER", "0") == "1"
        self.port = int(os.environ.get("DNS_PORT", "53"))
        self.upstream = os.environ.get("DNS_UPSTREAM", "").strip() or None
        self.answer_ip = os.environ.get("DNS_ANSWER", "").strip() or None
        self.rewrite = re.compile(os.environ.get("DNS_REWRITE", "") or DEFAULT_REWRITE,
                                  re.IGNORECASE)
        self.queries = self.rewritten = self.forwarded = self.failures = 0
        self.last_rewrite: dict | None = None
        self.last_error: str | None = None
        self.error: str | None = None             # set when the server could not start
        self._transport: asyncio.DatagramTransport | None = None
        self._tcp: asyncio.AbstractServer | None = None
        self._seen: set[tuple[str, str]] = set()
        # Abuse limits (see MAX_IN_FLIGHT): what was dropped, and why.
        self.dropped = {"source": 0, "malformed": 0, "busy": 0, "rate": 0, "duplicate": 0}
        self.in_flight = 0
        self._in_flight_keys: set[tuple[str, bytes]] = set()
        self._rate_window = 0.0
        self._rate_counts: dict[str, int] = {}
        self._tasks: set[asyncio.Task] = set()
        self._config_error = self._check_upstream_port()

    def _check_upstream_port(self) -> str | None:
        if not self.upstream or self.upstream.count(":") != 1:
            return None
        port = self.upstream.rpartition(":")[2]
        if port.isdigit() and not 1 <= int(port) <= 65535:
            return f"dns_upstream port {int(port)} is out of range (1-65535)"
        return None

    def upstream_address(self) -> str | None:
        if self.upstream:
            return self.upstream
        gateways = sorted(default_gateways())
        return gateways[0] if gateways else None

    def _upstream_endpoint(self) -> tuple[str, int] | None:
        """`dns_upstream` may carry a port (`10.0.0.1:5353`); default 53."""
        address = self.upstream_address()
        if not address:
            return None
        host, _, port = address.rpartition(":") if address.count(":") == 1 else ("", "", "")
        if host and port.isdigit():
            return (host, int(port)) if 1 <= int(port) <= 65535 else None
        return (address, 53)

    async def resolve(self, packet: bytes, client: str) -> bytes:
        """One query in, one reply out (empty bytes: drop it)."""
        self.queries += 1
        if not allowed_client(client):
            self.dropped["source"] += 1
            return b""
        if len(packet) < 12 or packet[2] & 0x80:
            self.dropped["malformed"] += 1      # too short, or a response: never a query
            return b""
        if not self._within_rate(client):
            self.dropped["rate"] += 1
            return b""
        if (packet[2] >> 3) & 0x0F != OPCODE_QUERY:
            return notimp(packet)
        question = parse_question(packet)
        if question:
            name, qtype, qclass, end = question
            if qclass == CLASS_IN and self.rewrite.fullmatch(name):
                if qtype == TYPE_A:
                    target = self.answer_ip or local_address_for(client)
                    if target:
                        self._note_rewrite(name, client, target)
                        return answer(packet, end, target)
                if qtype == TYPE_AAAA:
                    return answer(packet, end, None)
        key = (client, packet[:2] + packet[12:])
        if key in self._in_flight_keys:
            self.dropped["duplicate"] += 1      # a retransmit: the first one's reply answers it
            return b""
        if self.in_flight >= MAX_IN_FLIGHT:
            self.dropped["busy"] += 1
            return b""
        self.in_flight += 1
        self._in_flight_keys.add(key)
        try:
            return await self.forward(packet)
        finally:
            self.in_flight -= 1
            self._in_flight_keys.discard(key)

    def _within_rate(self, client: str) -> bool:
        now = time.monotonic()
        if now - self._rate_window >= 1.0:
            self._rate_window = now
            self._rate_counts.clear()
        count = self._rate_counts.get(client, 0) + 1
        self._rate_counts[client] = count
        return count <= CLIENT_RATE

    def _note_rewrite(self, name: str, client: str, target: str) -> None:
        self.rewritten += 1
        self.last_rewrite = {"name": name, "client": client, "answer": target,
                             "at": time.time()}
        if (client, name) not in self._seen and len(self._seen) < 64:
            self._seen.add((client, name))
            _LOGGER.warning("DNS: steering %s for %s to %s", name, client, target)

    async def forward(self, packet: bytes) -> bytes:
        endpoint = self._upstream_endpoint()
        if not endpoint:
            self._fail("no upstream resolver (no default gateway; set dns_upstream)")
            return servfail(packet)
        upstream = f"{endpoint[0]}:{endpoint[1]}"
        loop = asyncio.get_running_loop()
        reply: asyncio.Future[bytes] = loop.create_future()

        class _Once(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                if not reply.done() and data[:2] == packet[:2]:
                    reply.set_result(data)

            def error_received(self, exc):
                if not reply.done():
                    reply.set_exception(exc)

        transport = None
        try:
            transport, _ = await loop.create_datagram_endpoint(
                _Once, remote_addr=endpoint)
            transport.sendto(packet)
            data = await asyncio.wait_for(reply, FORWARD_TIMEOUT)
        except (OSError, TimeoutError) as err:
            self._fail(f"{upstream}: {type(err).__name__}")
            return servfail(packet)
        finally:
            if transport is not None:
                transport.close()
        self.forwarded += 1
        return data

    def _fail(self, message: str) -> None:
        self.failures += 1
        if message != self.last_error:
            _LOGGER.warning("DNS: forwarding failed (%s)", message)
        self.last_error = message

    # -- listeners -------------------------------------------------------
    async def start(self) -> None:
        if not self.enabled:
            return
        self.error = self._config_error or self._loop_error()
        if self.error:
            _LOGGER.error("DNS: not started: %s", self.error)
            return
        loop = asyncio.get_running_loop()
        server = self

        class _Udp(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                if not allowed_client(addr[0]):
                    server.queries += 1
                    server.dropped["source"] += 1      # no task for a stranger
                    return

                async def reply():
                    out = await server.resolve(data, addr[0])
                    if out:
                        self.transport.sendto(out, addr)
                task = loop.create_task(reply())
                server._tasks.add(task)
                task.add_done_callback(server._tasks.discard)

        async def _tcp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                with contextlib.suppress(OSError, asyncio.IncompleteReadError, TimeoutError):
                    size = struct.unpack(
                        ">H", await asyncio.wait_for(reader.readexactly(2), 10))[0]
                    data = await asyncio.wait_for(reader.readexactly(size), 10)
                    out = await server.resolve(data, writer.get_extra_info("peername")[0])
                    if out:
                        writer.write(struct.pack(">H", len(out)) + out)
                        await writer.drain()
            finally:
                writer.close()

        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                _Udp, local_addr=("0.0.0.0", self.port))
            self._tcp = await asyncio.start_server(_tcp, "0.0.0.0", self.port)
        except OSError as err:
            self.error = f"could not listen on port {self.port}: {err.strerror or err}"
            _LOGGER.error("DNS: %s. Is another DNS server (AdGuard, Pi-hole) running "
                          "on this host? Turn dns_server off and add a rewrite there.",
                          self.error)
            await self.stop()
            return
        _LOGGER.warning("DNS: serving on :%d; rewriting %s, forwarding the rest to %s",
                        self.port, self.rewrite.pattern,
                        self.upstream_address() or "nothing (no gateway)")

    def _loop_error(self) -> str | None:
        """dns_upstream naming this very server would forward every query to
        itself until each one timed out."""
        endpoint = self._upstream_endpoint()
        if endpoint and endpoint[1] == self.port and is_this_host(endpoint[0]):
            return (f"dns_upstream points back at this DNS server (port {self.port}); "
                    "set it to the router or another resolver")
        return None

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        if self._tcp is not None:
            self._tcp.close()
            with contextlib.suppress(Exception):
                await self._tcp.wait_closed()
            self._tcp = None

    def status(self) -> dict:
        return {"enabled": self.enabled, "running": self._transport is not None,
                "error": self.error, "port": self.port,
                "upstream": self.upstream_address(), "queries": self.queries,
                "rewritten": self.rewritten, "forwarded": self.forwarded,
                "failures": self.failures, "last_error": self.last_error,
                "dropped": dict(self.dropped), "in_flight": self.in_flight,
                "last_rewrite": self.last_rewrite}
