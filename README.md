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

## Status

Phase 1 (read-only relay + sensors) — working against a live HPS10-50H45DV.
Phase 2 (resilience: degraded-state detection, safe fallbacks) and phase 3
(writes: setpoint and mode, delivered on the module's own polls) are designed in
the research repo's `PLUGIN.md` and land here next.

## License

MIT — see [LICENSE](LICENSE).
