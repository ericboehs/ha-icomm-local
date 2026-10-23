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

  * the TLS listener the module connects to (LISTEN_PORT, default 8444; any port
    works because the router's DNAT rule chooses it). It serves a self-signed
    RSA-2048/SHA-256 leaf. The module checks nothing about that certificate --
    not the chain, not the name, not the SAN: a leaf with CN=garbage.example and
    no SAN at all was accepted on the first handshake. The only real constraint
    is RSA/SHA-256 (an ECDSA P-384 leaf silently deadlocks the handshake). See
    github.com/ericboehs/aosmith-water-heater, ARCHITECTURE.md.
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
import contextlib
import os
import re
import ssl
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

import icomm_frames as frames
from aiohttp import ClientSession, ClientTimeout, web

TEMP_F = 1.8 / 256.0


@dataclass
class PendingWrite:
    """A write waiting for the appliance to ask for it."""

    kind: str          # "setpoint" or "mode"
    value: object      # degrees F, or the mode name
    frame: str         # the encoded frame, in the spaced hex Ayla uses
    queued: float = field(default_factory=time.time)
    attempts: int = 0
    delivered: float | None = None
    confirmed: float | None = None
    replaced_upstream: bool | None = None
    superseded: int | None = None

# The register atlas, measured field by field. Offsets are into the five blocks
# of the module's superBlockDyn1 telemetry (block lengths 8/146/230/52/52;
# block[0] is a header). icomm/README.md in the research repo is the evidence.
MODE_PAIRS = {(100, 1): "electric", (0, 4): "hybrid", (0, 5): "heat_pump"}

# How long silence stays plausible with no evidence to the contrary. Six hours is
# the appliance's own measured reporting interval; being generous is deliberate,
# because a false alarm trains the user to ignore the real one.
IDLE_INTERVAL_DEFAULT = 6 * 3600
REPORTING_INTERVAL = re.compile(
    r"<name>\s*superBlockDynDataUpdateRate\s*</name>\s*<value>\s*(\d+)\s*</value>")


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
        # Writes wait here until the appliance asks for them. Only the newest
        # request per register survives: the appliance applies one frame at a
        # time, and delivering a stale queued value after a newer one is a bug.
        self.pending: dict[str, PendingWrite] = {}
        self.last_write: dict | None = None
        self.writes_delivered = 0
        # The local doorbell: answering once with 401 makes the module
        # re-register (measured), and a re-registration fetches the full
        # configuration -- which carries the setBlockParameter property a write
        # rides on. Left at 0 unless someone asks for a nudge.
        self.nudges_left = 0
        self.nudge_on_telemetry = False
        self.debug_requests = os.environ.get("DEBUG_REQUESTS", "") == "1"
        # Conversation shape, for the degraded-state detector: the module talks in
        # dials, so requests are grouped into them and their shape remembered.
        self.telemetry_times: deque[float] = deque(maxlen=12)
        self.dials: deque[dict] = deque(maxlen=6)
        self._dial: dict | None = None
        # The appliance advertises how often it reports; learned from the config
        # reply. Until then assume nothing, and treat silence generously.
        self.reporting_interval_s: int | None = None
        self.writes_confirmed = 0
        self.injections_held = 0
        # Pinned Entrust G2 for upstream verification: certifi dropped it, so
        # default verification fails against Ayla's ingress. Public CA cert.
        ca = Path(__file__).parent / "upstream-entrust-g2.pem"
        # aiohttp wants an SSLContext (a bare path string raises on use).
        self.upstream_ssl = ssl.create_default_context(cafile=str(ca)) if ca.exists() else None

    # -- the module-facing side ---------------------------------------
    async def proxy(self, request: web.Request,
                    transform=None) -> web.Response:
        """Forward a request upstream verbatim, optionally rewriting the reply.

        The appliance's cloud view must stay intact -- the mobile app depends on
        it -- so replies are returned exactly as Ayla sent them, with one
        exception: a command poll may carry a locally queued write instead of
        (never in addition to) whatever Ayla had queued for it.
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
                if upstream.status == 200 and "commands.xml" in request.path:
                    self._learn_reporting_interval(payload.decode(errors="replace"))
                if transform is not None and upstream.status == 200:
                    rewritten = transform(payload)
                    if rewritten is not None:
                        payload = rewritten
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
                # A synthetic empty reply is the only one Ayla's outage gives us;
                # it is also a perfectly good carrier for a queued write.
                synthetic = ("<commands>\n  <properties/>\n  <schedules/>\n"
                             "  <cmds/>\n</commands>\n")
                if transform is not None:
                    rewritten = transform(synthetic.encode())
                    if rewritten is not None:
                        synthetic = rewritten.decode(errors="replace")
                return web.Response(status=200, text_type="application/xml",
                                    text=synthetic)
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
            self._confirm_pending()
        return await self.proxy(request)

    # -- the write path ------------------------------------------------
    def queue_write(self, kind: str, value) -> PendingWrite:
        """Accept a write, clamped to what the appliance itself allows.

        The limits come from the appliance's telemetry, not from a hard-coded
        policy: this relay must not be the reason a tank is asked to do something
        its own controller advertises as out of range.
        """
        if kind == "setpoint":
            ceiling = min(float(self.state.get("max_setpoint") or 150.0),
                          frames.HARD_MAX_SETPOINT)
            value = float(value)
            if not frames.MIN_SETPOINT <= value <= ceiling:
                raise ValueError(
                    f"setpoint {value} outside {frames.MIN_SETPOINT}-{ceiling} F")
            payload_frame = frames.as_text(frames.setpoint_frame(value))
        elif kind == "mode":
            value = str(value)
            payload_frame = frames.as_text(frames.mode_frame(value))
        else:
            raise ValueError(f"unknown write kind {kind!r}")

        superseded = 0
        if kind in self.pending:
            superseded = (self.pending[kind].superseded or 0) + 1
        write = PendingWrite(kind=kind, value=value, frame=payload_frame,
                             superseded=superseded or None)
        self.pending[kind] = write
        return write

    def _confirm_pending(self) -> None:
        """Retire a write once telemetry shows it took effect.

        Confirmation is the appliance's own report, never our own ack: a frame can
        be delivered and ignored (the BLE register at block 26 is exactly that).
        """
        confirmed = []
        for kind, write in self.pending.items():
            if kind == "setpoint":
                got = self.state.get("setpoint")
                took = got is not None and abs(float(got) - float(write.value)) <= 0.5
            else:
                took = self.state.get("mode") == write.value
            if took:
                write.confirmed = time.time()
                self.writes_confirmed += 1
                confirmed.append(kind)
        for kind in confirmed:
            write = self.pending.pop(kind)
            self.last_write = {**asdict(write), "outcome": "confirmed"}

    def _inject(self, payload: bytes) -> bytes | None:
        """Put the oldest queued write into a command reply, if it is safe to.

        Gated on freshness on purpose: an appliance in its degraded state ignores
        queued commands entirely (and the cure for that is a breaker flip, not
        more writes), so holding is the honest response rather than pretending a
        write was delivered into a hole.
        """
        if not self.pending:
            return None
        if not self.last_telemetry or time.time() - self.last_telemetry > 600:
            self.injections_held += 1
            return None
        if self.health()["state"] == "degraded":
            # Injecting into a module that has stopped listening starves silently
            # and looks like a write bug. Hold, and let the state API say why.
            self.injections_held += 1
            return None
        write = self.pending[sorted(self.pending)[0]]
        text = payload.decode(errors="replace")
        rewritten, replaced = frames.rewrite_commands(text, write.frame)
        if rewritten == text:
            return None
        write.attempts += 1
        write.delivered = time.time()
        write.replaced_upstream = replaced
        self.writes_delivered += 1
        return rewritten.encode()

    # -- the Home-Assistant-facing side --------------------------------
    async def write_handler(self, request: web.Request) -> web.Response:
        """POST /write {"setpoint": 140} or {"mode": "heat_pump"}.

        Queues, never delivers: delivery happens on the appliance's own schedule,
        when it asks for commands. A write that is queued is not a write that
        happened, so the response says `queued`, and `GET /state` reports whether
        the appliance ever confirmed it.
        """
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "expected JSON"}, status=400)
        kind = "setpoint" if "setpoint" in body else "mode" if "mode" in body else None
        if kind is None:
            return web.json_response({"error": "give setpoint or mode"}, status=400)
        try:
            write = self.queue_write(kind, body[kind])
        except ValueError as err:
            return web.json_response({"error": str(err)}, status=400)
        return web.json_response({"queued": asdict(write), "pending": len(self.pending)})

    async def write_cancel_handler(self, request: web.Request) -> web.Response:
        dropped = {kind: asdict(w) for kind, w in self.pending.items()}
        self.pending.clear()
        return web.json_response({"cancelled": dropped})

    async def state_handler(self, request: web.Request) -> web.Response:
        out = dict(self.state)
        age = round(time.time() - self.last_telemetry, 1) if self.last_telemetry else None
        out["last_telemetry_age"] = age
        out["upstream_errors"] = self.upstream_errors
        out["pending"] = {
            kind: {k: v for k, v in asdict(write).items() if v is not None}
            for kind, write in self.pending.items()
        }
        out["last_write"] = self.last_write
        out["health"] = self.health()
        out["writes_delivered"] = self.writes_delivered
        out["writes_confirmed"] = self.writes_confirmed
        out["injections_held"] = self.injections_held
        # A stale tank is one that has not posted in 10 minutes: the module is
        # event-driven, and a satisfied tank goes quiet for long stretches.
        out["fresh"] = bool(self.last_telemetry) and (time.time() - self.last_telemetry) < 600
        return web.json_response(out)

    async def health_handler(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "uptime": round(time.time() - self.started),
                                  "upstream": self.upstream})

    # -- lifecycle ------------------------------------------------------
    def tls_context(self) -> ssl.SSLContext:
        certs = os.environ.get("CERT_DIR", "certs")
        cert = os.environ.get("RELAY_CERT", f"{certs}/aylanetworks.com.crt")
        key = os.environ.get("RELAY_KEY", f"{certs}/aylanetworks.com.key")
        dhparams = os.environ.get("RELAY_DHPARAMS", f"{certs}/dhparams.pem")
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
        state_app.router.add_post("/nudge", self.nudge_handler)
        state_app.router.add_post("/write", self.write_handler)
        state_app.router.add_post("/write/cancel", self.write_cancel_handler)

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

    # -- degraded-state detection --------------------------------------
    def _note_request(self, path: str) -> None:
        """Track the shape of the conversation: dials, their requests, and polls.

        A healthy module does at least two things per dial (telemetry plus a poll)
        and keeps coming back. A degraded one does exactly one telemetry POST per
        dial on a ~4 minute cadence, and polls never happen -- which is why
        injections starve silently in that state.
        """
        now = time.time()
        if self.debug_requests:
            # Off by default: the log is for diagnosing a conversation, and every
            # request in it is one more chance to leak a DSN into a log file.
            print(f"  {now:.0f} {path}", flush=True)
        if self._dial is None or now - self._dial["last"] > 10.0:
            if self._dial is not None:
                self.dials.append(self._dial)
            self._dial = {"start": now, "last": now, "requests": 0, "polls": 0}
        self._dial["last"] = now
        self._dial["requests"] += 1
        if "commands.xml" in path:
            self._dial["polls"] += 1
        if "superBlockDyn1" in path:
            self.telemetry_times.append(now)

    def _learn_reporting_interval(self, xml: str) -> None:
        """Pick up `superBlockDynDataUpdateRate` (seconds) from a config reply.

        Measured at 21601 -- about six hours -- which is why a satisfied tank can be
        legitimately silent for a long time. Treating that silence as a fault is how
        a detector cries wolf; the appliance tells us its own cadence, so use it.
        """
        found = REPORTING_INTERVAL.search(xml)
        if found:
            with contextlib.suppress(ValueError):
                self.reporting_interval_s = int(found.group(1))

    def _calling_for_heat(self) -> bool:
        water = self.state.get("water_top")
        setpoint = self.state.get("setpoint")
        if water is None or setpoint is None:
            return False
        return float(setpoint) - float(water) >= 2.0

    def _dial_shape(self) -> list[dict]:
        dials = list(self.dials)
        if self._dial is not None:
            dials.append(self._dial)
        return dials

    def health(self) -> dict:
        """Is the appliance behaving, and if not, what should a human do?

        The hard part is that silence is usually not a fault. This appliance reports
        on events and on its own interval (`superBlockDynDataUpdateRate`, measured at
        ~6 hours), so a satisfied tank that says nothing for hours is normal. Calling
        that degraded would train the user to ignore the one alarm that matters.

        So a fault needs positive evidence: silence while calling for heat, a queue
        that cannot drain, dials that shrink to a single request, or silence far
        beyond the interval the appliance itself advertised.

        Only symptoms in the relay's view are used. Ops keepalives ride UDP straight
        through the gateway -- measured present during a long HTTPS silence -- and the
        GraphQL message belongs to the app's API, so both are listed as unobservable
        rather than guessed at.
        """
        now = time.time()
        dials = self._dial_shape()
        telemetry = list(self.telemetry_times)
        gaps = [b - a for a, b in zip(telemetry, telemetry[1:], strict=False)]
        median_gap = sorted(gaps)[len(gaps) // 2] if gaps else None
        recent = dials[-3:]
        single_request_dials = sum(1 for d in recent if d["requests"] == 1)
        polls = sum(d["polls"] for d in dials)
        silent = now - self.last_telemetry if self.last_telemetry else None
        interval = self.reporting_interval_s or IDLE_INTERVAL_DEFAULT
        evidence = {
            "dials": len(dials),
            "polls": polls,
            "median_telemetry_gap_s": None if median_gap is None else round(median_gap),
            "silent_s": None if silent is None else round(silent),
            "reporting_interval_s": interval,
        }
        unobservable = ["ops keepalives (UDP, gateway-side)", "GraphQL write status (app API)"]

        def degraded(reason: str, remediation: str, extra: dict | None = None) -> dict:
            return {"state": "degraded", "reasons": [reason], "remediation": remediation,
                    "evidence": {**evidence, **(extra or {})}, "unobservable": unobservable}

        breaker = ("Flip the water heater's breaker off for ~30 seconds, then on. It "
                   "re-registers and fetches everything Ayla had queued, so nothing is "
                   "lost. Retrying writes cannot fix this state.")
        wait = (f"Wait: the appliance reports on its own schedule (~{interval} s). If it is "
                "calling for heat and still silent, reconnect it in the router's client "
                "list, or flip the breaker.")

        # Positive evidence first: silence while it should be busy.
        if silent is not None and silent > 600 and self._calling_for_heat():
            return degraded(
                f"silent {silent / 60:.0f} min while calling for heat "
                f"({self.state.get('setpoint')} F setpoint, "
                f"{self.state.get('water_top')} F water)",
                breaker, {"calling_for_heat": True})

        # Silence well beyond the interval it advertised for itself.
        if silent is not None and silent > interval + 900:
            return degraded(
                f"silent {silent / 3600:.1f} h, beyond its own ~{interval / 3600:.1f} h "
                f"reporting interval", breaker)

        # A queue that cannot drain while the module is otherwise quiet.
        if self.pending and silent is not None and silent > 900:
            return degraded(
                "a write is queued and the appliance has not asked for commands",
                wait)

        # The researched degraded signature: one request per dial, minutes apart.
        if (polls == 0 and single_request_dials == len(recent)
                and median_gap is not None and median_gap > 150):
            return degraded(
                f"{single_request_dials}/{len(recent)} dials carried a single request, "
                f"telemetry every ~{median_gap:.0f}s, no polls", breaker)

        if not self.last_telemetry:
            if now - self.started > 900:
                return degraded("no telemetry since the relay started",
                                "Check that the module is steered here, then wait: an "
                                "idle appliance can be silent for hours.")
            return {"state": "unknown", "reasons": ["nothing heard yet"],
                    "evidence": evidence, "unobservable": unobservable}

        if len(dials) < 3:
            return {"state": "unknown", "reasons": ["too few dials observed yet"],
                    "evidence": evidence, "unobservable": unobservable}

        # Silence, but a plausible reason for it.
        if silent is not None and silent > 900:
            return {"state": "idle",
                    "reasons": [f"quiet for {silent / 60:.0f} min; reports every "
                                f"~{interval} s or on events"],
                    "evidence": evidence, "unobservable": unobservable}

        return {"state": "healthy", "reasons": [], "evidence": evidence,
                "unobservable": unobservable}

    async def nudge_handler(self, request: web.Request) -> web.Response:
        """POST /nudge -- ask the module to re-register, without the cloud.

        Delivery otherwise waits for Ayla to notify the module that a command
        exists. A 401 on a normal request sends it through re-registration
        instead, whose configuration fetch we can answer with a queued write.
        Kept explicit and counted: churning a module that self-heals slowly is
        not something to do on a timer.
        """
        try:
            body = await request.json()
        except ValueError:
            body = {}
        self.nudges_left = int(body.get("attempts", 1))
        self.nudge_on_telemetry = bool(body.get("on_telemetry", False))
        return web.json_response({"nudges_left": self.nudges_left,
                                  "on_telemetry": self.nudge_on_telemetry})

    async def _route(self, request: web.Request) -> web.Response:
        """The module's requests; telemetry gets decoded, polls may get a write."""
        self._note_request(request.path)
        if "superBlockDyn1" in request.path:
            if self.nudges_left and self.nudge_on_telemetry:
                # An idle module only ever POSTs telemetry, so this is the only
                # request a 401 can reach. Its cue to re-register -- and a
                # re-registration fetches the configuration a write rides on.
                self.nudges_left -= 1
                return web.Response(status=401, text="")
            return await self.telemetry(request)
        if "commands.xml" in request.path:
            return await self.proxy(request, transform=self._inject)
        if self.nudges_left and request.method == "GET":
            self.nudges_left -= 1
            # No body, and nothing forwarded: this is the module's cue to
            # re-register, not a failure to paper over.
            return web.Response(status=401, text="")
        return await self.proxy(request)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(Relay().run())
