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

Everything is forwarded upstream verbatim, with three narrow exceptions:
a queued write may replace the setBlockParameter in a commands.xml reply (the
cloud fallback path); the DSN registration reply gets Ayla's two Local Connect
switches flipped on; and lan.json / the lanip ack are answered locally with the
relay's own key. Local Connect (icomm_lan.py) is the primary write path. The
module-facing listeners answer only the module and the gateway (icomm_peers.py).

Run:  python3 icomm_relay.py   (env below; see README for the full setup)

Env:
  RELAY_CERT / RELAY_KEY   TLS leaf (see gen-cert.sh)
  LISTEN_PORT=8444         port the module is steered to
  STATE_PORT=8124          localhost state API
  UPSTREAM=proxy-ingress-usfield.aylanetworks.com
  DEVICE_DSN=              optional, tags state with the DSN when known
  LAN_MODE=1               Ayla Local Connect server (the local doorbell)
  LAN_PORT=8888            port the module calls back on
  MODULE_IP=               optional; otherwise learned from the DSN reply
  RELAY_IP=                optional; address advertised for LAN callbacks
  LANIP_FILE=              shared-key store (default: next to the write queue)
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import os
import re
import ssl
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

import icomm_frames as frames
from aiohttp import ClientSession, ClientSSLError, web
from icomm_dns import DnsServer
from icomm_firmware import FirmwareGate
from icomm_heater import Heater, known_dsn, write_private
from icomm_lan import LanServer
from icomm_peers import TRUSTED, PeerPolicy, normalize
from yarl import URL

_LOGGER = logging.getLogger("icomm_relay")

TEMP_F = 1.8 / 256.0
# Deliveries per write, across both paths. Each delivery is acked or not; past
# this the appliance is accepting the frame without applying it, and more copies
# only crowd out the mobile app's own commands.
MAX_ATTEMPTS = 3
# An exhausted write stays visible (pending, with its status) this long, so a
# late report can still confirm it, then it is retired to last_write.
RETIRE_AFTER_S = 900



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
    restored: bool = False
    # Lifecycle evidence. Delivery and acknowledgement are progress, never success:
    # only the tank's own telemetry confirms a write.
    delivery_path: str | None = None    # "lan" or "cloud"
    command_id: str | None = None       # LAN property id echoed back in the ack
    acked: float | None = None
    ack_status: int | None = None
    reports_after_ack: int = 0          # fresh telemetry since the ack...
    observed_after_ack: object = None   # ...and what the tank actually reported
    days: int | None = None             # vacation length, for a vacation write

    @property
    def status(self) -> str:
        """queued -> delivered -> acknowledged -> confirmed (or not_applied)."""
        if self.confirmed:
            return "confirmed"
        if self.acked and self.ack_status not in (None, 200):
            return "rejected"           # the module answered the property with an error
        if self.acked and self.reports_after_ack and self.attempts >= MAX_ATTEMPTS:
            return "not_applied"        # acked 3x, the tank kept reporting the old value
        if self.acked:
            return "acknowledged"
        if self.delivered:
            return "delivered"
        return "queued"

    def public(self) -> dict:
        out = {k: v for k, v in asdict(self).items() if v is not None}
        out["status"] = self.status
        return out

# The register atlas, measured field by field. Offsets are into the five blocks
# of the module's superBlockDyn1 telemetry (block lengths 8/146/230/52/52;
# block[0] is a header). icomm/README.md in the research repo is the evidence.
MODE_PAIRS = {(100, 1): "electric", (0, 4): "hybrid", (0, 5): "heat_pump"}


def mode_name(pair: tuple[int, int]) -> str:
    """The mode register pair as a name. Vacation is (days, 2)."""
    if pair in MODE_PAIRS:
        return MODE_PAIRS[pair]
    return "vacation" if pair[1] == frames.VACATION else "unknown"

# How long silence stays plausible with no evidence to the contrary. Six hours is
# the appliance's own measured reporting interval; being generous is deliberate,
# because a false alarm trains the user to ignore the real one.
IDLE_INTERVAL_DEFAULT = 6 * 3600
# No cloud request for this long while Local Connect delivers changing reports:
# the module's cloud side has stopped (health: degraded, code cloud_idle).
CLOUD_IDLE_AFTER = int(os.environ.get("CLOUD_IDLE_AFTER", "1800"))
# The cloud-echo probe (phase 53). A healthy module passes every LAN
# reporting-mask write on to the cloud (POST periodicParameterSetting?echo=true)
# about 100 ms before it acks it over LAN. After a long relay outage (a host
# reboot) it acks with no echo: its cloud side is down until its Wi-Fi drops or
# it's power-cycled. So: send the mask when the cloud has been quiet a while,
# and an ack with no cloud request since the send means the cloud side is down.
CLOUD_PROBE_FIRST_S = 300            # after start, if no cloud request has come yet
CLOUD_PROBE_GRACE_S = 15             # after the ack, for a late echo
CLOUD_PROBE_ACK_WAIT_S = 120         # an unacked probe blocks the next one this long
REPORTING_INTERVAL = re.compile(
    r"<name>\s*superBlockDynDataUpdateRate\s*</name>\s*<value>\s*(\d+)\s*</value>")


# Hot-water availability, as A.O. Smith's cloud computes it: their 0/50/100 %
# follows the controller's own energy-deficit counter (block 4 offset 24, Wh),
# which falls at roughly the 4500 W element's 75 Wh per minute (76/min over a
# 40-minute recovery). Matched against 33
# cloud-labelled captures (research phase 31): 4950-5100 Wh read 0 %, 906-4800
# read 50 %, 0-450 read 100 %. The 0->50 edge landed exactly as the counter went
# 4950 -> 4800. The 50->100 edge is somewhere in 450-906 and is not pinned yet,
# so the midpoint is used.
AVAILABILITY_EMPTY_WH = 4875
AVAILABILITY_FULL_WH = 678


def availability(deficit_wh: int) -> int:
    if deficit_wh >= AVAILABILITY_EMPTY_WH:
        return 0
    return 100 if deficit_wh <= AVAILABILITY_FULL_WH else 50


# The heater's own hot-water level, block 4 offset 46 (Block 27 param 23 in
# stumpylog/aosmith-ble's BLE map, research phase 49): 0/5/10 for low, medium
# and high per that report. Exposed beside availability() above, not instead of
# it, and not as availability: against the 33 app-labelled captures it reads
# "high" in electric with 2850-5100 Wh still to go (app: 50/0 %), "medium" at
# 120-300 Wh in heat pump (app: 100 %), and both "low" and "high" for the same
# 906 Wh state. availability() matches the app in heat pump mode too (18 of the
# 33 captures). Its meaning is open (research phase 49b).
HOT_WATER_LEVELS = {0: "low", 5: "medium", 10: "high"}
# Block 4 offset 4 (Block 27 param 2): what the controller is doing.
# 5 seen only during an active fault (the 10-26 leak test, phase 52).
OPERATING_STATES = {11: "idle", 12: "heating", 5: "fault"}


# What a frozen snapshot claims about activity is reported as unknown.
FROZEN_UNKNOWN = ("watts", "operating_state", "operating_state_raw",
                  "heating_element", "heat_flags")


def word(block: bytes, offset: int) -> int:
    return int.from_bytes(block[offset:offset + 2], "big")


def lifetime_energy(block: bytes) -> int | None:
    """Block 4 offsets 14-19 (Block 27 params 7-9): a 48-bit running total, in
    Wh, of the power the heater reports. All ones means no value."""
    value = int.from_bytes(block[14:20], "big")
    return None if value == 0xFFFFFFFFFFFF else value


def fahrenheit(raw: bytes) -> float:
    return int.from_bytes(raw, "big") * TEMP_F + 32


def heating_element(flags: int) -> str | None:
    """Which resistive element is on, from block 1 byte 61 (research phase 36).

    Bit 0x04 is the upper element, 0x08 the lower one: five switches in one
    afternoon's electric recoveries matched the tank sensors every time (upper
    while the top is below its cut-off, lower once the top is satisfied). The
    elements never run together, so both bits set is not a state we know.
    Bits 0x01 and 0x02 appear with the heat pump and stay unnamed until
    measured.
    """
    upper, lower = bool(flags & 0x04), bool(flags & 0x08)
    if upper and lower:
        return None
    return "upper" if upper else "lower" if lower else "off"


DSN_PATH_RE = re.compile(r"/dsns/([A-Za-z0-9]{6,32})\.xml$")
# decode()'s furthest reads: block 1 [100:102], block 3 [31], block 4 [46:48].
# Real blocks are 146/52/52 bytes. Block 2 is optional (the fault table).
MIN_B1, MIN_B3, MIN_B4 = 102, 32, 48


