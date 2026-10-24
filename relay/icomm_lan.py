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
import hmac
import json
import logging
import os
import random
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


def _build_key(secret: bytes, msg: bytes) -> bytes:
    """libada's client_lan_key_gen: HMAC(secret, HMAC(secret, msg) + msg)."""
    return hmac.digest(secret, hmac.digest(secret, msg, "sha256") + msg, "sha256")


class Direction:
    """One direction's keys: sign, AES-CBC encrypt, and the reverse."""

    def __init__(self, secret: bytes, msg: bytes) -> None:
        self.sign_key = _build_key(secret, msg + b"0")
        crypto_key = _build_key(secret, msg + b"1")
        self.cipher = AES.new(crypto_key, AES.MODE_CBC, _build_key(secret, msg + b"2")[:16])

    @staticmethod
    def _pad(data: bytes) -> bytes:
        # Zero padding, not PKCS7: that is what libada and AirCon both do.
        return data.ljust((len(data) + 15) // 16 * 16, bytes([0]))

    def encrypt_sign(self, payload: dict) -> dict:
        text = json.dumps(payload).encode()
        return {"enc": base64.b64encode(self.cipher.encrypt(self._pad(text))).decode(),
                "sign": base64.b64encode(hmac.digest(self.sign_key, text, "sha256")).decode()}

    def decrypt_validate(self, body: dict) -> dict:
        text = self.cipher.decrypt(base64.b64decode(body["enc"])).rstrip(bytes([0]))
        expect = base64.b64encode(hmac.digest(self.sign_key, text, "sha256")).decode()
        if not hmac.compare_digest(expect, body["sign"]):
            raise ValueError("bad LAN signature (wrong shared key?)")
        return json.loads(text.decode())


class Session:
    """Keys for one registered LAN session, from the key-exchange nonces."""

    def __init__(self, secret: bytes, exchange: dict) -> None:
        self.random_2 = "".join(random.choices(string.ascii_letters + string.digits, k=16))
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
        self._client: ClientSession | None = None
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
        app = web.Application()
        app.router.add_post("/local_lan/key_exchange.json", self._key_exchange)
        app.router.add_route("*", "/local_lan/commands.json", self._commands)
        app.router.add_route("*", "/local_lan/property/datapoint.json", self._datapoint)
        app.router.add_route("*", "/local_lan/property/datapoint/ack.json", self._datapoint)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", self.port).start()
        self._client = ClientSession(timeout=ClientTimeout(total=5))
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

    def status(self) -> dict:
        now = time.time()
        return {"enabled": self.enabled, "registered": self.registered,
                "session": self.session is not None, "module_ip": self.module_ip,
                "port": self.port, "key_id": self.key_id,
                "key_served": bool(self.key_served_at),
                "last_heartbeat_age": round(now - self.last_heartbeat, 1)
                if self.last_heartbeat else None,
                "last_update_age": round(now - self.last_update, 1)
                if self.last_update else None,
                "updates": self.updates, "writes_delivered": self.writes_delivered,
                "last_registration_result": self.last_registration_result,
                "registration_failures": self.registration_failures,
                "needs_key_push": self.needs_key_push}

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
            except (TimeoutError, OSError) as err:
                self.registration_failures += 1
                self.registered = False
                self.last_registration_result = type(err).__name__
                delay = 15.0
            await asyncio.sleep(delay)

    # -- the module-facing endpoints --------------------------------------
    async def _key_exchange(self, request: web.Request) -> web.Response:
        body = await request.json()
        exchange = body.get("key_exchange", {})
        if exchange.get("key_id") != self.key_id:
            _LOGGER.warning("LAN mode: key exchange for unknown key id %s "
                            "(module has a key we did not issue?)",
                            exchange.get("key_id"))
            return web.Response(status=404)
        self.session = Session(self.key.encode(), exchange)
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

    async def _commands(self, request: web.Request) -> web.Response:
        """The 10-second local poll: deliver one queued write, or nothing."""
        if self.session is None:
            return web.Response(status=400)
        self.seq_no += 1
        data: dict = {}
        write = self.relay.take_pending_for_lan()
        if write is not None:
            data = {"properties": [{"property": {
                "base_type": "string", "name": "setBlockParameter",
                "value": write.frame,
                "id": "".join(random.choices(string.ascii_letters + string.digits, k=8))}}]}
            self.writes_delivered += 1
            _LOGGER.warning("LAN mode: delivering %s = %s locally",
                            write.kind, write.value)
        return web.json_response(self.session.app.encrypt_sign(
            {"seq_no": self.seq_no, "data": data}))

    async def _datapoint(self, request: web.Request) -> web.Response:
        if self.session is None:
            return web.Response(status=400)
        try:
            update = self.session.dev.decrypt_validate(await request.json())
        except (ValueError, KeyError) as err:
            _LOGGER.warning("LAN mode: undecryptable update: %s", err)
            return web.Response(status=400)
        self.updates += 1
        self.last_update = time.time()
        self.exchanges_since_update = 0
        self.needs_key_push = False
        await self.relay.ingest_lan_update(update)
        return web.Response()
