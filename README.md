# ha-icomm-local

Local control for A.O. Smith-family water heaters (A.O. Smith, State, American,
Lowe's Signature 900 — anything with an iCOMM module), without giving up the
cloud or the mobile app.

It works by being **a transparent relay** in the path of the water heater's
traffic to Ayla's cloud: every request is forwarded upstream untouched — so the
mobile app keeps working and Home Assistant's own `aosmith` integration keeps
seeing the device — while the telemetry is decoded locally and exposed to
Home Assistant with more fields than the cloud app shows.

Two pieces, one repo:

| Piece | What it is | Where it runs |
|---|---|---|
| `relay/` | a single-file asyncio service: TLS listener, upstream forward, telemetry decoder, localhost state API | any always-on host; also packaged as a Home Assistant add-on |
| `custom_components/icomm_local/` | a Home Assistant integration that polls the relay and creates sensors | your HA config |

## What you get (phase 1, read-only)

Sensors in Home Assistant, straight from the appliance's own telemetry:

water temperature at the top and bottom of the tank, setpoint, max setpoint,
mode (heat pump / hybrid / electric), live power draw in watts, line voltage,
air temperature at the unit, Wi-Fi RSSI — plus an online/fresh indicator.

The power number is *live*, not the app's derived figure: the telemetry carries
the element's 4500 W when energised, the compressor's ~350 W when the heat pump
runs, and 0 when idle.

## Writes (working, cloud-assisted)

Writes are delivered by rewriting the reply to the module's own command poll, so
the appliance never learns where a command came from:

```bash
curl -X POST -d '{"setpoint": 139}' http://127.0.0.1:8124/write
curl -X POST -d '{"mode": "electric"}' http://127.0.0.1:8124/write   # or hybrid, heat_pump
curl -X POST http://127.0.0.1:8124/write/cancel                     # give up on queued writes
```

A queued write is delivered on the module's next `commands.xml` poll, replacing
whatever Ayla had queued for that reply — verified by queueing 139 °F locally while
a cloud write asked for 142 °F: the tank took 139. Delivery is *not* success; the
write stays `pending` until telemetry shows it took effect, because a frame can be
delivered and ignored (the BLE map's mode register at block 26 is exactly that).

Queued writes are **persisted** (`QUEUE_FILE`, `/data/pending.json` in the add-on), so
a restart no longer drops them — it used to, which was demonstrated while waiting on
an idle appliance. The file is written atomically, a corrupt one is ignored rather
than fatal, and a write older than `QUEUE_MAX_AGE_S` (default 24 h) is dropped on
load instead of being delivered as a surprise. Restored writes report
`restored: true` and `queue_restored` in the state API, so it is visible that a
value came back from before the restart.

Safety rules, all tested: setpoints clamp to the ceiling the appliance advertises
(and to a hard cap, so a garbage `max_setpoint` cannot become an unbounded write);
only the newest write per register survives; a stale appliance **holds** writes
rather than injecting into a device that ignores them.

### Silence is usually idleness, not a fault

This appliance reports on events and on its own schedule — the configuration it
fetches at registration says `superBlockDynDataUpdateRate = 21601`, about six
hours — and it only polls for commands when Ayla says a command exists. A tank
holding temperature therefore goes completely quiet on HTTPS for long stretches
while remaining perfectly healthy: its ops keepalives (UDP, straight through the
router) keep flowing the whole time.

That makes silence ambiguous, and the relay's health model refuses to guess:

| Observation | Verdict |
|---|---|
| quiet, tank satisfied, within its advertised interval | `idle` — normal |
| quiet while calling for heat (20 F below setpoint) | `degraded` — intervene |
| quiet longer than its own reporting interval + 15 min | `degraded` — intervene |
| a write queued and the appliance not asking for commands | `degraded` — wait or reconnect |
| one request per dial, minutes apart, no polls | `degraded` — the researched signature |

An earlier version of this file claimed a relay restart costs 20+ minutes of
silence. That was wrong: the silence was the module being idle, and the claim was
an inference from two samples that happened to be quiet for unrelated reasons. The
symptom that actually distinguishes a wedged module — ops keepalives disappearing —
is not visible to the relay at all, which is why it is listed as unobservable
instead of being approximated from timing.

**The honest limitation, now measured rather than suspected:** polls happen only when
Ayla notifies the module that a command exists, so a queued write needs something to
ring that bell. Three candidate local triggers have been tested and all three are
dead:

| Candidate trigger | Result |
|---|---|
| drop the connection (force a re-dial) | re-dials and posts telemetry, **never** fetches `commands.xml` |
| 401 on a GET | an idle module issues no GETs at all, so it never fires |
| **401 on a telemetry POST** | module retries **+6 s**, then resumes its cadence; **zero** `regtoken` fetches, **zero** polls — no re-registration |

That dead end is what LAN mode bypasses: instead of making the module poll the
*cloud*, the relay becomes a *local* command source the module polls every ~10 s
(see "The local doorbell" below). Note
that a *healthy idle* module shows the same trace as the researched "degraded" one —
telemetry-only dials, 60–240 s apart, no polls — so the relay does not claim to
detect a wedge from HTTPS shape alone.


## Home Assistant entities

| Entity | What it is |
|---|---|
| `water_heater.icomm_water_heater` | thermostat card: target temperature + mode (electric / heat_pump / eco = hybrid). Shows the queued target until telemetry confirms it. |
| `sensor.icomm_water_heater_*` | water top/bottom, setpoint, max setpoint, mode, power, line voltage, air at unit, Wi-Fi RSSI |
| `sensor.icomm_water_heater_relay_health` | healthy / idle / degraded / unknown, with reasons, remediation and what the relay *cannot* observe |
| `sensor.icomm_water_heater_pending_write` | what is queued and not yet confirmed |

## The local doorbell: Ayla LAN mode (no account, no cloud writes)

**Since 0.3.0, writes and telemetry work entirely on your LAN.** The relay arms
the module's own Ayla Local Connect feature:

1. When the module re-registers, the relay rewrites two booleans in Ayla's
   registration reply (`lan-enabled`, `local-access-enabled`). Everything else
   in that reply is forwarded byte-identical.
2. Per Ayla's own agent source, an armed module then asks its cloud — the relay —
   for `GET /devices/<key>/lan.json`, and accepts whatever key answers. The relay
   answers with a **key it generated itself**, stored in `/data/lanip.json`
   (0600). There is no A.O. Smith credential anywhere in this project.
3. The module's LAN server arms (`local_reg.json` starts returning 202), the
   relay registers itself and heartbeats every 10 s, and the module polls the
   relay's **local** command endpoint on that heartbeat. Queued writes ride that
   poll; telemetry arrives as encrypted property updates and is decoded exactly
   like the cloud POSTs.

Measured on the real tank: a 140 °F setpoint write delivered, acked
(`ack_status: 200`) and confirmed in decrypted LAN telemetry in **about 6
seconds** — with no A.O. Smith request involved. Cloud telemetry and the mobile
app keep working in parallel, and the cloud poll path remains as the fallback
for a module whose LAN session is not up.

**One-time arming:** the module only asks for its LAN configuration when it
re-registers, so after upgrading to 0.3.0 power-cycle the module once (the
water heater's breaker, ~10 s). The tank keeps its heat and its own thermostat
is unaffected. Watch `GET /state` → `lan`: `registered: true` and
`session: true` mean the local doorbell is up. `registered: false` with
`last_registration_result` of 404 means the module has not taken the rewritten
config yet; 412 means it is armed but still needs the key.

If the module already holds a LAN key this relay did not issue (for example
from earlier experiments), it will not ask for a new one — libada only fetches
`lan.json` when it has *no* stored key. See
[docs/LAN_MODE.md](docs/LAN_MODE.md) for the protocol, the state fields, and
recovery notes.

To opt out of LAN mode entirely, set `LAN_MODE=0` (add-on: `lan_mode: false`);
writes then wait for a cloud poll, as before.

Timing once delivered: electric mode starts heating within ~30 s; heat pump has a
~3 min compressor delay (measured: watts 0 → 356 three minutes after acceptance).

## How it works

The iCOMM module dials `proxy-ingress-usfield.aylanetworks.com` on its own
schedule. The relay:

1. terminates that TLS with a self-signed `*.aylanetworks.com` leaf — the module
   never validates the chain, measured extensively
   ([why that is safe to rely on](https://github.com/ericboehs/aosmith-water-heater/blob/main/ARCHITECTURE.md));
2. forwards every request upstream verbatim — telemetry POSTs, command polls,
   acks — so Ayla's view of the device is unchanged;
3. decodes `superBlockDyn1` as it passes and serves the fields at
   `http://127.0.0.1:8124/state` for the integration to poll.

Writes (setpoint, mode) are phase 2/3: delivered as replacements for the poll
reply the module asks for. The full protocol research — frame formats, register
atlas, the write recipe — lives in the research repo above; this repo is the
product.

## Setup

### 1. The relay

```bash
cd relay
./gen-cert.sh                     # self-signed leaf + 2048-bit DH params
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
LISTEN_PORT=8444 .venv/bin/python icomm_relay.py
```

**On Home Assistant OS / Supervised**, install it as an add-on instead — it gets
persistent storage for its keys (`/data/certs`), host networking, and restarts
with HA:

1. Settings → Add-ons → Add-on Store → ⋮ → **Repositories** → add
   `https://github.com/ericboehs/ha-icomm-local`
2. Install **iCOMM Local Relay**, then start it. Defaults: module listener
   `8444`, state API `8124`.

There is no need to take port 443 from whatever owns it: the DNAT rule below
chooses the port, so `8444` is fine.

On a plain Linux host, use the systemd unit (`relay/icomm-relay.service`).

### 2. Steering the water heater to the relay (required)

The module must send its cloud traffic to the relay host: a destination-NAT rule
on the gateway (module's TCP 443 → `<relay-host>:8444`) plus a source-NAT rule for
the hairpin return path. A DNS override does **not** work — the module asks 1.1.1.1
directly.

**[docs/STEERING.md](docs/STEERING.md)** has the full recipe, including native
UniFi NAT rules (Policy Table → NAT) that survive reboots and firmware updates,
the raw `iptables` version, and how to check it is working.

**Black-hole warning:** while the rules are active the relay is the water heater's
only route to the cloud. If you stop the relay, disable the NAT rules too.

### 3. The integration

With [HACS](https://hacs.xyz) installed (recommended): HACS → Integrations → ⋮ →
**Custom repositories** → add `https://github.com/ericboehs/ha-icomm-local` as an
*Integration*, then install **iCOMM Local** and restart HA.

Without HACS: copy `custom_components/icomm_local/` into your HA
`config/custom_components/` and restart.

Then add the *iCOMM Local* integration and point it at the relay's state URL
(default `http://127.0.0.1:8124`).

## Verified against hardware

- The relay terminates the module's TLS, decodes live telemetry, and forwards
  upstream with zero errors (cloud and app unaffected).
- A **queued cloud command was retrieved through the relay** — proof the poll
  path survives the interception, which is what makes phase 3 (writes) viable.
- The certificate is not validated *at all*: `CN=garbage.example`, no SAN, accepted.
- Add-on entrypoint: fresh install up in ~1 s with keys minted into `/data/certs`.
- **A write delivered by the relay**: local 139 °F beat the cloud's 142 °F, confirmed
  by telemetry 0.6 s later. All seven frames Ayla ever sent are reproduced byte for
  byte by the encoder (that is a test, not a claim).
- 60 tests, no mocking of the appliance: real captured payloads and real handshakes,
  including a TLS 1.2 DHE handshake and a refused TLS 1.3 one.

## Status

- **Phase 1, read-only relay + sensors** — verified against a live HPS10-50H45DV.
- **Phase 3, writes** — mechanism proven (see above); no fully local trigger yet.
- **Phase 2, resilience** — degraded-state detection gates injection, but the
  richer symptom table from the research repo's `PLUGIN.md` is not implemented.
- The Home Assistant integration is read-only: writes are exposed over the relay's
  HTTP API today, not as HA services yet.

## Development

```bash
pip install -r requirements-dev.txt
ruff check . && python -m pytest
```

## License

MIT — see [LICENSE](LICENSE).