def decode(blocks: list[bytes]) -> dict:
    """One telemetry post -> the fields the integration exposes."""
    b1, b2, b3, b4 = blocks[1], blocks[2], blocks[3], blocks[4]
    # Slicing a short block reads zeros, not an error: an empty block 4 would
    # decode as 0 W and 100 % hot water. The furthest offsets read below.
    if len(b1) < MIN_B1 or len(b3) < MIN_B3 or len(b4) < MIN_B4:
        raise IndexError("telemetry block too short")
    has_faults = len(b2) >= 56
    pair = (b3[30], b3[31])
    return {
        "setpoint": round(fahrenheit(b3[0:2]), 1),
        "max_setpoint": round(fahrenheit(b1[86:88]), 1),
        # The target the controller is heating to right now. Equal to the
        # setpoint except in vacation, where it drops to 50 F while the
        # setpoint (block 3) keeps the value to return to. Three identical
        # copies follow at 84, 112 and 114 (research phase 34).
        "active_setpoint": round(fahrenheit(b1[80:82]), 1),
        # Two tank thermistors and the controller's blend of them (research
        # phase 37). The keys predate knowing which is which and are kept for
        # compatibility: "water_mid" [2:4] is the bottom thermistor, and
        # "water_bottom" [4:6] is computed, (top + bottom) / 2 in electric
        # mode and about (3 top + bottom) / 4 with the heat pump.
        "water_top": round(fahrenheit(b1[0:2]), 1),
        "water_bottom": round(fahrenheit(b1[4:6]), 1),        # the blend
        "water_mid": round(fahrenheit(b1[2:4]), 1),           # bottom thermistor
        "heat_deficit_wh": int.from_bytes(b4[24:26], "big"),
        "hot_water_availability": availability(word(b4, 24)),     # as the app shows it
        "hot_water_level": HOT_WATER_LEVELS.get(word(b4, 46)),    # the heater's own
        # Block 27 params 2, 7-9 and 15 (research phase 49). "air" used to be
        # read from offset 30: it is this capacity, which stepped with the
        # setpoint (2412 -> 1812 Wh at 150 -> 130 F), not a temperature.
        # Fault table, block 2 (phase 52; aosmith-ble's CURRENT_FAULT_ERROR_CODE
        # is block 2 word 7): 8-byte records, the code in the last word.
        # Record 1 is the active fault (0 = none); records 3-6 are the
        # maintenance display's H1-H4, newest first.
        "active_fault": word(b2, 14) if has_faults else None,
        "fault_history": ([word(b2, offset) for offset in (30, 38, 46, 54)]
                          if has_faults else None),
        "operating_state": OPERATING_STATES.get(word(b4, 4)),
        "operating_state_raw": word(b4, 4),
        "energy_wh": lifetime_energy(b4),
        "energy_capacity_wh": word(b4, 30),
        # Expansion valve opening, block 1 offset 100: the maintenance
        # display's P07 (480 on both, 10-26 14:39; research phase 51).
        "eev_pulses": word(b1, 100),
        # Ambient air thermistor, block 1 offset 14: the maintenance display's
        # P06 (61 -> 62 F while this read 58 -> 63, 10-26 14:39; phase 51).
        # It sits in the unit's air path: the garage temperature while the
        # compressor is off (67 F overnight), a few degrees under it while it
        # runs (54-58 F). A new key: "air" (until 0.7.4) was a capacity in Wh.
        "ambient": round(fahrenheit(b1[14:16]), 1),
        "mode_raw": f"{pair[0]}/{pair[1]}",
        "mode": mode_name(pair),
        # Vacation's first byte: the day count the app set (captured as 1 for
        # "1 day"). Whether the controller counts it down is not yet observed.
        "vacation_days": pair[0] if mode_name(pair) == "vacation" else None,
        "watts": int.from_bytes(b4[12:14], "big"),
        "heat_flags": b1[61],
        "heating_element": heating_element(b1[61]),
        "volts": b1[99],
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
        # Empty parts are kept, as on the LAN path: dropping one would shift
        # every later block into the wrong place.
        return [bytes.fromhex(part) for part in blob.split(",")]
    except (ValueError, IndexError):
        return None


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# Restored PendingWrite fields and the types they must have (None allowed).
_RESTORED_NUMBERS = ("queued", "delivered", "acked", "attempts", "ack_status",
                     "reports_after_ack", "days")
_RESTORED_STRINGS = ("frame", "kind", "delivery_path", "command_id")


def _restorable(kind, entry) -> bool:
    """A saved write of a shape the queue can use: a hand-edited or foreign
    queue file must not crash start-up or, later, every telemetry report."""
    if not isinstance(kind, str) or not isinstance(entry, dict):
        return False
    if any(entry.get(k) is not None and not _number(entry[k]) for k in _RESTORED_NUMBERS):
        return False
    if any(entry.get(k) is not None and not isinstance(entry[k], str)
           for k in _RESTORED_STRINGS):
        return False
    if entry.get("kind", kind) != kind:
        return False
    value = entry.get("value")
    if kind == "setpoint":
        return _number(value)           # _confirm_pending does float() on it
    return kind != "mode" or isinstance(value, str)


DEVICE_ID_RE = re.compile(r"<key>(\d{1,20})</key>")
DEVICE_PATH_RE = re.compile(r"^/devices/(\d{1,20})/")


class Relay:
    """One heater's engine: queue, telemetry, LAN session, cloud conversation.

    Run on its own it is the whole relay (one heater). The hub (icomm_hub, #18)
    runs one per heater, shares the listeners, client, DNS and peer policy, and
    routes each request to the right one.
    """

    def __init__(self, heater: Heater | None = None, secondary: bool = False) -> None:
        self.upstream = os.environ.get("UPSTREAM", "proxy-ingress-usfield.aylanetworks.com")
        self.last_raw: dict | None = None      # the last superBlockDyn1, hex
        self.listen_port = int(os.environ.get("LISTEN_PORT", "8444"))
        self.state_port = int(os.environ.get("STATE_PORT", "8124"))
        # Loopback by default: the add-on shares the host network with HA. A dev
        # relay on another machine sets 0.0.0.0 -- the API is unauthenticated, so
        # only on a trusted LAN.
        self.state_host = os.environ.get("STATE_HOST", "127.0.0.1")
        self.secondary = secondary
        self.device_dsn = heater.dsn if secondary and heater else os.environ.get("DEVICE_DSN", "")
        self.state: dict = {"online": False}
        self.last_telemetry = 0.0
        # Reports can keep arriving with byte-identical contents: the module
        # re-sends what the controller last told it (measured 2026-10-25,
        # 3+ hours frozen while the heat pump ran). A healthy idle tank still
        # changes some bytes; the longest identical run seen was 35 minutes.
        self.contents_changed = 0.0
        self._last_blocks: tuple[bytes, ...] | None = None
        self._last_active_fault: int | None = None
        self.decode_errors = 0
        self.ingest_errors = 0
        self.stray_headers = 0      # the module's "Content-Length: 0" after GET /ping
        self.contents_stale_after = float(os.environ.get("CONTENTS_STALE_AFTER", "5400"))
        # Heating changes tank temperatures within minutes: the longest healthy
        # gap between controller pushes while heating was 10 min (n=115); idle
        # reached 52 (phase 50). So a "heating" report unchanged for 20 min is
        # frozen -- 10-26 it said 356 W for 1 h 51 m after the compressor stopped.
        self.contents_stale_heating_after = float(
            os.environ.get("CONTENTS_STALE_HEATING_AFTER", "1200"))
        # Renew the controller's periodic reporting when the contents freeze
        # (phase 42). Off by default until confirmed on the tank.
        self.mask_renew = os.environ.get("REPORTING_MASK_RENEW", "1") != "0"
        self.mask_renew_after = float(os.environ.get("REPORTING_MASK_RENEW_AFTER", "1800"))
        # Experimental, off by default: after 2 renewals haven't unfrozen the
        # data, drop the LAN session so the module re-registers (new key
        # exchange). 10-26 the only reconnect during a freeze was followed by
        # live data within 11 min, while 5 renewals were not; 0 of 17 relay
        # reconnects outside a freeze started one (phase 50). One trial.
        self.freeze_reconnect = os.environ.get("FREEZE_RECONNECT", "0") == "1"
        # The cloud-echo probe: on by default; CLOUD_PROBE_AFTER is both the
        # cloud quiet time that triggers one and the minimum spacing.
        self.cloud_probe = os.environ.get("CLOUD_PROBE", "1") != "0"
        self.cloud_probe_after = float(os.environ.get("CLOUD_PROBE_AFTER", "1800"))
        self.cloud_probes = 0
        self.cloud_down_since: float | None = None
        self.cloud_down_count = 0
        self.cloud_probe_result: str | None = None     # "echoed" or "missing"
        self.freeze_masks = 0            # renewals since the contents last changed
        self.freeze_reconnected = False  # at most one reconnect per freeze
        self.freeze_reconnects = 0
        self.last_freeze_reconnect: float | None = None
        self.started = time.time()
        self.upstream_errors = 0
        # Ayla's certificate failing verification (an expired pin, a new CA)
        # looks like any outage to the module, which gets synthetic replies
        # and carries on. Telemetry then stops reaching the cloud and the app
        # quietly goes stale, so it is tracked apart from other errors.
        self.upstream_tls: dict = {"failures": 0, "last_failure": None,
                                   "last_error": None, "last_ok": None}
        self._tls_warned = 0.0
        self.client: ClientSession | None = None
        # Writes wait here until the appliance asks for them. Only the newest
        # request per register survives: the appliance applies one frame at a
        # time, and delivering a stale queued value after a newer one is a bug.
        self.pending: dict[str, PendingWrite] = {}
        self.last_write: dict | None = None
        self.writes_delivered = 0
        # Relay-wide files sit in the data dir; the heater's own (key, queue,
        # facts, saved config) in heaters/<DSN>/ under it (icomm_heater, #18).
        queue_env = Path(os.environ.get("QUEUE_FILE", "state/pending.json"))
        self.data_dir = Path(os.environ.get("DATA_DIR") or queue_env.parent)
        self.heater = heater or Heater(self.data_dir, known_dsn(self.data_dir, self.device_dsn),
                                       os.environ.get("OFFLINE_FALLBACK", "1") != "0",
                                       queue_env.name)
        # Ayla's ID for this heater (the config reply's <key>): the /devices/<id>/
        # in every cloud request, so the hub can route them under NAT steering.
        self.device_id: str | None = self.device.facts.get("ayla_id")
        # Set by the hub: every heater's DSN, for /state.
        self.heaters_list = lambda: [d for d in [self.heater.dsn] if d]
        self._device_read_asked = False
        # Identity from the last real config reply this relay saved (the DSN
        # rewrite evidence), so it shows before the heater's next boot.
        with contextlib.suppress(OSError, UnicodeDecodeError):
            saved = (self.heater.dir / "dsn-original.xml").read_text()
            self.device.note_dsn(saved)
            self._learn_device_id(saved)
        # Firmware updates wait for the owner (icomm_firmware explains why).
        self.firmware = FirmwareGate(os.environ.get("FIRMWARE_UPDATES", "hold"),
                                     self.data_dir / "firmware.json")
        # A restored write older than this is dropped rather than delivered: an
        # appliance that sat idle overnight should not surprise anyone at breakfast.
        self.queue_max_age_s = int(os.environ.get("QUEUE_MAX_AGE_S", str(24 * 3600)))
        self.queue_restored = 0
        # The local doorbell: Ayla Local Connect, with a key we mint ourselves
        # (phase 28). LAN delivery and LAN telemetry; the cloud poll stays as
        # the fallback. No A.O. Smith account anywhere.
        self.lan = LanServer(self, secondary)
        if self.device_dsn or self.lan.module_dsn:
            self.heater.adopt(self.device_dsn or self.lan.module_dsn)
            self.device.note_dsn_id(self.device_dsn or self.lan.module_dsn)
        self.peers = PeerPolicy(lambda: self.lan.module_ip)
        self.dns = DnsServer()
        self.peers.pin_candidate = lambda request: self._pinnable_dsn(request) is not None
        # DNS steering lands the module on 443, the port its cloud name uses.
        # Listened on beside listen_port, so NAT steering keeps working too.
        self.dns_https_error: str | None = None
        # The 401 nudge: an experiment, not a doorbell. Phase 24 measured that
        # a 401 on a telemetry POST is simply retried and provokes no poll, so
        # this is left at 0 unless someone explicitly asks. LAN mode is the
        # mechanism that actually delivers writes promptly.
        self.nudges_left = 0
        self.nudge_on_telemetry = False
        self.debug_requests = os.environ.get("DEBUG_REQUESTS", "") == "1"
        # The LAN-mode experiment (branch experiment/dsn-lan-rewrite): the
        # module's own Ayla record ships with the OEM's factory kill switches
        # -- <lan-enabled>false</lan-enabled> and
        # <local-access-enabled>false</local-access-enabled>. With this set, the
        # relay flips exactly those two values in a DSN config reply, and stashes
        # the original and rewritten bodies next to the queue file as evidence.
        self.dsn_lan_rewrite = os.environ.get("DSN_LAN_REWRITE", "") == "1"
        # LAN key repair/rotation: Ayla's own reverse-REST command. A
        # PUT lanip.json inside a commands.xml reply sets AND SAVES a new key
        # (libada's client_lanip_json_put), which is the only network-reachable
        # way to replace a key the module already holds. Pushed on each poll
        # until the module's PUT .../lanip.json?cmd_id=N&status=204 ack arrives.
        # Forced push (testing/override); normally the LAN server decides,
        # by detecting a module that holds a foreign key.
        self.lanip_push = os.environ.get("LANIP_PUSH", "") == "1"
        self.lanip_cmd_id = 0
        self.lanip_push_acked = False
        # Conversation shape, for the degraded-state detector: the module talks in
        # dials, so requests are grouped into them and their shape remembered.
        self.telemetry_times: deque[float] = deque(maxlen=12)
        # When telemetry delivered over Local Connect changed: with no cloud
        # request alongside, the module's cloud side has stopped (health).
        self.lan_changes: deque[float] = deque(maxlen=12)
        # Start and end of every cloud dial in the last 24 h, for health's
        # dial count and longest gap: a recurrence of the stopped cloud side
        # shows in /state (and the telemetry log), not only in the repair.
        # In memory: a relay restart -- the usual trigger -- starts it over, and
        # the gap then counts from the start.
        self.dial_log: deque[list[float]] = deque(maxlen=5000)
        self.dials: deque[dict] = deque(maxlen=6)
        self._dial: dict | None = None
        # The appliance advertises how often it reports; learned from the config
        # reply. Until then assume nothing, and treat silence generously.
        self.reporting_interval_s: int | None = None
        self.writes_confirmed = 0
        self.injections_held = 0
        # Writes that came from the cloud (the A.O. Smith app or its API), seen
        # as setBlockParameter in a commands.xml reply on their way to the module.
        self.cloud_writes = 0
        self.last_cloud_write: dict | None = None
        # Pinned Entrust G2 for upstream verification: certifi dropped it, so
        # default verification fails against Ayla's ingress. Public CA cert.
        ca = Path(__file__).parent / "upstream-entrust-g2.pem"
        # aiohttp wants an SSLContext (a bare path string raises on use).
        self.upstream_ssl = ssl.create_default_context(cafile=str(ca)) if ca.exists() else None

    # The heater's components (one heater until #18 phase 2).
    @property
    def queue_file(self) -> Path:
        return self.heater.queue_file

    @property
    def device(self):
        return self.heater.device

    @property
    def offline(self):
        return self.heater.offline

    # -- the module-facing side ---------------------------------------
    async def proxy(self, request: web.Request,
                    transform=None) -> web.Response:
        """Forward a request upstream verbatim, optionally rewriting the reply.

        The appliance's cloud view must stay intact -- the mobile app depends on
        it -- so replies are returned exactly as Ayla sent them, with one
        exception: a command poll may carry a locally queued write instead of
        (never in addition to) whatever Ayla had queued for it.
        """
        if transform is None and "commands.xml" in request.path:
            # FIRMWARE_UPDATES=hold covers every command poll that passes
            # through, trusted or not: an untrusted peer or a heater no engine
            # owns (hub) is still a module that would install an OTA command.
            transform = self.firmware.filter
        body = await request.read()
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in {"host", "content-length", "transfer-encoding", "connection"}
        }
        try:
            assert self.client is not None
            # The host is fixed by configuration; only the path and query come
            # from the client (a transparent proxy must forward those). Built
            # with yarl, nothing in them can move the request to another host.
            target = URL.build(scheme="https", host=self.upstream,
                               path=request.raw_path.split("?", 1)[0],
                               query_string=request.query_string, encoded=True)
            async with self.client.request(
                request.method, target,
                headers=headers, data=body, allow_redirects=False,
                ssl=self.upstream_ssl,
            ) as upstream:
                self.upstream_tls["last_ok"] = time.time()
                if upstream.status >= 500:
                    # Ayla's ingress is up but its backend isn't: the outage case.
                    offline = self._offline_config(request, transform)
                    if offline is not None:
                        self.upstream_errors += 1
                        return offline
                payload = await upstream.read()
                if (upstream.status == 200 and "commands.xml" in request.path
                        and request.get(TRUSTED, True)):
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
        except Exception as err:
            # Ayla is often down or slow; the module must never see our failure
            # become its own. Synthetic replies keep its conversation alive.
            self.upstream_errors += 1
            if isinstance(err, (ClientSSLError, ssl.SSLError)):
                self._note_tls_failure(err)
            offline = self._offline_config(request, transform)
            if offline is not None:
                return offline
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
                return web.Response(status=200, content_type="application/xml",
                                    text=synthetic)
            return web.Response(status=503, text="")

    def _offline_config(self, request: web.Request, transform) -> web.Response | None:
        """The module's own config fetch, answered locally while Ayla is out."""
        found = DSN_PATH_RE.search(request.path)
        if not found or not request.get(TRUSTED, True):
            return None
        known = (self.device_dsn or self.lan.module_dsn or self.heater.dsn
                 or self.device.facts.get("dsn"))
        return self.offline.answer(request, found.group(1), known, transform)

    def _learn_device_id(self, text: str) -> None:
        found = DEVICE_ID_RE.search(text)
        if found:
            self.device_id = found.group(1)
            self.device.note_ayla_id(self.device_id)

    def _note_tls_failure(self, err: Exception) -> None:
        cause = getattr(err, "certificate_error", None) or err
        message = (getattr(cause, "verify_message", None) or getattr(cause, "reason", None)
                   or type(cause).__name__)
        now = time.time()
        self.upstream_tls.update(failures=self.upstream_tls["failures"] + 1,
                                 last_failure=now, last_error=str(message)[:200])
        if now - self._tls_warned > 3600:
            self._tls_warned = now
            _LOGGER.warning("upstream TLS failed verifying %s: %s -- cloud sync is down "
                            "until the relay trusts Ayla's certificate again",
                            self.upstream, message)

    def tls_status(self) -> dict:
        tls = dict(self.upstream_tls)
        failed, ok = tls["last_failure"], tls["last_ok"]
        tls["failing"] = failed is not None and (ok is None or failed > ok)
        return tls

    async def telemetry(self, request: web.Request) -> web.Response:
        """superBlockDyn1 POST: decode, then forward as if we were not here."""
        body = await request.read()
        self._safe_ingest(telemetry_blocks(body))
        return await self.proxy(request)

    def _safe_ingest(self, blocks: list[bytes] | None, lan: bool = False) -> None:
        """_ingest_blocks, but a bug in it must not cost the report its trip
        to Ayla (cloud) or turn into a 500 for the module (LAN)."""
        try:
            self._ingest_blocks(blocks, lan=lan)
        except Exception as err:  # noqa: BLE001 -- forwarding must happen regardless
            self.ingest_errors += 1
            _LOGGER.warning("telemetry ingest failed (%s); %d so far",
                            type(err).__name__, self.ingest_errors)

    def _ingest_blocks(self, blocks: list[bytes] | None, lan: bool = False) -> None:
        """Decode a superBlockDyn1 report, from the cloud POST or the LAN."""
        if blocks and len(blocks) >= 5:
            # Kept raw for register research (GET /raw): the decoder only knows
            # the offsets someone has already mapped.
            self.last_raw = {"at": time.time(), "mode": None,
                             "blocks": [block.hex() for block in blocks]}
            if tuple(blocks) != self._last_blocks:
                self._last_blocks = tuple(blocks)
                self.contents_changed = time.time()
                self.freeze_masks = 0
                self.freeze_reconnected = False
                if lan:
                    self.lan_changes.append(self.contents_changed)
            self._maybe_renew_mask()
            try:
                self.state = decode(blocks)
                self.state["online"] = True
                self.state["last_telemetry"] = time.time()
                if self.device_dsn:
                    self.state["dsn"] = self.device_dsn
                self.last_telemetry = time.time()
                self.last_raw["mode"] = self.state.get("mode_raw")
                self._note_active_fault(self.state.get("active_fault"))
            except (IndexError, ValueError) as err:
                # Kept going: a report we can't decode must not break the
                # proxy. Counted on /state, and logged by type only (the
                # bytes came off the network).
                self.decode_errors += 1
                _LOGGER.warning("telemetry report didn't decode (%s); %d so far",
                                type(err).__name__, self.decode_errors)
            self._confirm_pending()

    def _note_active_fault(self, code: int | None) -> None:
        """The telemetry's active fault feeds the same fact as the faultCode
        property, but only when it changes: faultCode arrives first (1.5 s to
        HA in the leak test, the next report 23 s later), and a report taken
        just before a clear must not bring the fault back. On the first report
        after a restart it settles a fault fact that was never posted or that
        went stale while the relay was down."""
        if code is None or code == self._last_active_fault:
            return
        self._last_active_fault = code
        self.device.note_fault(code, "telemetry")

    def _heating(self) -> bool:
        watts = self.state.get("watts")
        return isinstance(watts, (int, float)) and watts > 0

    def stale_after(self) -> float:
        """How long unchanged contents may last before they count as frozen."""
        if self._heating():
            return min(self.contents_stale_heating_after, self.contents_stale_after)
        return self.contents_stale_after

    def renew_after(self) -> float:
        return min(self.mask_renew_after, self.stale_after())

    def _maybe_renew_mask(self) -> None:
        """Contents frozen this long: ask for periodic reporting again, at most
        once per interval, so a mask that doesn't help can't become a loop.
        With freeze_reconnect, the third attempt is a LAN reconnect instead."""
        if not self.mask_renew or not self.contents_changed:
            return
        now = time.time()
        after = self.renew_after()
        if now - self.contents_changed < after or now - self.lan.last_mask_sent < after:
            return
        if (self.freeze_reconnect and not self.freeze_reconnected
                and self.freeze_masks >= 2 and self.lan.reconnect()):
            self.freeze_reconnected = True
            self.freeze_reconnects += 1
            self.last_freeze_reconnect = now
            self.lan.last_mask_sent = now      # the next renewal waits a full interval
            _LOGGER.warning("telemetry unchanged for %d min after %d reporting-mask renewals: "
                            "reconnecting the LAN session (experimental_freeze_reconnect)",
                            (now - self.contents_changed) // 60, self.freeze_masks)
            return
        if self.lan.request_mask():
            self.freeze_masks += 1
            _LOGGER.warning("telemetry unchanged for %d min: renewing the reporting mask",
                            (now - self.contents_changed) // 60)

    async def reporting_mask_handler(self, request: web.Request) -> web.Response:
        """POST /lan/reporting-mask: send the app's reporting mask once, now."""
        if not self.lan.request_mask():
            return web.json_response({"queued": False, "error": "no LAN session"},
                                     status=409)
        _LOGGER.warning("reporting mask requested through the state API")
        return web.json_response({"queued": True})

    async def raw_handler(self, request: web.Request) -> web.Response:
        """GET /raw: the last superBlockDyn1 as hex, one string per block."""
        if self.last_raw is None:
            return web.json_response({"error": "no telemetry yet"}, status=404)
        return web.json_response({**self.last_raw, "static": self.device.static})

    async def ingest_lan_update(self, update: dict) -> None:
        """A decrypted LAN property update: telemetry, or a write ack."""
        data = update.get("data") if isinstance(update, dict) else None
        data = data if isinstance(data, dict) else {}
        name = data.get("name")
        name = name if isinstance(name, str) and name else (
            "ack" if "ack_status" in data else "?")
        raw = data.get("value")
        value = raw if isinstance(raw, str) else "" if raw is None else str(raw)
        if name not in ("superBlockDyn1", "ack") and 0 < len(value) <= 512:
            # Small configuration/echo properties (periodicParameterSetting,
            # setBlockParameter echoes...) carry no secrets, and phase 30 showed
            # that logging only their length leaves a question unanswerable.
            _LOGGER.info("LAN update: %r = %r", name, value)
            if name == "periodicParameterSetting":
                self.lan.note_mask(value)
        else:
            _LOGGER.info("LAN update: %r (%d chars)", name, len(value))
        self.device.note_property(name, value)
        if not self._device_read_asked and self.lan.session_verified:
            # Reported only at boot: ask once, instead of waiting for one.
            self._device_read_asked = True
            if "controller_firmware" not in self.device.facts:
                self.lan.read_queue.append("active_image_version")
            if "serial_number" not in self.device.facts:
                self.lan.read_queue.append("superBlockStatic1")
        if name == "superBlockDyn1" and value:
            try:
                blocks = [bytes.fromhex(part) for part in value.split(",")]
            except ValueError:
                _LOGGER.warning("LAN telemetry undecodable")
            else:
                self._safe_ingest(blocks, lan=True)
        elif "ack_status" in data:
            _LOGGER.info("LAN write ack: status %s id %s",
                         data.get("ack_status"), data.get("id"))
            self._record_ack(data.get("id"), data.get("ack_status"))
            if str(data.get("ack_status")) == "200":
                # The tank does not reliably report after a local write. Ask
                # the MCU for a fresh value now instead of re-sending the write
                # or waiting for a spontaneous report.
                self.lan.request_telemetry()
                self.lan.wake()

    def _record_ack(self, command_id, status) -> None:
        """Attach a LAN ack to the write it answers (matched by property id)."""
        if command_id and command_id == self.lan.mask_id:
            self.lan.mask_acked = time.time()
            return
        for write in self.pending.values():
            if command_id and write.command_id == command_id:
                write.acked = time.time()
                try:
                    write.ack_status = int(status)
                except (TypeError, ValueError):
                    write.ack_status = -1
                write.reports_after_ack = 0
                write.observed_after_ack = None
                self._save_queue()
                return

    def take_pending_for_lan(self):
        """Oldest queued write for the LAN poll; None when there is nothing.

        No cloud-freshness gate here: the LAN session being alive is itself the
        evidence the module is listening, and a LAN delivery works even when
        the internet is down.
        """
        # Only into a session that has proven it can talk to us: a module with
        # a foreign key completes key exchanges but cannot decrypt our replies,
        # and every copy sent into it would use up the delivery cap the cloud
        # fallback needs (second review).
        if not self.lan.session_verified:
            return None
        # The LAN poll comes every ~10 s; re-sending the same frame each time
        # until telemetry confirms would hammer the appliance. One delivery,
        # then a quiet 60 s retry window if confirmation never comes.
        write = self._next_write(retry_after=60.0)
        if write is None:
            return None
        self._mark_delivered(write, "lan")
        return write

    def _next_write(self, retry_after: float = 0.0) -> PendingWrite | None:
        """The oldest deliverable write: not exhausted, not inside its retry window.

        Oldest by queue time, not by name. Picking sorted(self.pending)[0] meant
        a stuck "mode" write blocked every "setpoint" write behind it forever
        (pre-publish review).
        """
        self._expire_pending()
        now = time.time()
        for write in sorted(self.pending.values(), key=lambda w: w.queued):
            if write.attempts >= MAX_ATTEMPTS or write.status == "rejected":
                continue
            if retry_after and write.delivered and now - write.delivered < retry_after:
                continue
            return write
        return None

    def _expire_pending(self) -> None:
        """Retire writes that cannot succeed any more, so they stop showing as targets.

        Exhausted writes are kept for RETIRE_AFTER_S after the last delivery, in
        case a late report confirms them. Then they move to last_write as
        not_applied, rejected, or unconfirmed (cloud path: no ack exists). Any
        write older than QUEUE_MAX_AGE_S is retired as expired, in memory too,
        not only on restore.
        """
        now = time.time()
        retired = []
        for kind, write in self.pending.items():
            if now - write.queued > self.queue_max_age_s:
                retired.append((kind, "expired"))
            elif ((write.attempts >= MAX_ATTEMPTS or write.status == "rejected")
                  and write.delivered and now - write.delivered > RETIRE_AFTER_S):
                status = write.status
                retired.append((kind, status if status in ("not_applied", "rejected")
                                else "unconfirmed"))
        for kind, outcome in retired:
            write = self.pending.pop(kind)
            self.last_write = {**write.public(), "outcome": outcome}
            _LOGGER.warning("retired %s %s write: %s after %d attempt(s)",
                            kind, write.value, outcome, write.attempts)
        if retired:
            self._save_queue()

    def _mark_delivered(self, write: PendingWrite, path: str = "cloud") -> None:
        write.delivery_path = path
        write.acked = None              # a new delivery needs its own ack
        write.ack_status = None
        write.attempts += 1
        write.delivered = time.time()
        self.writes_delivered += 1
        self._save_queue()          # record the attempt, so a restart cannot replay it

    # -- persistence ---------------------------------------------------
    def _load_queue(self) -> None:
        """Restore writes queued before a restart.

        The queue used to live only in memory, so a restart silently dropped it --
        demonstrated the hard way while waiting on an idle appliance. A write that
        survived a restart is marked as such: an appliance can be idle for hours, so
        a restored write is legitimate, but a human should be able to see that a
        value came back from before the restart rather than from just now.
        """
        if not self.queue_file.exists():
            return
        try:
            saved = json.loads(self.queue_file.read_text())
        except (OSError, ValueError):
            _LOGGER.warning("unreadable queue file %s; starting empty", self.queue_file)
            return
        if not isinstance(saved, dict):
            _LOGGER.warning("queue file %s is not a JSON object; starting empty",
                            self.queue_file)
            return
        now = time.time()
        restored = 0
        pending = saved.get("pending")
        for kind, entry in (pending.items() if isinstance(pending, dict) else ()):
            if not _restorable(kind, entry):
                _LOGGER.warning("dropping a malformed queued write from the queue file")
                continue
            queued = entry.get("queued") or now
            if now - queued > self.queue_max_age_s:
                # Older than the appliance could plausibly still care about. Dropping
                # it beats surprising someone with a write from hours ago.
                _LOGGER.warning("dropping expired %s write (%.0f s old)", kind, now - queued)
                continue
            write = PendingWrite(
                kind=entry.get("kind", kind),
                value=entry.get("value"),
                frame=entry.get("frame", ""),
                queued=queued,
                attempts=entry.get("attempts", 0),
                delivered=entry.get("delivered"),
                delivery_path=entry.get("delivery_path"),
                command_id=entry.get("command_id"),
                acked=entry.get("acked"),
                ack_status=entry.get("ack_status"),
                reports_after_ack=entry.get("reports_after_ack", 0),
                observed_after_ack=entry.get("observed_after_ack"),
                days=entry.get("days"),
                restored=True,
            )
            if write.frame:
                self.pending[kind] = write
                restored += 1
        last_write = saved.get("last_write")
        self.last_write = last_write if isinstance(last_write, dict) else None
        self.queue_restored = restored
        if restored:
            _LOGGER.info("restored %d queued write(s) from %s", restored, self.queue_file)

    def _save_queue(self) -> None:
        """Write the queue atomically: a half-written file must not look empty."""
        payload = {
            "pending": {kind: asdict(write) for kind, write in self.pending.items()},
            "last_write": self.last_write,
        }
        try:
            self.queue_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.queue_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=1))
            temporary.replace(self.queue_file)      # atomic on the same filesystem
        except OSError as err:
            _LOGGER.warning("could not persist the write queue: %s", err)

    # -- the write path ------------------------------------------------
    def queue_write(self, kind: str, value, days: object = None) -> PendingWrite:
        """Accept a write, clamped to what the appliance itself allows.

        The limits come from the appliance's telemetry, not from a hard-coded
        policy: this relay must not be the reason a tank is asked to do something
        its own controller advertises as out of range.
        """
        if days is not None and not (kind == "mode" and str(value) == "vacation"):
            raise ValueError("days applies only to a vacation mode write")
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
            if value == "vacation":
                days = frames.vacation_days(days)
            payload_frame = frames.as_text(frames.mode_frame(value, days))
        else:
            raise ValueError(f"unknown write kind {kind!r}")

        superseded = 0
        if kind in self.pending:
            superseded = (self.pending[kind].superseded or 0) + 1
        write = PendingWrite(kind=kind, value=value, frame=payload_frame,
                             superseded=superseded or None, days=days)
        self.pending[kind] = write
        self._save_queue()
        # Do not wait up to 10 s for the module's next poll: an out-of-band
        # heartbeat with notify=1 makes it fetch commands now (libada
        # client_lan_refresh sets lan->pending and wakes the agent).
        self.lan.wake()
        return write

    def _confirm_pending(self) -> None:
        """Retire a write once telemetry shows it took effect.

        Confirmation is the appliance's own report, never our own ack: a frame can
        be delivered and ignored (the BLE register at block 26 is exactly that).
        """
        confirmed = []
        observed = False
        for kind, write in self.pending.items():
            if kind == "setpoint":
                got = self.state.get("setpoint")
                took = got is not None and abs(float(got) - float(write.value)) <= 0.5
            else:
                got = self.state.get("mode")
                took = got == write.value
            if not took and write.acked:
                write.reports_after_ack += 1
                write.observed_after_ack = got
                observed = True
            if took:
                write.confirmed = time.time()
                self.writes_confirmed += 1
                confirmed.append(kind)
        if confirmed:
            for kind in confirmed:
                write = self.pending.pop(kind)
                self.last_write = {**write.public(), "outcome": "confirmed"}
        elif self._late_confirmation():
            observed = True
        if confirmed or observed:
            self._save_queue()

    def _late_confirmation(self) -> bool:
        """A retired write that the tank applied after all: say so."""
        last = self.last_write or {}
        if last.get("outcome") not in ("not_applied", "unconfirmed", "expired", "rejected"):
            return False
        if last.get("kind") in self.pending:
            return False                # a newer write for the register owns it now
        delivered = last.get("delivered")
        if not _number(delivered) or time.time() - delivered > 3600:
            # Never sent, or long ago: a matching value now is more likely the
            # app or a person at the tank than our write landing late.
            return False
        got = self.state.get(last.get("kind"))
        if last.get("kind") == "setpoint":
            value = last.get("value")
            took = (got is not None and _number(value)
                    and abs(float(got) - float(value)) <= 0.5)
        else:
            took = got is not None and got == last.get("value")
        if took:
            self.last_write = {**last, "outcome": "confirmed",
                               "confirmed_late": time.time(), "earlier_outcome": last["outcome"]}
        return took

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
        # Same 60 s window as the LAN path, so a cloud poll seconds after a LAN
        # delivery does not re-send it and use up an attempt.
        write = self._next_write(retry_after=60.0)
        if write is None:
            return None
        text = payload.decode(errors="replace")
        rewritten, replaced = frames.rewrite_commands(text, write.frame)
        if rewritten == text:
            return None
        write.replaced_upstream = replaced
        self._mark_delivered(write)
        return rewritten.encode()

    def _note_cloud_write(self, payload: bytes, path_qs: str) -> None:
        """Record a write the cloud is delivering: the app's, not ours.

        A plain commands.xml reply carrying setBlockParameter is a command
        delivery. The re-registration fetch (commands.xml?input=true) also
        carries it, but there it describes the current state, so it is skipped.
        """
        if "input=true" in path_qs:
            return
        found = frames.SET_BLOCK_PROPERTY.search(payload.decode(errors="replace"))
        frame = found.group(2).strip() if found else ""
        if not frame:
            return
        try:
            decoded = frames.decode_frame(frame)
        except ValueError:
            decoded = {}
        kind = "setpoint" if "setpoint" in decoded else "mode" if "mode" in decoded else "frame"
        self.cloud_writes += 1
        self.last_cloud_write = {
            "at": time.time(), "kind": kind, "value": decoded.get(kind, frame),
            "frame": frame, "source": "cloud",
            "days": decoded.get("vacation_days"),
            # Filled in by _transform_commands if a local write replaces it.
            "replaced_by_local": False}
        _LOGGER.warning("cloud write: %s %r (frame %r) -- from the A.O. Smith app or API",
                        kind, self.last_cloud_write["value"], frame)

    def _transform_commands(self, payload: bytes, path_qs: str = "") -> bytes | None:
        """Hold firmware, note any cloud write, then apply the queued write and the lanip push."""
        gated = self.firmware.filter(payload)
        if gated is not None:
            payload = gated
        self._note_cloud_write(payload, path_qs)
        rewritten = self._inject(payload)
        if rewritten is not None and self.last_cloud_write and \
                time.time() - self.last_cloud_write["at"] < 1:
            # Ours replaced the app's in this same reply; the app's never lands.
            self.last_cloud_write["replaced_by_local"] = True
            _LOGGER.warning("cloud write superseded by a queued local write")
        if rewritten is not None:
            payload = rewritten
        pushed = self._maybe_push_lanip(payload)
        # None means "unchanged" to proxy(). Returning the push result alone
        # (since 0.3.1) silently discarded every injected write whenever no
        # key push was armed: the module got Ayla's original reply while the
        # write was counted as delivered. LAN delivery masked it. The same
        # goes for a held firmware command: dropping it must reach the module.
        for result in (pushed, rewritten, gated):
            if result is not None:
                return result
        return None

    def _maybe_push_lanip(self, payload: bytes) -> bytes | None:
        """Deliver Ayla's key-rotation command: PUT lanip.json in <cmds>.

        XML shapes per libada's parsers: <resource> takes no leading slash (the
        agent prepends it), <uri> keeps it (the agent strips the first char),
        <data> is the raw JSON body, one command per poll. The agent answers
        with PUT /devices/<key>/lanip.json?cmd_id=N&status=204, which is the
        confirmation that the key was actually saved.
        """
        if not self.lan.enabled:
            return None
        if self.lan.needs_key_push:
            self.lanip_push_acked = False       # a fresh need re-arms the push
        elif not self.lanip_push or self.lanip_push_acked:
            return None
        text = payload.decode(errors="replace")
        if "<commands" not in text:
            return None
        self.lanip_cmd_id += 1
        body = json.dumps({"lanip": {"status": "enable",
                                     "lanip_key_id": self.lan.key_id,
                                     "lanip_key": self.lan.key,
                                     "keep_alive": 30, "auto_sync": 1}})
        cmd = (f"<cmd><id>{self.lanip_cmd_id}</id><method>PUT</method>"
               f"<resource>lanip.json</resource><uri>/lanip.json</uri>"
               f"<data>{body}</data></cmd>")
        if "<cmds/>" in text:
            text = text.replace("<cmds/>", f"<cmds>{cmd}</cmds>", 1)
        elif "</cmds>" in text:
            text = text.replace("</cmds>", f"{cmd}</cmds>", 1)
        else:
            return None
        _LOGGER.warning("LANIP PUSH: delivering PUT lanip.json (cmd %s)",
                        self.lanip_cmd_id)
        return text.encode()

    def _note_config(self, payload: bytes) -> str:
        """What a config reply says about the heater, LAN mode or not: its
        firmware, identity and Ayla ID (which the hub routes by)."""
        text = payload.decode(errors="replace")
        self.firmware.note_dsn(text)
        self.device.note_dsn(text)
        self._learn_device_id(text)
        return text

    def note_config(self, payload: bytes) -> None:
        """A transform that only reads: the reply goes on unchanged."""
        self._note_config(payload)

    def _rewrite_dsn(self, payload: bytes) -> bytes | None:
        """Flip Ayla's factory LAN-mode switches in a DSN config reply.

        Phase 27 showed the flags alone change nothing; phase 28 showed why:
        the agent must also fetch lan.json, which the LAN server answers with
        our own key. That combination arms the module's LAN server and gives
        local writes and telemetry. Only 200s are rewritten; a reply without a
        switch present passes through untouched.
        """
        text = self._note_config(payload)
        switched = []
        for element in ("lan-enabled", "local-access-enabled"):
            off = f"<{element}>false</{element}>"
            if off in text:
                text = text.replace(off, f"<{element}>true</{element}>")
                switched.append(element)
        if not switched:
            return None
        self.lan.learn_module_ip(text)
        evidence = self.heater.dir
        rewritten = text.encode()
        try:
            # Atomic and 0600: dsn-original.xml is the offline fallback's
            # source, and a disk error must not turn Ayla's 200 into ours.
            write_private(evidence / "dsn-original.xml", payload)
            write_private(evidence / "dsn-rewritten.xml", rewritten)
        except OSError as err:
            _LOGGER.warning("DSN rewrite: evidence not saved (%s)", type(err).__name__)
        _LOGGER.warning("DSN rewrite: flipped %s in config reply (evidence in %s)",
                        ", ".join(switched), evidence)
        return rewritten

    # -- the Home-Assistant-facing side --------------------------------
    async def write_handler(self, request: web.Request) -> web.Response:
        """POST /write {"setpoint": 140}, {"mode": "heat_pump"} or
        {"mode": "vacation", "days": 1}.

        Queues, never delivers: delivery happens on the appliance's own schedule,
        when it asks for commands. A write that is queued is not a write that
        happened, so the response says `queued`, and `GET /state` reports whether
        the appliance ever confirmed it.
        """
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "expected JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "expected a JSON object"}, status=400)
        kind = "setpoint" if "setpoint" in body else "mode" if "mode" in body else None
        if kind is None:
            return web.json_response({"error": "give setpoint or mode"}, status=400)
        try:
            write = self.queue_write(kind, body[kind], days=body.get("days"))
        except (ValueError, TypeError) as err:
            return web.json_response({"error": str(err)}, status=400)
        # Delivery: the module's own 10-second LAN poll when the LAN session is
        # up, otherwise the next cloud poll. Either way confirmation is telemetry.
        return web.json_response({"queued": write.public(), "pending": len(self.pending),
                                  "lan_registered": self.lan.registered})

    async def write_cancel_handler(self, request: web.Request) -> web.Response:
        dropped = {kind: asdict(w) for kind, w in self.pending.items()}
        self.pending.clear()
        self._save_queue()
        return web.json_response({"cancelled": dropped})

    async def firmware_allow_handler(self, request: web.Request) -> web.Response:
        """The owner allows one held update: {"key": "<type>:<version>"}."""
        try:
            key = str((await request.json())["key"])
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "expected {\"key\": \"type:version\"}"},
                                     status=400)
        if not self.firmware.allow(key):
            return web.json_response({"error": f"no firmware offer {key!r}"}, status=404)
        return web.json_response({"allowed": key})

    async def fetch_firmware(self, request: web.Request, target: str) -> web.Response:
        """Forward an allowed image download to its real host, verified.

        Ranged reads pass through: the agent fetches the image in pieces.
        """
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() in {"range", "accept", "user-agent", "if-range"}}
        try:
            assert self.client is not None
            async with self.client.get(target, headers=headers, allow_redirects=False,
                                       ssl=True) as upstream:
                payload = await upstream.read()
                response = web.Response(status=upstream.status, body=payload)
                for k in ("Content-Type", "Content-Range", "Accept-Ranges", "ETag"):
                    if k in upstream.headers:
                        response.headers[k] = upstream.headers[k]
                return response
        except Exception as err:
            _LOGGER.warning("firmware download failed: %s", type(err).__name__)
            return web.Response(status=502, text="")

    async def state_handler(self, request: web.Request) -> web.Response:
        self._expire_pending()
        out = dict(self.state)
        age = round(time.time() - self.last_telemetry, 1) if self.last_telemetry else None
        out["last_telemetry_age"] = age
        out["upstream_errors"] = self.upstream_errors
        out["upstream_tls"] = self.tls_status()
        out["pending"] = {kind: write.public() for kind, write in self.pending.items()}
        out["last_write"] = self.last_write
        out["health"] = self.health()
        out["queue_file"] = str(self.queue_file)
        out["queue_restored"] = self.queue_restored
        out["writes_delivered"] = self.writes_delivered
        out["writes_confirmed"] = self.writes_confirmed
        out["injections_held"] = self.injections_held
        out["cloud_writes"] = self.cloud_writes
        out["last_cloud_write"] = self.last_cloud_write
        out["lan"] = self.lan.status()
        out["peers"] = self.peers.status()
        out["firmware"] = self.firmware.status()
        out["dns"] = {**self.dns.status(), "https_error": self.dns_https_error}
        out["device"] = self.device.status()
        out["offline"] = self.offline.status()
        out["heater"] = self.heater.status()
        out["heaters"] = self.heaters_list()
        out["reporting_mask"] = {"renew": self.mask_renew,
                                 "after_s": self.renew_after(),
                                 "sent": self.lan.masks_sent}
        now = time.time()
        self._update_cloud_down(now)
        out["cloud_probe"] = {
            "enabled": self.cloud_probe, "after_s": self.cloud_probe_after,
            "probes": self.cloud_probes, "result": self.cloud_probe_result,
            "down": self.cloud_down_since is not None,
            "down_s": (round(now - self.cloud_down_since)
                       if self.cloud_down_since is not None else None),
            "times_down": self.cloud_down_count}
        out["freeze_reconnect"] = {"enabled": self.freeze_reconnect,
                                   "reconnects": self.freeze_reconnects,
                                   "last": self.last_freeze_reconnect}
        # A stale tank is one that has not posted in 10 minutes: the module is
        # event-driven, and a satisfied tank goes quiet for long stretches.
        out["fresh"] = bool(self.last_telemetry) and (time.time() - self.last_telemetry) < 600
        unchanged = (round(time.time() - self.contents_changed)
                     if self.contents_changed else None)
        out["decode_errors"] = self.decode_errors
        out["ingest_errors"] = self.ingest_errors
        out["stray_headers"] = self.stray_headers
        out["contents_unchanged_s"] = unchanged
        out["contents_stale_after_s"] = self.stale_after()
        out["contents_stale"] = bool(out["fresh"] and unchanged is not None
                                     and unchanged >= out["contents_stale_after_s"])
        if out["contents_stale"]:
            # The last report's activity can't be trusted: the module resends a
            # cached snapshot while the heater may have stopped (or started).
            # Tank temperatures stay, as the last known values.
            for key in FROZEN_UNKNOWN:
                if key in out:
                    out[key] = None
            out["frozen_unknown"] = list(FROZEN_UNKNOWN)
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
            self.dial_log.append([now, now])
        self._dial["last"] = now
        self.dial_log[-1][1] = now
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
        polls = sum(d["polls"] for d in dials)
        silent = now - self.last_telemetry if self.last_telemetry else None
        interval = self.reporting_interval_s or IDLE_INTERVAL_DEFAULT
        evidence = {
            **self._cloud_history(now),
            "dials": len(dials),
            "polls": polls,
            "median_telemetry_gap_s": None if median_gap is None else round(median_gap),
            "silent_s": None if silent is None else round(silent),
            "reporting_interval_s": interval,
        }
        unobservable = ["ops keepalives (UDP, gateway-side)", "GraphQL write status (app API)"]

        def degraded(reason: str, remediation: str, extra: dict | None = None,
                     code: str | None = None) -> dict:
            out = {"state": "degraded", "reasons": [reason], "remediation": remediation,
                   "evidence": {**evidence, **(extra or {})}, "unobservable": unobservable}
            if code:
                out["code"] = code             # machine-readable: HA raises a repair on it
            return out

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

        # The cloud-echo probe caught it directly: the module acked a LAN write
        # and passed nothing on to the cloud.
        self._update_cloud_down(now)
        if self.cloud_down_since is not None:
            down = now - self.cloud_down_since
            return degraded(
                "cloud side down: the water heater acked a local write but didn't "
                f"pass it on to Ayla ({down / 60:.0f} min ago)",
                "Home Assistant is unaffected (Local Connect), but Ayla and the "
                "A.O. Smith app get no updates. Turn the water heater's network "
                "access off for about 90 seconds, then on (the integration can do "
                "this for you with a network switch), or flip its breaker off for "
                "~30 seconds, then on. This can follow a relay outage such as a "
                "host reboot.",
                {"cloud_down_s": round(down)}, code="cloud_down")

        # The cloud side stopped while Local Connect carries on. Seen after the
        # relay was unreachable (a host reboot, an add-on restart): libada drops
        # the cloud as a destination after a failed connect and, with its UDP
        # notification channel up, doesn't poll -- 25+ min without a single SYN
        # on the wire, until a power cycle. Positive evidence only: telemetry
        # contents changing over LAN, which a healthy module would also post to
        # the cloud, against no cloud request at all for CLOUD_IDLE_AFTER.
        cloud_idle = self._cloud_idle(now)
        if cloud_idle is not None:
            quiet, changes = cloud_idle
            return degraded(
                f"cloud side idle: no request to Ayla for {quiet / 60:.0f} min while "
                f"Local Connect delivered {changes} changing reports",
                "Home Assistant is unaffected (Local Connect), but Ayla and the "
                "A.O. Smith app get no updates. Flip the water heater's breaker off "
                "for ~30 seconds, then on: it signs in to Ayla again. This can "
                "follow a relay restart or host reboot.",
                {"cloud_quiet_s": round(quiet), "lan_changes": changes}, code="cloud_idle")

        # Silence well beyond the interval it advertised for itself. Gentler
        # remediation than a breaker: overrunning a six-hour cadence is suspicious but
        # not proof, and reconnecting the client is cheaper than a power cycle.
        if silent is not None and silent > interval + 900:
            return degraded(
                f"silent {silent / 3600:.1f} h, beyond its own ~{interval / 3600:.1f} h "
                f"reporting interval", wait)

        # Deliberately NOT a fault: a queued write that is not being delivered.
        # Observed on hardware -- the appliance reported telemetry every ~60 s while
        # calling for heat and never polled, because polling is notify-driven and Ayla
        # had nothing to tell it. Local writes wait for a poll that may not come; that
        # is the cost of not needing the cloud account, not a malfunction.

        # Removed deliberately: the researched "degraded signature" (one request per
        # dial, minutes apart, no polls) turned out to describe a HEALTHY idle module
        # too. Measured on hardware: telemetry-only dials, 60-240 s apart, no polls,
        # while the tank was fine and the appliance was answering normally. The
        # symptom that actually separates wedged from idle -- ops keepalives stopping
        # -- is not visible from a relay in the HTTPS path, so this module does not
        # pretend to see it.

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
            reasons = [f"quiet for {silent / 60:.0f} min; reports every ~{interval} s "
                       f"or on events"]
            if self.pending:
                reasons.append(
                    f"{len(self.pending)} write(s) queued; delivered on the next command "
                    f"poll, which needs Ayla to notify the appliance")
            return {"state": "idle", "reasons": reasons,
                    "evidence": evidence, "unobservable": unobservable}

        return {"state": "healthy", "reasons": [], "evidence": evidence,
                "unobservable": unobservable}

    def _last_cloud_request(self) -> float:
        return max([d["last"] for d in self._dial_shape()], default=0.0)

    def maybe_probe_cloud(self) -> None:
        """Called on every LAN poll: send the mask as a cloud-echo probe when
        the cloud has been quiet long enough (5 min after start with nothing
        heard, then CLOUD_PROBE_AFTER), at most once per CLOUD_PROBE_AFTER."""
        if not self.cloud_probe or not self.lan.session_verified or self.lan.mask_pending:
            return
        now = time.time()
        self._update_cloud_down(now)
        sent = self.lan.last_mask_sent
        if sent and self.lan.mask_acked < sent and now - sent < CLOUD_PROBE_ACK_WAIT_S:
            return                                  # one still in flight
        if sent and now - sent < self.cloud_probe_after:
            return
        last = self._last_cloud_request()
        quiet = now - max(last, self.started)
        wait = CLOUD_PROBE_FIRST_S if last < self.started else self.cloud_probe_after
        if quiet >= wait and self.lan.request_mask():
            self.cloud_probes += 1
            _LOGGER.info("cloud probe: no cloud request for %d min; sending the reporting "
                         "mask to see whether the heater passes it on", quiet // 60)

    def _update_cloud_down(self, now: float) -> None:
        """Turn the last mask's ack (or its echo) into a verdict."""
        sent, acked = self.lan.last_mask_sent, self.lan.mask_acked
        last = self._last_cloud_request()
        if self.cloud_down_since is not None:
            if last >= self.cloud_down_since:       # any request since: it's back
                down = now - self.cloud_down_since
                self.cloud_down_since = None
                self.cloud_probe_result = "echoed"
                _LOGGER.warning("cloud probe: the heater is reaching the cloud again "
                                "(after %d min)", down // 60)
            return
        if not sent or acked < sent:
            return
        if last >= sent:
            self.cloud_probe_result = "echoed"
        elif now - acked >= CLOUD_PROBE_GRACE_S:
            self.cloud_probe_result = "missing"
            self.cloud_down_since = acked
            self.cloud_down_count += 1
            _LOGGER.warning("cloud probe: the heater acked the reporting mask but didn't "
                            "pass it on to the cloud; its cloud side is down")

    def _cloud_history(self, now: float) -> dict:
        """Cloud dials over the last 24 h, the time since the last request, and
        the longest stretch without one (from the relay's start, if later)."""
        horizon = now - 86400
        while self.dial_log and self.dial_log[0][1] < horizon:
            self.dial_log.popleft()
        start = max(self.started, horizon)
        edges = [start] + [t for dial in self.dial_log for t in dial] + [now]
        gaps = [max(0.0, b - a) for a, b in zip(edges[::2], edges[1::2], strict=True)]
        last = self.dial_log[-1][1] if self.dial_log else None
        return {"cloud_dials_24h": len(self.dial_log),
                "cloud_quiet_s": round(now - (last if last is not None else self.started)),
                "longest_cloud_gap_24h_s": round(max(gaps))}

    def _cloud_idle(self, now: float) -> tuple[float, int] | None:
        """(seconds without a cloud request, LAN content changes meanwhile), if
        that looks like a stopped cloud side; else None."""
        if not (self.lan.enabled and self.lan.registered):
            return None
        last = max([d["last"] for d in self._dial_shape()], default=0.0)
        since = max(last, self.started)
        quiet = now - since
        changes = [t for t in self.lan_changes if t > since]
        if (quiet >= CLOUD_IDLE_AFTER and len(changes) >= 2
                and now - changes[-1] < 900):              # and it's still happening
            return quiet, len(changes)
        return None

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
        if not isinstance(body, dict):
            body = {}
        try:
            # Clamped: each nudge 401s a module request, so a large count would
            # wedge the module's cloud connection (pre-publish review).
            self.nudges_left = max(0, min(3, int(body.get("attempts", 1))))
        except (TypeError, ValueError):
            return web.json_response({"error": "attempts must be 0-3"}, status=400)
        self.nudge_on_telemetry = bool(body.get("on_telemetry", False))
        return web.json_response({"nudges_left": self.nudges_left,
                                  "on_telemetry": self.nudge_on_telemetry})

    def _pinnable_dsn(self, request: web.Request) -> str | None:
        """The DSN if this untrusted request could prove it is the module.

        Only a GET of /dsns/<DSN>.xml from a private IPv4 address that is not a
        gateway, and only while LAN mode is on and no module_ip is configured.
        """
        if not self.lan.enabled or self.lan.module_ip_configured or request.method != "GET":
            return None
        found = DSN_PATH_RE.search(request.path)
        if not found:
            return None
        peer = normalize(request.remote)
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return None
        if (address.version != 4 or not address.is_private or address.is_loopback
                or peer in self.peers.gateways):
            return None
        return found.group(1)

    async def _route(self, request: web.Request) -> web.Response:
        """The module's requests; telemetry gets decoded, polls may get a write."""
        dsn = None if request.get(TRUSTED, True) else self._pinnable_dsn(request)
        if dsn:
            # Forwarded verbatim; only Ayla's 200 (the module's credentials
            # checked) pins the peer, and then the reply gets the usual rewrite.
            peer = normalize(request.remote)

            def pin_then_rewrite(payload: bytes) -> bytes | None:
                if self.lan.pin_module(peer, dsn):
                    return self._rewrite_dsn(payload)
                return None

            return await self.proxy(request, transform=pin_then_rewrite)
        if not request.get(TRUSTED, True):
            # Not the module or the gateway: forward untouched, with none of
            # the local behavior (the key, telemetry, writes, DSN rewrite).
            return await self.proxy(request)
        self._note_request(request.path)
        found = DSN_PATH_RE.search(request.path)
        if found:
            self.heater.adopt(found.group(1))
            self.device.note_dsn_id(found.group(1))
        if (request.method == "POST" and "/properties/" in request.path
                and "superBlockDyn1" not in request.path):
            self.device.note_upload(request.path, await request.read())
        target = self.firmware.download_target(request.host, request.path)
        if target is not None:
            return await self.fetch_firmware(request, target)
        if "ota_status" in request.path:
            # The agent reporting on an update: worth keeping, then Ayla's.
            self.firmware.note_status(request.query_string)
        if self.lan.enabled and request.path.endswith("/lan.json"):
            return web.json_response(self.lan.lan_json())
        if "superBlockDyn1" in request.path:
            if self.nudges_left and self.nudge_on_telemetry:
                # An idle module only ever POSTs telemetry, so this is the only
                # request a 401 can reach. Its cue to re-register -- and a
                # re-registration fetches the configuration a write rides on.
                self.nudges_left -= 1
                return web.Response(status=401, text="")
            return await self.telemetry(request)
        if "commands.xml" in request.path:
            path_qs = request.path_qs
            return await self.proxy(
                request, transform=lambda payload: self._transform_commands(payload, path_qs))
        if request.method == "PUT" and "lanip.json" in request.path:
            # The module's reverse-REST ack: /devices/<key>/lanip.json?cmd_id=N&status=M
            if "status=204" in request.query_string or "status=200" in request.query_string:
                self.lanip_push_acked = True
                self.lan.needs_key_push = False
                self.lan.exchanges_since_update = 0
                _LOGGER.warning("LANIP PUSH ACKED: %r -- the module saved our key",
                                request.query_string)
            else:
                _LOGGER.warning("lanip command response: %r", request.query_string)
            # Our command, our ack: Ayla never issued it, and answered the
            # forwarded ack with a 404 in the live repair. Keep it local.
            return web.Response(status=200, text="")
        if "/dsns/" in request.path:
            if self.dsn_lan_rewrite or self.lan.enabled:
                return await self.proxy(request, transform=self._rewrite_dsn)
            return await self.proxy(request, transform=self.note_config)
        if self.nudges_left and request.method == "GET":
            self.nudges_left -= 1
            # No body, and nothing forwarded: this is the module's cue to
            # re-register, not a failure to paper over.
            return web.Response(status=401, text="")
        return await self.proxy(request)


if __name__ == "__main__":
    # The entry point is icomm_hub.py (#18); this keeps `python icomm_relay.py`
    # working for setups that still start it by this name.
    import runpy
    runpy.run_module("icomm_hub", run_name="__main__")
