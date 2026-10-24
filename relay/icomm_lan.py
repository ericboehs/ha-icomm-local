#!/usr/bin/env python3
"""Ayla Local Connect ("LAN mode") server for the iCOMM relay.

Phase 28 (research repo, captures INDEX) proved the whole path: when the relay
rewrites the module's registration reply to say `lan-enabled`, the agent asks
its cloud -- us -- for `GET /devices/<key>/lan.json` and accepts whatever key
the answer carries. Its LAN server then arms (`local_reg.json` 202), and the
module polls a *local* command endpoint every ~10 s while the app heartbeat
(`PUT /local_reg.json`, notify=1) keeps the registration alive. Writes ride
that poll; telemetry arrives as encrypted property updates. No Ayla account,
no cloud on the control path.

Protocol per Ayla's libada (client.c / lan.c, via the public Roomba source
dump), cross-checked against deiger/AirCon's independent implementation:

  key exchange   POST /local_lan/key_exchange.json
                 {"key_exchange":{ver:1,proto:1,random_1,time_1,key_id}}
                 -> {"random_2": <16 chars>, "time_2": <40-bit int>}
  session keys   per direction, HMAC-SHA256(secret, HMAC-SHA256(secret, msg)+msg)
                 with msg = random_2|random_1|time_2|time_1   (module -> app)
                       and  random_1|random_2|time_1|time_2   (app -> module)
                 suffix "0" = signing key, "1" = AES key, "2" = IV seed
  framing        {"enc": b64(AES-CBC(zero-pad(json))), "sign": b64(HMAC(sign_key, json))}
  commands       GET  /local_lan/commands.json -> encrypted {seq_no, data}
  telemetry      POST /local_lan/property/datapoint.json -> encrypted {seq_no, data}

The shared secret is generated here, not obtained from A.O. Smith. It is
persisted next to the write queue (0600) so a relay restart does not strand
the module on a key nobody knows: libada only fetches lan.json when it has
*no* stored key, and never wipes it over the network.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import string
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web
from Crypto.Cipher import AES

_LOGGER = logging.getLogger("icomm_lan")
LAN_IP_RE = re.compile(r"<lan-ip>([^<]+)</lan-ip>")
_ALNUM = string.ascii_letters + string.digits


def _build_key(secret: bytes, msg: bytes) -> bytes:
    """libada's client_lan_key_gen: HMAC(secret, HMAC(secret, msg) + msg)."""
    return hmac.digest(secret, hmac.digest(secret, msg, "sha256") + msg, "sha256")


