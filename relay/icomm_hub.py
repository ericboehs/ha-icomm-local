"""Several water heaters behind one relay (#18, phase 2).

Each heater gets its own engine (`icomm_relay.Relay`): queue, telemetry, LAN
session, saved config and offline fallback, stored in `heaters/<DSN>/`. The hub
owns everything there is only one of:
  * the listeners: the impersonated cloud (listen_port, and 443 for DNS
    steering), Local Connect (lan_port) and the state API;
  * the upstream HTTP client, the DNS server, the peer policy and the firmware
    gate.

It routes each request to its heater's engine:

  module-facing    a config fetch /dsns/<DSN>.xml      by its DSN
                   from a heater's own address         by that address (DNS steering)
                   /devices/<id>/...                   by Ayla's device ID (NAT
                                                       steering, where every heater
                                                       arrives from the gateway)
                   anything else                       the primary heater
  Local Connect    by the caller's address (only heaters call back here)

A new heater gets an engine only when Ayla answers its config fetch with 200:
that proves the requester holds the device's credentials, exactly the evidence
single-heater pinning already relies on. Until then it is plain-proxied, so a
stranger on the LAN can't create heaters, directories or state. At most
MAX_HEATERS (default 4).

The **primary** heater is the first one the relay knew (`heaters/PRIMARY`). The
single-heater state API (/state, /raw, /write...) keeps acting on it, so an
older integration still works. Writes through it are refused (409) once there
is more than one heater: /heaters/<dsn>/write says which.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import signal
import time

from aiohttp import ClientSession, ClientTimeout, web
from icomm_heater import Heater, clean_dsn
from icomm_peers import TRUSTED, normalize
from icomm_relay import DEVICE_PATH_RE, DSN_PATH_RE, Relay

_LOGGER = logging.getLogger("icomm_hub")

# State-API routes that act on one heater: aliased to the primary, and under
# /heaters/<dsn>/ for each.
PERSISTED = ("lanip.json", "dsn-original.xml")
HEATER_GETS = {"/state": "state_handler", "/raw": "raw_handler"}
HEATER_POSTS = {"/write": "write_handler", "/write/cancel": "write_cancel_handler",
                "/lan/reporting-mask": "reporting_mask_handler",
                "/nudge": "nudge_handler"}


class Hub:
    def __init__(self, primary: Relay | None = None) -> None:
        self.primary = primary or Relay()
        self.max_heaters = max(1, int(os.environ.get("MAX_HEATERS", "4")))
        self.secondaries: dict[str, Relay] = {}
        self.client: ClientSession | None = None
        self.running = False
        self.refused: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._marked: str | None = None
        self.peers = self.primary.peers
        # Every heater's address is a trusted module address.
        self.peers._module_ip = self._module_ips
        self.peers.pin_candidate = self._pin_candidate
        self.primary.heaters_list = self.dsns
        self._mark_primary()
        self._discover()

    # -- the heaters --------------------------------------------------------
    def engines(self) -> list[Relay]:
        return [self.primary, *self.secondaries.values()]

    def dsns(self) -> list[str]:
        return [e.heater.dsn for e in self.engines() if e.heater.dsn]

    def _module_ips(self) -> set[str]:
        return {e.lan.module_ip for e in self.engines() if e.lan.module_ip}

    def _mark_primary(self) -> None:
        if self.primary.heater.dsn and self._marked != self.primary.heater.dsn:
            self.primary.heater.mark_primary()
            self._marked = self.primary.heater.dsn

    def _discover(self) -> None:
        """Heaters this relay added before a restart: a heaters/<DSN>/ holding the
        key or the config reply (written only after Ayla's 200)."""
        heaters = self.primary.data_dir / "heaters"
        if not heaters.is_dir():
            return
        for path in sorted(heaters.iterdir()):
            dsn = clean_dsn(path.name)
            if dsn != path.name:
                continue             # not ours (or a lower-cased copy on a case-blind disk)
            if (dsn != self.primary.heater.dsn and dsn not in self.secondaries
                    and any((path / name).is_file() for name in PERSISTED)
                    and len(self.engines()) < self.max_heaters):
                self._add(dsn)

    def _add(self, dsn: str) -> Relay:
        heater = Heater(self.primary.data_dir, dsn, self.primary.offline.enabled, legacy=False)
        engine = Relay(heater=heater, secondary=True)
        engine.peers, engine.dns = self.peers, self.primary.dns
        engine.firmware, engine.client = self.primary.firmware, self.client
        engine.heaters_list = self.dsns
        self.secondaries[dsn] = engine
        _LOGGER.warning("heater %s: added (%d of at most %d)", dsn, len(self.engines()),
                        self.max_heaters)
        if self.running:
            engine._load_queue()
            task = asyncio.get_running_loop().create_task(engine.lan.start(listen=False))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return engine

    def by_dsn(self, dsn: str | None) -> Relay | None:
        if not dsn:
            return None
        if dsn == self.primary.heater.dsn:
            return self.primary
        return self.secondaries.get(dsn)

    # -- routing ------------------------------------------------------------
    def _resolve(self, request: web.Request) -> tuple[Relay | None, str]:
        """(engine, how it was found): dsn, first, new, address, device, default."""
        found = DSN_PATH_RE.search(request.path)
        if found:
            dsn = clean_dsn(found.group(1))
            engine = self.by_dsn(dsn)
            if engine is not None:
                return engine, "dsn"
            # The first heater: the primary adopts it, as a single relay does.
            if self.primary.heater.dsn is None:
                return self.primary, "first"
            return None, "new"
        peer = normalize(request.remote)
        for engine in self.engines():
            if engine.lan.module_ip and engine.lan.module_ip == peer:
                return engine, "address"
        device = DEVICE_PATH_RE.match(request.path)
        if device:
            for engine in self.engines():
                if engine.device_id == device.group(1):
                    return engine, "device"
        return self.primary, "default"

    def _pinnable(self, peer: str) -> bool:
        """A heater's own address: private IPv4, not loopback, not a gateway."""
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return (address.version == 4 and address.is_private and not address.is_loopback
                and peer not in self.peers.gateways)

    def _pin_candidate(self, request: web.Request) -> bool:
        found = DSN_PATH_RE.search(request.path)
        if request.method != "GET" or found is None or not self.primary.lan.enabled:
            return False
        engine = self.by_dsn(clean_dsn(found.group(1)))
        if engine is not None and engine.lan.module_ip_configured:
            return False                     # module_ip set: nothing to wait for
        return self._pinnable(normalize(request.remote))

    async def route(self, request: web.Request) -> web.Response:
        engine, how = self._resolve(request)
        if engine is None:
            return await self._new_heater(request)
        peer = normalize(request.remote)
        if how == "default" and self._foreign_device(request):
            # NAT: another heater's cloud request, one no engine owns (refused
            # by max_heaters, or not yet confirmed). Ayla only, untouched: it
            # must never get the primary's writes, key or telemetry.
            request[TRUSTED] = False
            return await self.primary.proxy(request)
        repin = how == "dsn" and self._needs_repin(engine, request, peer)
        if repin:
            # A heater fetching its config from a new address (DHCP): through
            # the pin path, so only Ayla's 200 moves its pin -- and the address
            # is released from any other heater that held it.
            request[TRUSTED] = False
        response = await engine._route(request)
        if repin and engine.lan.module_ip == peer:
            self._release(peer, keep=engine)
        self._mark_primary()
        return response

    def _foreign_device(self, request: web.Request) -> bool:
        device = DEVICE_PATH_RE.match(request.path)
        return bool(device and self.primary.device_id
                    and device.group(1) != self.primary.device_id)

    def _needs_repin(self, engine: Relay, request: web.Request, peer: str) -> bool:
        return (request.method == "GET" and engine.lan.enabled
                and not engine.lan.module_ip_configured
                and engine.lan.module_ip != peer and self._pinnable(peer))

    def _release(self, peer: str, keep: Relay) -> None:
        for other in self.engines():
            if (other is not keep and other.lan.module_ip == peer
                    and not other.lan.module_ip_configured):
                other.lan.module_ip, other.lan.module_ip_observed = None, False
                other.lan._persist()
                _LOGGER.warning("heater %s: address %s now belongs to heater %s; "
                                "waiting for its next config fetch", other.heater.dsn, peer,
                                keep.heater.dsn)

    async def _new_heater(self, request: web.Request) -> web.Response:
        """Another heater's config fetch: an engine on Ayla's 200, if there's room."""
        dsn = clean_dsn(DSN_PATH_RE.search(request.path).group(1))
        peer = normalize(request.remote)
        pin = self.primary.lan.enabled and self._pinnable(peer)
        # Who may introduce a heater: the heater itself (DNS steering), the
        # gateway (NAT steering), or an address the owner listed. Not loopback,
        # and not a stranger -- even one Ayla would answer.
        vouched = (pin or peer in self.peers.gateways or peer in self.peers.extra
                   or self.peers.allow_all)
        if (request.method != "GET" or not vouched
                or len(self.engines()) >= self.max_heaters):
            if len(self.engines()) >= self.max_heaters and dsn not in self.refused:
                self.refused.add(dsn)
                _LOGGER.warning("heater %s: not added, max_heaters (%d) reached; it keeps "
                                "working through Ayla only", dsn, self.max_heaters)
            return await self.primary.proxy(request)

        def adopt_then_rewrite(payload: bytes) -> bytes | None:
            engine = self.secondaries.get(dsn)
            if engine is None:
                if len(self.engines()) >= self.max_heaters:
                    self.refused.add(dsn)    # another heater won the race for the last slot
                    return None
                engine = self._add(dsn)
            if pin:
                engine.lan.pin_module(peer, dsn)
            if engine.lan.enabled or engine.dsn_lan_rewrite:
                return engine._rewrite_dsn(payload)
            engine.note_config(payload)          # LAN mode off: identity only
            return None

        # Untrusted to the primary: Ayla's real 200 is the only way in, never
        # the primary's offline answer during an outage.
        request[TRUSTED] = False
        return await self.primary.proxy(request, transform=adopt_then_rewrite)

    def _lan_engine(self, request: web.Request) -> Relay:
        peer = normalize(request.remote)
        for engine in self.engines():
            if engine.lan.module_ip and engine.lan.module_ip == peer:
                return engine
        return self.primary          # loopback or TRUSTED_PEERS: the primary, as before

    def _lan(self, name: str):
        async def handler(request: web.Request) -> web.Response:
            return await getattr(self._lan_engine(request).lan, name)(request)
        return handler

    # -- the state API ------------------------------------------------------
    def _primary(self, name: str):
        async def handler(request: web.Request) -> web.Response:
            if name in HEATER_POSTS.values() and len(self.engines()) > 1:
                return web.json_response(
                    {"error": "several heaters: use /heaters/<dsn>/... to say which",
                     "heaters": self.dsns()}, status=409)
            return await getattr(self.primary, name)(request)
        return handler

    def _heater(self, name: str):
        async def handler(request: web.Request) -> web.Response:
            engine = self.by_dsn(clean_dsn(request.match_info["dsn"]))
            if engine is None:
                return web.json_response({"error": "no such heater", "heaters": self.dsns()},
                                         status=404)
            return await getattr(engine, name)(request)
        return handler

    async def heaters_handler(self, request: web.Request) -> web.Response:
        now = time.time()
        rows = []
        for engine in self.engines():
            state = engine.state
            rows.append({
                "dsn": engine.heater.dsn, "primary": engine is self.primary,
                "module_ip": engine.lan.module_ip, "device_id": engine.device_id,
                "online": state.get("online", False),
                "fresh": bool(engine.last_telemetry) and now - engine.last_telemetry < 600,
                "mode": state.get("mode"), "setpoint": state.get("setpoint"),
                "water_top": state.get("water_top"),
                "lan_verified": engine.lan.session_verified, "pending": len(engine.pending),
                "model_number": engine.device.facts.get("model_number"),
                "serial_number": engine.device.facts.get("serial_number")})
        return web.json_response({"heaters": rows, "max_heaters": self.max_heaters,
                                  "refused": sorted(self.refused)})

    def state_app(self) -> web.Application:
        app = web.Application()
        for path, name in HEATER_GETS.items():
            app.router.add_get(path, self._primary(name))
            app.router.add_get(f"/heaters/{{dsn}}{path}", self._heater(name))
        for path, name in HEATER_POSTS.items():
            app.router.add_post(path, self._primary(name))
            app.router.add_post(f"/heaters/{{dsn}}{path}", self._heater(name))
        app.router.add_get("/health", self.primary.health_handler)
        app.router.add_post("/firmware/allow", self.primary.firmware_allow_handler)
        app.router.add_get("/heaters", self.heaters_handler)
        return app

    def lan_app(self) -> web.Application:
        port = self.primary.lan.port
        app = web.Application(middlewares=[self.peers.middleware(self.peers.lan_allowed,
                                                                 f":{port}")])
        app.router.add_post("/local_lan/key_exchange.json", self._lan("_key_exchange"))
        app.router.add_route("*", "/local_lan/commands.json", self._lan("_commands"))
        app.router.add_route("*", "/local_lan/property/datapoint.json", self._lan("_datapoint"))
        app.router.add_route("*", "/local_lan/property/datapoint/ack.json",
                             self._lan("_datapoint"))
        return app

    # -- lifecycle ----------------------------------------------------------
    async def run(self) -> None:
        primary = self.primary
        # Only the module (via the gateway's SNAT, or directly) may talk to the
        # impersonated cloud: lan.json alone hands out the LAN key.
        module_app = web.Application(middlewares=[
            self.peers.middleware(self.peers.cloud_allowed, f":{primary.listen_port}",
                                  refuse=False)])
        # Everything the module dials goes upstream verbatim; /dsns/ (fetched
        # during re-registration) is the one path outside /devices and must not
        # fall to a default 404 -- the module is mid-boot when it asks.
        module_app.router.add_route("*", "/{tail:.*}", self.route)

        # The client and the queues first: the listeners can be called the
        # moment they're up. `running` before any await lets a heater added
        # from here on start itself (_add); the ones known now start below.
        timeout = ClientTimeout(total=30)  # Ayla answers in ~100 ms when healthy
        self.client = ClientSession(timeout=timeout, raise_for_status=False)
        known = self.engines()
        for engine in known:
            engine.client = self.client
            engine._load_queue()
        self.running = True

        # Everything from here is torn down on the way out, however far it got:
        # a stop (or a cancel) can arrive in the middle of starting up.
        runners: list[web.AppRunner] = []
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        handled = []
        try:
            runner_state = web.AppRunner(self.state_app())
            await runner_state.setup()
            runners.append(runner_state)
            await web.TCPSite(runner_state, primary.state_host, primary.state_port).start()

            runner_module = web.AppRunner(module_app)
            await runner_module.setup()
            runners.append(runner_module)
            # No keep-alive timeout tuning: the module holds connections for ~26 s
            # per dial and re-dials on its own; nothing here needs to outlive it.
            await web.TCPSite(runner_module, "0.0.0.0", primary.listen_port,
                              ssl_context=primary.tls_context()).start()
            if primary.dns.enabled and primary.listen_port != 443:
                try:
                    await web.TCPSite(runner_module, "0.0.0.0", 443,
                                      ssl_context=primary.tls_context()).start()
                except OSError as err:
                    primary.dns_https_error = f"port 443 is in use: {err.strerror or err}"
                    _LOGGER.error("DNS steering: %s. Another add-on (an SSL proxy?) owns "
                                  "it; the module can't reach the relay until it's free.",
                                  primary.dns_https_error)
            await primary.dns.start()

            if primary.lan.enabled:
                runner_lan = web.AppRunner(self.lan_app())
                await runner_lan.setup()
                runners.append(runner_lan)
                await web.TCPSite(runner_lan, "0.0.0.0", primary.lan.port).start()
            for engine in known:
                await engine.lan.start(listen=False)
            print(f"icomm_local relay: module on :{primary.listen_port} (TLS), "
                  f"state on {primary.state_host}:{primary.state_port}, "
                  f"upstream {primary.upstream}, "
                  f"LAN {'on :' + str(primary.lan.port) if primary.lan.enabled else 'off'}, "
                  f"heaters {len(self.engines())} of at most {self.max_heaters}")
            # The relay runs as PID 1 (run.sh execs it), and the kernel ignores
            # SIGTERM for PID 1 unless a handler is installed. Without one, every
            # add-on stop waited out Docker's timeout and ended in SIGKILL (exit
            # 137, seen live on the 0.4.7 update).
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(sig, stop.set)
                    handled.append(sig)
            await stop.wait()
            _LOGGER.info("stopping on signal")
        finally:
            self.running = False
            for task in list(self._tasks):
                task.cancel()
            for sig in handled:
                loop.remove_signal_handler(sig)
            for engine in self.engines():
                await engine.lan.stop()
            await primary.dns.stop()
            await self.client.close()
            for runner in reversed(runners):
                await runner.cleanup()
