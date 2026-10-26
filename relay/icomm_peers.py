"""Who may talk to the module-facing listeners.

Both module-facing ports bind 0.0.0.0, because the router steers the module to
the host's LAN address. Until 0.3.8 they answered anyone, and the pre-publish
review showed what that meant. Any host on the LAN could fetch `lan.json` from
:8444 and get the relay's LAN key. With the key it could register with the
module and write raw frames to any register, bypassing every clamp. It could
also post fake telemetry that "confirmed" writes, or drain the write queue by
polling :8888.

Port 8444 (the impersonated Ayla cloud) is reached through the router's DNAT and
SNAT rules, so the module arrives *from the gateway's address*. When the module
is steered some other way, it connects directly from its own address. Port 8888
(Local Connect) is only ever called back by the module itself, from its own
address, after the relay registers with it.

Untrusted peers on :8444 are not refused. They get plain proxying to Ayla: no
local answers (lan.json, the lanip ack), no telemetry ingest, no write
injection, no DSN rewrite. A misconfigured but legitimate module, for example
one whose SNAT source is not the host's default gateway, therefore keeps its
cloud connection instead of being blacked out. It just gets no local features,
and the log says why. A stranger gains nothing it could not get by talking to
Ayla directly. Untrusted peers on :8888 are refused with 403.

Trusted peers:

  :8444  loopback, the host's default gateway(s), the module's learned
         address, and TRUSTED_PEERS
  :8888  loopback, the module's learned address, and TRUSTED_PEERS

TRUSTED_PEERS is a comma-separated list of extra addresses. `*` turns the check
off, as an escape hatch for unusual steering. Loopback stays trusted because
the unauthenticated state API is on loopback too: a process on the host can
already queue writes.

Residual risk, documented rather than hidden: anything the gateway itself NATs
to this host looks like the module on :8444. With the documented rules, only
the module's own traffic is NATed.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import time

from aiohttp import web

_LOGGER = logging.getLogger("icomm_peers")
LOOPBACK = {"127.0.0.1", "::1"}
TRUSTED = web.RequestKey("icomm_trusted", bool)


def normalize(address: str | None) -> str:
    """'::ffff:10.0.1.1' -> '10.0.1.1'; None -> ''."""
    if not address:
        return ""
    return address[7:] if address.startswith("::ffff:") else address


def default_gateways(route_file: str = "/proc/net/route") -> set[str]:
    """IPv4 default gateways from the kernel routing table (Linux only).

    The add-on runs with host_network, so this is the HA host's own table. On a
    platform without /proc (a development Mac) the set is empty and
    TRUSTED_PEERS must name the gateway.
    """
    found: set[str] = set()
    try:
        with open(route_file) as table:
            next(table, None)
            for line in table:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000" and fields[2] != "00000000":
                    found.add(socket.inet_ntoa(struct.pack("<L", int(fields[2], 16))))
    except (OSError, ValueError):
        pass
    return found


class PeerPolicy:
    def __init__(self, module_ip_source) -> None:
        # A callable, because the module's address is learned (and can change)
        # at runtime from its own registration reply.
        self._module_ip = module_ip_source
        raw = os.environ.get("TRUSTED_PEERS", "").strip()
        self.allow_all = raw == "*"
        self.extra = {normalize(p.strip()) for p in raw.split(",")
                      if p.strip() and p.strip() != "*"}
        self.gateways = default_gateways()
        self._gateways_read = time.time()
        self.rejected: dict[str, int] = {}
        self._warned: dict[str, float] = {}
        # Set by the relay: is this untrusted request the module's config
        # fetch, which can still pin it (DNS steering, before Ayla's 200)?
        self.pin_candidate = lambda request: False
        self.awaiting_pin = 0

    def _refresh_gateways(self) -> None:
        """The add-on can start before the host has its default route."""
        if time.time() - self._gateways_read > 60:
            self._gateways_read = time.time()
            self.gateways = default_gateways()

    def _base(self) -> set[str]:
        module = self._module_ip()
        # One address (a single relay) or several (the hub's heaters, #18).
        modules = {module} if isinstance(module, str) else set(module or ())
        return LOOPBACK | self.extra | {m for m in modules if m}

    def cloud_allowed(self, remote: str | None) -> bool:
        """:8444, the impersonated Ayla cloud."""
        peer = normalize(remote)
        if self.allow_all or peer in self._base() or peer in self.gateways:
            return True
        self._refresh_gateways()
        return peer in self.gateways

    def lan_allowed(self, remote: str | None) -> bool:
        """:8888, Local Connect: only the module calls back here."""
        return self.allow_all or normalize(remote) in self._base()

    def status(self) -> dict:
        return {"gateways": sorted(self.gateways), "extra": sorted(self.extra),
                "allow_all": self.allow_all, "rejected": dict(self.rejected),
                "awaiting_pin": self.awaiting_pin}

    @staticmethod
    def _port(request: web.Request, fallback: str) -> str:
        """The port the request really arrived on (one app serves 8444 and 443)."""
        sockname = request.transport.get_extra_info("sockname") if request.transport else None
        return f":{sockname[1]}" if isinstance(sockname, tuple) and len(sockname) > 1 else fallback

    def note_untrusted(self, request: web.Request, port_label: str, action: str) -> None:
        peer = normalize(request.remote) or "?"
        port_label = self._port(request, port_label)
        if self.pin_candidate(request):
            # Expected once per boot with DNS steering: the module's first
            # config fetch, before Ayla has checked its credentials.
            self.awaiting_pin += 1
            _LOGGER.info("%s from %s on %s: waiting for Ayla to confirm the module "
                         "(its config fetch) before trusting it",
                         request.path.replace("\r", "").replace("\n", ""),
                         peer.replace("\r", "").replace("\n", ""), port_label)
            return
        if peer in self.rejected or len(self.rejected) < 64:      # bounded
            self.rejected[peer] = self.rejected.get(peer, 0) + 1
        now = time.time()
        if now - self._warned.get(peer, 0) > 3600:
            if len(self._warned) > 256:
                self._warned.clear()
            self._warned[peer] = now
            # The path is decoded, so %0a in it would forge a log line: strip
            # CR/LF from everything the client chose before logging it.
            method = request.method.replace("\r", "").replace("\n", "")
            path = request.path.replace("\r", "").replace("\n", "")
            shown = peer.replace("\r", "").replace("\n", "")
            _LOGGER.warning(
                "%s %s %r from %s on %s: not a trusted peer (gateways %s, module %s). "
                "If %s is your router/gateway, add it to the trusted_peers option. If it "
                "is the water heater, power-cycle it so Ayla can confirm it (DNS "
                "steering), or set module_ip.", action, method,
                path, shown, port_label, sorted(self.gateways) or "none detected",
                self._modules_shown(), shown)

    def _modules_shown(self) -> str:
        module = self._module_ip()
        modules = [module] if isinstance(module, str) else sorted(m for m in module or () if m)
        return ", ".join(modules) or "unknown"

    def middleware(self, allowed, port_label: str, refuse: bool = True):
        """refuse=True: 403 for untrusted peers. False: mark request[TRUSTED]."""
        policy = self

        @web.middleware
        async def check(request: web.Request, handler):
            trusted = allowed(request.remote)
            request[TRUSTED] = trusted
            if not trusted:
                policy.note_untrusted(request, port_label,
                                      "refused" if refuse else "plain-proxying")
                if refuse:
                    return web.Response(status=403)
            return await handler(request)

        return check