class Direction:
    """One direction's keys: sign, AES-CBC encrypt, and the reverse.

    The CBC chain runs across messages for the whole session: each message's IV
    is the previous message's last ciphertext block. The chain is tracked
    explicitly, not left inside a long-lived cipher object, so that a message
    which fails authentication cannot advance it. With a stateful cipher, one
    garbage POST desynced every later real message (pre-publish review).
    """

    def __init__(self, secret: bytes, msg: bytes) -> None:
        self.sign_key = _build_key(secret, msg + b"0")
        self.crypto_key = _build_key(secret, msg + b"1")
        self.iv = _build_key(secret, msg + b"2")[:16]

    @staticmethod
    def _pad(data: bytes) -> bytes:
        # Zero padding, not PKCS7: that is what libada and AirCon both do.
        return data.ljust((len(data) + 15) // 16 * 16, bytes([0]))

    def encrypt_sign(self, payload: dict) -> dict:
        text = json.dumps(payload).encode()
        enc = AES.new(self.crypto_key, AES.MODE_CBC, self.iv).encrypt(self._pad(text))
        self.iv = enc[-16:]
        return {"enc": base64.b64encode(enc).decode(),
                "sign": base64.b64encode(hmac.digest(self.sign_key, text, "sha256")).decode()}

    def decrypt_validate(self, body: dict) -> dict:
        enc = base64.b64decode(body["enc"])
        if not enc or len(enc) % 16:
            raise ValueError("LAN ciphertext is not whole AES blocks")
        text = AES.new(self.crypto_key, AES.MODE_CBC, self.iv).decrypt(enc).rstrip(bytes([0]))
        expect = base64.b64encode(hmac.digest(self.sign_key, text, "sha256")).decode()
        if not hmac.compare_digest(expect, str(body["sign"])):
            raise ValueError("bad LAN signature (wrong shared key?)")
        self.iv = enc[-16:]         # advance the chain only for an authentic message
        return json.loads(text.decode())


class Session:
    """Keys for one registered LAN session, from the key-exchange nonces."""

    def __init__(self, secret: bytes, exchange: dict) -> None:
        self.random_2 = "".join(secrets.choice(_ALNUM) for _ in range(16))
        self.time_2 = time.monotonic_ns() % 2**40
        r1, r2 = exchange["random_1"].encode(), self.random_2.encode()
        t1 = str(exchange["time_1"]).encode()
        t2 = str(self.time_2).encode()
        self.app = Direction(secret, r1 + r2 + t1 + t2)   # us -> module
        self.dev = Direction(secret, r2 + r1 + t2 + t1)   # module -> us

    def response(self) -> dict:
        return {"random_2": self.random_2, "time_2": self.time_2}


class LanServer:
    """The relay's local doorbell: registration, command polls, telemetry."""

    def __init__(self, relay) -> None:
        self.relay = relay
        self.enabled = os.environ.get("LAN_MODE", "1") != "0"
        self.port = int(os.environ.get("LAN_PORT", "8888"))
        self.module_ip = os.environ.get("MODULE_IP", "") or None
        self.advertise_ip = os.environ.get("RELAY_IP", "") or None
        self.key_id = int(os.environ.get("LANIP_KEY_ID", "1"))
        self.key_file = Path(os.environ.get(
            "LANIP_FILE", str(relay.queue_file.parent / "lanip.json")))
        self.key = self._load_or_create_key()
        self.key_served_at = 0.0          # when we last answered lan.json
        self.registered = False
        self.session: Session | None = None
        # True once the current session has delivered one authenticated update:
        # proof the module holds our key, so LAN writes will not vanish.
        self.session_verified = False
        self.seq_no = 0
        self.updates = 0
        self.writes_delivered = 0
        self.last_update = 0.0
        self.last_heartbeat = 0.0
        self.last_registration_result = ""
        self.registration_failures = 0
        # Foreign-key detection: a module holding a key we did not issue still
        # registers (202) and starts key exchanges, but can never decrypt our
        # replies, so it re-exchanges every heartbeat and sends no updates.
        self.exchanges_since_update = 0
        self.needs_key_push = False
        # A one-shot reverse-REST read asks the MCU for current telemetry.
        # Spontaneous superBlockDyn1 reports can be hours apart on an idle tank.
        self.refresh_pending = False
        self.refresh_cmd_id = 0
        self.telemetry_requests = 0
        self.last_refresh_request = 0.0
        # Keep-alive reads: an idle tank can go hours without reporting, and the
        # relay calls telemetry older than 10 min stale -- Home Assistant then
        # marks the entity unavailable and silently drops writes, although the
        # LAN session is fine. One read-only MCU read per interval prevents it.
        self.refresh_interval = float(os.environ.get("LAN_REFRESH_INTERVAL", "300"))
        self._client: ClientSession | None = None
        self._wake_event: asyncio.Event | None = None
        self.wakes = 0
        # Stall detection. Live 2026-10-23 23:50: after an app (cloud) write the
        # module kept answering our PUT heartbeats with 202 but stopped polling
        # commands.json for 5+ minutes. A fresh POST registration (new LAN
        # session, key exchange) resumed it within a second.
        self.last_poll = 0.0
        self.stall_recoveries = 0
        self.stall_after = float(os.environ.get("LAN_STALL_AFTER", "60"))
        self._task: asyncio.Task | None = None
        self._runner: web.AppRunner | None = None

    # -- the shared secret ---------------------------------------------
    def _load_or_create_key(self) -> str:
        """Persist our own key; the module will not replace one it already has.

        LANIP_KEY overrides for development. Otherwise reuse the file, or mint
        a key once. Never logged, and the file is 0600.
        """
        if not self.enabled:
            return ""
        override = os.environ.get("LANIP_KEY", "")
        if override:
            return override
        try:
            saved = json.loads(self.key_file.read_text())
            if saved.get("lanip_key"):
                self.key_id = int(saved.get("lanip_key_id", self.key_id))
                # The module's address is only announced at re-registration,
                # which can be hours after a relay restart: remember it.
                self.module_ip = self.module_ip or saved.get("module_ip")
                return saved["lanip_key"]
        except (OSError, ValueError):
            pass
        key = secrets.token_urlsafe(21)       # 28 chars; libada caps at 32
        try:
            self.key_file.parent.mkdir(parents=True, exist_ok=True)
            self.key_file.write_text(json.dumps(
                {"lanip_key": key, "lanip_key_id": self.key_id}))
            os.chmod(self.key_file, 0o600)
            _LOGGER.info("LAN mode: generated a new shared key at %s", self.key_file)
        except OSError as err:
            _LOGGER.warning("LAN mode: key not persisted (%s); a restart will strand "
                            "a module that already learned it", err)
        return key

    def _persist(self) -> None:
        """Rewrite the key file with the learned module address (still 0600)."""
        if os.environ.get("LANIP_KEY"):
            return                      # dev override: nothing of ours on disk
        try:
            self.key_file.write_text(json.dumps({
                "lanip_key": self.key, "lanip_key_id": self.key_id,
                "module_ip": self.module_ip}))
            os.chmod(self.key_file, 0o600)
        except OSError as err:
            _LOGGER.warning("LAN mode: could not persist module address: %s", err)

    def lan_json(self) -> dict:
        """Answer the agent's GET /devices/<key>/lan.json with our own key."""
        self.key_served_at = time.time()
        _LOGGER.warning("LAN mode: module fetched lan.json; answered with our key "
                        "(id %s)", self.key_id)
        return {"lanip": {"status": "enable", "lanip_key_id": self.key_id,
                          "lanip_key": self.key, "keep_alive": 30, "auto_sync": 1}}

    def learn_module_ip(self, dsn_xml: str) -> None:
        """The registration doc carries the module's own LAN address."""
        found = LAN_IP_RE.search(dsn_xml)
        if found and found.group(1) != self.module_ip:
            self.module_ip = found.group(1)
            _LOGGER.info("LAN mode: module is at %s", self.module_ip)
            self._persist()

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        if not self.enabled:
            return
        # Only the module itself may call back: anyone else could reset the
        # session with a key exchange or drain queued writes by polling.
        peers = self.relay.peers
        app = web.Application(middlewares=[peers.middleware(peers.lan_allowed, f":{self.port}")])
        app.router.add_post("/local_lan/key_exchange.json", self._key_exchange)
        app.router.add_route("*", "/local_lan/commands.json", self._commands)
        app.router.add_route("*", "/local_lan/property/datapoint.json", self._datapoint)
        app.router.add_route("*", "/local_lan/property/datapoint/ack.json", self._datapoint)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", self.port).start()
        self._client = ClientSession(timeout=ClientTimeout(total=5))
        self._wake_event = asyncio.Event()
        self._task = asyncio.create_task(self._registration_loop())
        _LOGGER.info("LAN mode: listening on :%s (module %s)", self.port,
                     self.module_ip or "learned at registration")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._client:
            await self._client.close()
        if self._runner:
            await self._runner.cleanup()

    def _check_stall(self) -> None:
        """Heartbeats accepted but no polls: drop to a fresh POST registration."""
        if not self.registered or not self.last_poll or self.stall_after <= 0:
            return
        quiet = time.time() - self.last_poll
        if quiet > self.stall_after:
            self.stall_recoveries += 1
            self.registered = False
            self.last_poll = time.time()     # one re-registration per stall window
            _LOGGER.warning("LAN mode: heartbeats accepted but no command poll for "
                            "%.0f s; re-registering to reset the module's LAN session",
                            quiet)

    def wake(self) -> None:
        """Send the next heartbeat now: notify=1 makes the module poll at once."""
        if self._wake_event is not None and self.registered:
            self.wakes += 1
            self._wake_event.set()

    async def _pause(self, delay: float) -> None:
        if self._wake_event is None:
            await asyncio.sleep(delay)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake_event.wait(), delay)
        self._wake_event.clear()

    def status(self) -> dict:
        now = time.time()
        return {"enabled": self.enabled, "registered": self.registered,
                "session": self.session is not None,
                "session_verified": self.session_verified, "module_ip": self.module_ip,
                "port": self.port, "key_id": self.key_id,
                "key_served": bool(self.key_served_at),
                "last_heartbeat_age": round(now - self.last_heartbeat, 1)
                if self.last_heartbeat else None,
                "last_update_age": round(now - self.last_update, 1)
                if self.last_update else None,
                "updates": self.updates, "writes_delivered": self.writes_delivered,
                "last_registration_result": self.last_registration_result,
                "registration_failures": self.registration_failures,
                "needs_key_push": self.needs_key_push,
                "refresh_pending": self.refresh_pending,
                "telemetry_requests": self.telemetry_requests, "wakes": self.wakes,
                "last_poll_age": round(now - self.last_poll, 1) if self.last_poll else None,
                "stall_recoveries": self.stall_recoveries}

    # -- registration: the app's heartbeat, from libada's client side -----
    def _registration_body(self) -> dict:
        if not self.advertise_ip and self.module_ip:
            # The address the module should call back on: ours, on its subnet.
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect((self.module_ip, 80))
                self.advertise_ip = sock.getsockname()[0]
            finally:
                sock.close()
        return {"local_reg": {"ip": self.advertise_ip, "port": self.port,
                              "uri": "/local_lan", "notify": 1}}

    async def _registration_loop(self) -> None:
        """POST once, then PUT every ~10 s; the module polls us per heartbeat."""
        while True:
            delay = 10.0
            try:
                if not self.module_ip:
                    delay = 5.0
                else:
                    self._check_stall()
                    body = self._registration_body()
                    method = "PUT" if self.registered else "POST"
                    assert self._client is not None
                    async with self._client.request(
                            method, f"http://{self.module_ip}/local_reg.json",
                            json=body) as response:
                        self.last_registration_result = f"{method} {response.status}"
                        if response.status == 202:
                            if not self.registered:
                                _LOGGER.warning("LAN mode: registered with the module")
                            self.registered = True
                            self.last_heartbeat = time.time()
                        else:
                            if self.registered:
                                _LOGGER.warning("LAN mode: registration lost (%s)",
                                                self.last_registration_result)
                            self.registered = False
                            # 404 = LAN not armed (needs the rewritten DSN reply),
                            # 412 = armed but the module has no key yet.
                            delay = 15.0
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 -- this loop must never die
                # aiohttp's ServerDisconnectedError and ClientPayloadError are not
                # OSError. Catching only (TimeoutError, OSError) let one of them
                # end the task silently, leaving LAN mode dead until a restart.
                self.registration_failures += 1
                if self.registered or self.last_registration_result != type(err).__name__:
                    _LOGGER.warning("LAN mode: heartbeat failed: %s: %s",
                                    type(err).__name__, err)
                self.registered = False
                self.last_registration_result = type(err).__name__
                delay = 15.0
            await self._pause(delay)

    # -- the module-facing endpoints --------------------------------------
    async def _key_exchange(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            exchange = body.get("key_exchange") or {}
            if not isinstance(exchange, dict):
                raise ValueError
        except (ValueError, AttributeError):
            return web.Response(status=400)
        if exchange.get("key_id") != self.key_id:
            _LOGGER.warning("LAN mode: key exchange for unknown key id %r "
                            "(module has a key we did not issue?)",
                            exchange.get("key_id"))
            return web.Response(status=404)
        try:
            self.session = Session(self.key.encode(), exchange)
        except (KeyError, TypeError, AttributeError):
            return web.Response(status=400)
        self.session_verified = False
        self.last_poll = time.time()       # a new session counts as contact
        self.request_telemetry()       # do not wait hours after a relay restart
        self.exchanges_since_update += 1
        if self.exchanges_since_update >= 3 and not self.needs_key_push:
            # Three exchanges, no update between them: the module's key is
            # not ours. Ask the relay to push ours with Ayla's PUT lanip.json.
            self.needs_key_push = True
            _LOGGER.warning("LAN mode: module keeps re-exchanging with no updates "
                            "-- it holds a key we did not issue; pushing ours on "
                            "the next cloud poll")
        else:
            _LOGGER.info("LAN mode: key exchange accepted; session keys derived")
        return web.json_response(self.session.response())

    def request_telemetry(self) -> None:
        """Ask for one fresh superBlockDyn1 value on a future commands poll.

        libada's GET /property.json?name=... handler calls ada_prop_mgr_get(),
        which obtains the value from the host MCU. Its response comes back to
        our normal datapoint endpoint, encrypted like a spontaneous update.
        """
        self.refresh_pending = True

    def _refresh_due(self) -> bool:
        if self.refresh_interval <= 0:
            return False
        now = time.time()
        newest = max(self.last_update, self.relay.last_telemetry or 0.0)
        return (now - newest > self.refresh_interval
                and now - self.last_refresh_request > self.refresh_interval)

    async def _commands(self, request: web.Request) -> web.Response:
        """The 10-second local poll: deliver one write or telemetry request."""
        if self.session is None:
            return web.Response(status=400)
        self.last_poll = time.time()
        self.seq_no += 1
        data: dict = {}
        write = self.relay.take_pending_for_lan()
        if write is not None:
            write.command_id = "".join(secrets.choice(_ALNUM) for _ in range(8))
            self.relay._save_queue()
            data = {"properties": [{"property": {
                "base_type": "string", "name": "setBlockParameter",
                "value": write.frame, "id": write.command_id}}]}
            self.writes_delivered += 1
            _LOGGER.warning("LAN mode: delivering %s = %s locally",
                            write.kind, write.value)
        elif self.refresh_pending or self._refresh_due():
            self.refresh_pending = False       # one shot; never retry in a loop
            self.last_refresh_request = time.time()
            self.refresh_cmd_id += 1
            self.telemetry_requests += 1
            data = {"cmds": [{"cmd": {
                "method": "GET",
                "resource": "property.json?name=superBlockDyn1",
                "data": "", "cmd_id": self.refresh_cmd_id,
                "uri": "/local_lan/property/datapoint.json"}}]}
            _LOGGER.info("LAN mode: requesting fresh telemetry (cmd %d)",
                         self.refresh_cmd_id)
        return web.json_response(self.session.app.encrypt_sign(
            {"seq_no": self.seq_no, "data": data}))

    async def _datapoint(self, request: web.Request) -> web.Response:
        if self.session is None:
            return web.Response(status=400)
        try:
            update = self.session.dev.decrypt_validate(await request.json())
        except (ValueError, KeyError, TypeError) as err:
            _LOGGER.warning("LAN mode: undecryptable update: %r", err)
            return web.Response(status=400)
        if not self.session_verified and self.relay.pending:
            # A write queued while the session was re-keying waited for this
            # proof; deliver it now rather than on the next ~10 s heartbeat.
            self.session_verified = True
            self.wake()
        self.session_verified = True
        self.updates += 1
        self.last_update = time.time()
        self.exchanges_since_update = 0
        self.needs_key_push = False
        await self.relay.ingest_lan_update(update)
        return web.Response()
