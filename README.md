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

Safety rules, all tested: setpoints clamp to the ceiling the appliance advertises
(and to a hard cap, so a garbage `max_setpoint` cannot become an unbounded write);
only the newest write per register survives; a stale appliance **holds** writes
rather than injecting into a device that ignores them.

### Restarts are expensive — treat them as rare

Dropping the module's connection makes it go quiet for a long time: measured
recovery after a relay restart is **20+ minutes**, and once it went silent for
about an hour. The module answers ARP and ICMP throughout, so it looks alive on the
network while sending nothing at all — not even its own ops keepalives.

So a relay restart (including an add-on update) has a real cost: the appliance's
cloud bridge is dark for the duration, and writes cannot be delivered. The state
API says so instead of pretending (`health.state == "degraded"` with a remediation),
and a module can also be nudged back sooner by reconnecting it in the router's
client list — on UniFi that is a client kick, which does not interrupt heating.

**The honest limitation:** polls happen only when Ayla notifies the module that a
command exists, so a queued write needs something to ring that bell. A plain
reconnect does not poll (measured), and the local nudge (`POST /nudge`, which
answers a request with 401 to force re-registration) cannot fire while the module is
idle because an idle module issues no GETs — only telemetry POSTs. So today:
writes ride the doorbell a cloud write rings, or wait for a natural poll. A fully
local trigger is the open problem.

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

The module must send its cloud traffic to the relay host. It resolves its
endpoints with public DNS (1.1.1.1 directly — it ignores the gateway's DNS, so
a DNS override will **not** work). The mechanism that does:

```bash
# on the gateway (Ubiquiti example; any iptables router works)
iptables -t nat -I PREROUTING 1 -s <module-ip> -p tcp --dport 443 \
  -m comment --comment icomm-relay -j DNAT --to-destination <relay-host>:8444
iptables -t nat -I POSTROUTING 1 -s <module-ip> -d <relay-host> -p tcp --dport 8444 \
  -m comment --comment icomm-relay -j SNAT --to-source <gateway-ip>
conntrack -D -s <module-ip> -p tcp --dport 443   # break the old connection so it re-dials
```

**Black-hole warning:** if these rules are armed and the relay is down, the
water heater has no cloud at all — silently. Remove the rules if you stop the
relay (`iptables -t nat -D ...` or match on the `icomm-relay` comment).

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
