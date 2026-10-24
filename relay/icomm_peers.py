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
        self.rejected: dict[str, int] = {}
        self._warned: dict[str, float] = {}

    def _base(self) -> set[str]:
        module = self._module_ip()
        return LOOPBACK | self.extra | ({module} if module else set())

    def cloud_allowed(self, remote: str | None) -> bool:
        """:8444, the impersonated Ayla cloud."""
        peer = normalize(remote)
        return self.allow_all or peer in self._base() or peer in self.gateways

    def lan_allowed(self, remote: str | None) -> bool:
        """:8888, Local Connect: only the module calls back here."""
        return self.allow_all or normalize(remote) in self._base()

    def status(self) -> dict:
        return {"gateways": sorted(self.gateways), "extra": sorted(self.extra),
                "allow_all": self.allow_all, "rejected": dict(self.rejected)}

    def middleware(self, allowed, port_label: str):
        policy = self

        @web.middleware
        async def check(request: web.Request, handler):
            if not allowed(request.remote):
                peer = normalize(request.remote) or "?"
                policy.rejected[peer] = policy.rejected.get(peer, 0) + 1
                now = time.time()
                if now - policy._warned.get(peer, 0) > 3600:
                    policy._warned[peer] = now
                    _LOGGER.warning(
                        "refused %s %s from %s on %s: not the module or the "
                        "gateway. If this IS the module, set the add-on's "
                        "module_ip or trusted_peers option.", request.method,
                        request.path, peer, port_label)
                return web.Response(status=403)
            return await handler(request)

        return check
