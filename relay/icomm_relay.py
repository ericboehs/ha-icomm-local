#!/usr/bin/env python3
"""icomm_local relay — a transparent local stand-in for Ayla's cloud.

Sits in the path of an iCOMM water heater module's TLS traffic, forwards every
request upstream to Ayla untouched (so the cloud and the mobile app keep working),
and decodes the telemetry as it passes. The decoded state is served as JSON for
the icomm_local Home Assistant integration to poll.

    module ──▶ :443 (this relay, TLS) ──▶ proxy-ingress-usfield.aylanetworks.com
                    │
                    └── decode superBlockDyn1 ──▶ http://127.0.0.1:8124/state

Two listeners, one process:

  * the TLS listener the module connects to (LISTEN_PORT, default 8444 in
    development, 443 in production). Serves the certificate Ayla-shaped enough
    for the module: any RSA-2048 leaf signed SHA-256 with a *.aylanetworks.com
    name works -- the module never checks the chain. That is the measured,
    load-bearing finding of the research this comes from
    (github.com/ericboehs/aosmith-water-heater, ARCHITECTURE.md).
  * the plain-HTTP state API (STATE_PORT, default 8124, localhost only) that the
    Home Assistant integration polls: GET /state, GET /health.

Everything is forwarded upstream verbatim -- this relay is read-only in phase 1
and deliberately never modifies a response. Writes (setpoint, mode) come later,
delivered as replacements for commands.xml poll replies.

Run:  python3 icomm_relay.py   (env below; see README for the full setup)

Env:
  RELAY_CERT / RELAY_KEY   TLS leaf (see gen-cert.sh)
  LISTEN_PORT=8444         port the module is steered to
  STATE_PORT=8124          localhost state API
  UPSTREAM=proxy-ingress-usfield.aylanetworks.com
  DEVICE_DSN=              optional, tags state with the DSN when known
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

TEMP_F = 1.8 / 256.0

# The register atlas, measured field by field. Offsets are into the five blocks
# of the module's superBlockDyn1 telemetry (block lengths 8/146/230/52/52;
# block[0] is a header). icomm/README.md in the research repo is the evidence.
MODE_PAIRS = {(100, 1): "electric", (0, 4): "hybrid", (0, 5): "heat_pump"}


def fahrenheit(raw: bytes) -> float:
    return int.from_bytes(raw, "big") * TEMP_F + 32


def decode(blocks: list[bytes]) -> dict:
    """One telemetry post -> the fields the integration exposes."""
    b1, b3, b4 = blocks[1], blocks[3], blocks[4]
    pair = (b3[30], b3[31])
    return {
        "setpoint": round(fahrenheit(b3[0:2]), 1),
        "max_setpoint": round(fahrenheit(b1[86:88]), 1),
        "water_top": round(fahrenheit(b1[0:2]), 1),
        "water_bottom": round(fahrenheit(b1[4:6]), 1),
        "mode_raw": f"{pair[0]}/{pair[1]}",
        "mode": MODE_PAIRS.get(pair, "unknown"),
        "watts": int.from_bytes(b4[12:14], "big"),
        "volts": b1[99],
        "air": round(fahrenheit(b4[30:32]), 1),
        "rssi": int.from_bytes(b4[40:42], "big", signed=True),
    }


def telemetry_blocks(body: bytes) -> list[bytes] | None:
    """Pull the hex block list out of a superBlockDyn1 datapoint POST."""
    try:
        text = body.decode(errors="replace")
        start = text.index("<value>") + len("<value>")
        end = text.index("</value>", start)
        blob = text[start:end].strip()
        if "," not in blob:
            return None
        return [bytes.fromhex(part) for part in blob.split(",") if part]
    except (ValueError, IndexError):
        return None


class Relay:
    def __init__(self) -> None:
        self.upstream = os.environ.get("UPSTREAM", "proxy-ingress-usfield.aylanetworks.com")
        self.listen_port = int(os.environ.get("LISTEN_PORT", "8444"))
        self.state_port = int(os.environ.get("STATE_PORT", "8124"))
        self.device_dsn = os.environ.get("DEVICE_DSN", "")
        self.state: dict = {"online": False}
        self.last_telemetry = 0.0
        self.started = time.time()
        self.upstream_errors = 0
        self.client: ClientSession | None = None
        # Pinned Entrust G2 for upstream verification: certifi dropped it, so
        # default verification fails against Ayla's ingress. Public CA cert.
        ca = Path(__file__).parent / "upstream-entrust-g2.pem"
        # aiohttp wants an SSLContext (a bare path string raises on use).
        self.upstream_ssl = ssl.create_default_context(cafile=str(ca)) if ca.exists() else None

    # -- the module-facing side ---------------------------------------
    async def proxy(self, request: web.Request) -> web.Response:
        """Forward a request upstream verbatim; decode telemetry as it passes.

        The appliance's cloud view must stay intact (the mobile app depends on
        it), so the response is returned exactly as Ayla sent it. Nothing is
        modified in phase 1.
        """
        body = await request.read()
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in {"host", "content-length", "transfer-encoding", "connection"}
        }
        try:
            assert self.client is not None
            async with self.client.request(
                request.method, f"https://{self.upstream}{request.path_qs}",
                headers=headers, data=body, allow_redirects=False,
                ssl=self.upstream_ssl,
            ) as upstream:
                payload = await upstream.read()
                response = web.Response(status=upstream.status, body=payload)
                for k, v in upstream.headers.items():
                    if k.lower() not in {"content-length", "transfer-encoding",
                                        "connection", "content-encoding"}:
                        response.headers[k] = v
                return response
        except Exception:
            # Ayla is often down or slow; the module must never see our failure
            # become its own. Synthetic replies keep its conversation alive.
            self.upstream_errors += 1
            if request.method == "POST":
                return web.Response(status=200, text="")
            if "commands.xml" in request.path:
                return web.Response(status=200, text_type="application/xml",
                                    text="<commands>\n  <properties/>\n  <schedules/>\n  <cmds/>\n</commands>\n")
            return web.Response(status=503, text="")

    async def telemetry(self, request: web.Request) -> web.Response:
        """superBlockDyn1 POST: decode, then forward as if we were not here."""
        body = await request.read()
        blocks = telemetry_blocks(body)
        if blocks and len(blocks) >= 5:
            try:
                self.state = decode(blocks)
                self.state["online"] = True
                self.state["last_telemetry"] = time.time()
                if self.device_dsn:
                    self.state["dsn"] = self.device_dsn
                self.last_telemetry = time.time()
            except (IndexError, ValueError):
                pass
        return await self.proxy(request)

    # -- the Home-Assistant-facing side --------------------------------
    async def state_handler(self, request: web.Request) -> web.Response:
        out = dict(self.state)
        out["last_telemetry_age"] = round(time.time() - self.last_telemetry, 1) if self.last_telemetry else None
        out["upstream_errors"] = self.upstream_errors
        # A stale tank is one that has not posted in 10 minutes: the module is
        # event-driven, and a satisfied tank goes quiet for long stretches.
        out["fresh"] = bool(self.last_telemetry) and (time.time() - self.last_telemetry) < 600
        return web.json_response(out)

    async def health_handler(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "uptime": round(time.time() - self.started),
                                  "upstream": self.upstream})

    # -- lifecycle ------------------------------------------------------
    def tls_context(self) -> ssl.SSLContext:
        cert = os.environ.get("RELAY_CERT", "certs/aylanetworks.com.crt")
        key = os.environ.get("RELAY_KEY", "certs/aylanetworks.com.key")
        dhparams = os.environ.get("RELAY_DHPARAMS", "certs/dhparams.pem")
        for path in (cert, key):
            if not Path(path).exists():
                raise SystemExit(f"missing {path} -- run relay/gen-cert.sh first")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        # The module offers exactly these suites and no P-384 signature scheme;
        # anything else either fails the handshake or deadlocks it silently.
        ctx.set_ciphers("DHE-RSA-AES256-SHA:AES128-SHA:AES256-SHA")
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        if Path(dhparams).exists():
            ctx.load_dh_params(dhparams)
        return ctx

    async def run(self) -> None:
        module_app = web.Application()
        module_app.router.add_route("*", "/devices/{tail:.*}", self._route)
        state_app = web.Application()
        state_app.router.add_get("/state", self.state_handler)
        state_app.router.add_get("/health", self.health_handler)

        runner_state = web.AppRunner(state_app)
        await runner_state.setup()
        await web.TCPSite(runner_state, "127.0.0.1", self.state_port).start()

        runner_module = web.AppRunner(module_app)
        await runner_module.setup()
        # No keep-alive timeout tuning: the module holds connections for ~26 s
        # per dial and re-dials on its own; nothing here needs to outlive it.
        await web.TCPSite(runner_module, "0.0.0.0", self.listen_port,
                         ssl_context=self.tls_context()).start()

        timeout = ClientTimeout(total=30)  # Ayla answers in ~100 ms when healthy
        self.client = ClientSession(timeout=timeout, raise_for_status=False)
        print(f"icomm_local relay: module on :{self.listen_port} (TLS), "
              f"state on 127.0.0.1:{self.state_port}, upstream {self.upstream}")
        try:
            await asyncio.Event().wait()
        finally:
            await self.client.close()

    async def _route(self, request: web.Request) -> web.Response:
        """The module's three requests; telemetry gets decoded on the way."""
        if "superBlockDyn1" in request.path:
            return await self.telemetry(request)
        return await self.proxy(request)


if __name__ == "__main__":
    relay = Relay()
    try:
        asyncio.run(relay.run())
    except KeyboardInterrupt:
        pass
