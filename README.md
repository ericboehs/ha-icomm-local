# ha-icomm-local

Local Home Assistant control of an A.O. Smith iCOMM heat-pump water heater: live
telemetry and setpoint/mode writes over your LAN, while the A.O. Smith app and
cloud keep working.

> **Status: early, single-model.** Verified on one **A.O. Smith HPS10-50H45DV**
> (iCOMM Wi-Fi module, Ayla firmware). Other iCOMM models (A.O. Smith, State,
> American, Lowe's Signature 900) probably use the same cloud. Their register
> layout is unverified, and writes send fixed register frames, so try writes on
> another model only if you are prepared to watch the tank. Reports are welcome as issues.
> Router setup is tested only on UniFi.

## What you get

- `water_heater.icomm_water_heater`: target temperature and mode
  (electric / heat pump / eco = hybrid) from the thermostat card. **Away mode**
  is the app's vacation mode: turning it on sends vacation (1 day), and the
  operation reads `off` while it is on, as in Home Assistant's own A.O. Smith
  integration. Turning it off restores the mode the tank was in before.
- Sensors: water temperature (top and bottom of the tank, plus the controller's
  blended tank average), **hot water
  available %** (the same 0/50/100 figure the A.O. Smith app shows, computed
  locally), heat deficit (Wh), setpoint, active setpoint (what the tank is heating to: 50 °F
  in vacation), max setpoint, mode, **live power draw** (4500 W element, ~350 W compressor, 0 W
  idle), **which heating element is on** (upper or lower), line voltage, air temperature
  at the unit, Wi-Fi RSSI, relay health, and
  the pending write's status.
- Services: `icomm_local.set_setpoint`, `icomm_local.set_mode` (including
  `vacation` with an optional `days`), `icomm_local.cancel_writes`.
- Writes go over the LAN and are usually confirmed by the tank's own telemetry
  within a few seconds. Writes made in the A.O. Smith app are recorded too
  (*Last app write*), so app and Home Assistant changes can be told apart. The mobile app and A.O. Smith cloud keep working in
  parallel.

## How it works (one paragraph)

The water heater's Wi-Fi module talks to Ayla's IoT cloud over HTTPS. Two NAT
rules on your router send that traffic to a small relay (a Home Assistant
add-on). The relay forwards everything to the real cloud unchanged, so the app
keeps working, and decodes the telemetry as it passes. It also flips the
module's built-in Ayla **Local Connect** feature on, with an encryption key the
relay generates itself. After that, the module polls the relay on your LAN every
~10 seconds, and writes ride that poll. No A.O. Smith account or password is
used anywhere. Details: [docs/LAN_MODE.md](docs/LAN_MODE.md).

## Requirements

- **Home Assistant OS or Supervised** (for the add-on). HA Container/Core users
  can run the relay as a plain Python service; see *Without the add-on* below.
- A way to steer the water heater's HTTPS to the relay, either:
  - **NAT on the router:** destination NAT matched on a LAN source address,
    plus source NAT for the hairpin. UniFi (Network 9.3+) is tested. Linux,
    OpenWrt, pfSense and OPNsense should work but are untested.
    See [docs/STEERING.md](docs/STEERING.md).
  - **Or DNS:** turn on the add-on's built-in `dns_server` and set the
    router's DHCP DNS server to Home Assistant. This works with mesh and ISP
    routers that can't do NAT, and needs ports 53 and 443 free on the HA host.
    If you already run AdGuard or Pi-hole, add one rewrite there instead. See
    [docs/DNS_STEERING.md](docs/DNS_STEERING.md).
- The Home Assistant host and the water heater on the **same subnet/VLAN**
  (Local Connect calls back to the relay directly).
- Access to the water heater's breaker for one power cycle during setup.

## Install

### 0. Before you start

1. Find the water heater's IP address in your router's client list (an
   Ayla/"Espressif"/unnamed device, or match the MAC shown in the iCOMM app's
   device info).
2. **Reserve that IP** (a DHCP reservation/fixed IP). The NAT rules match on it.
3. Note your Home Assistant host's IP and your gateway's LAN IP.

### 1. The add-on (relay)

1. Settings → Add-ons → Add-on Store → ⋮ → **Repositories** → add
   `https://github.com/ericboehs/ha-icomm-local`
2. Install **iCOMM Local Relay** and start it. The defaults are fine: module
   listener `8444`, state API `127.0.0.1:8124`, LAN mode on. It generates its own
   TLS certificate and LAN key in `/data` on first start.

### 2. The router (steering)

No NAT-capable router? Follow **[docs/DNS_STEERING.md](docs/DNS_STEERING.md)**
instead, then continue at step 3. Otherwise create the two NAT rules from
**[docs/STEERING.md](docs/STEERING.md)**:
destination NAT from `<heater-ip>` TCP 443 to `<ha-ip>:8444`, and source NAT
for that flow to `<gateway-ip>`. The page has the native UniFi recipe, which
survives reboots, plus the raw iptables version.

### 3. Arm Local Connect: power-cycle the water heater once

Turn the water heater's breaker off for ~10 seconds, then back on. The tank keeps
its heat and its thermostat. On boot, the module re-registers through the relay,
takes the relay's LAN key, and starts polling it locally. Within a minute or
two the add-on log shows:

```
LAN mode: module fetched lan.json; answered with our key
LAN mode: registered with the module
```

### 4. The integration

With [HACS](https://hacs.xyz): HACS → ⋮ → **Custom repositories** → add
`https://github.com/ericboehs/ha-icomm-local`, type **Integration** → install
**iCOMM Local** → restart Home Assistant. Without HACS: copy
`custom_components/icomm_local/` into `config/custom_components/` and restart.

Then Settings → Devices & services → **Add integration** → *iCOMM Local*, URL
`http://127.0.0.1:8124`.

## Using it

Set the temperature or mode from the water heater card, or call the services.
A write moves through `queued → delivered → acknowledged → confirmed`, shown
by the *Pending write* sensor and the water heater's `write_status` attribute.
Only the tank's own telemetry counts as **confirmed**. A write gets up to three
deliveries. If the tank accepts it three times but keeps reporting the old
value, it ends as `not_applied`; an error ack ends it as `rejected`. Fifteen
minutes later it leaves the queue. Setpoints are clamped to the maximum the
tank advertises. All statuses: [docs/LAN_MODE.md](docs/LAN_MODE.md#write-status).

If the tank has not reported for 10+ minutes, the entities keep their
last-known values with `stale: true`, and a warning appears under
**Settings → Repairs**. Writes are still accepted. The relay requests fresh
telemetry every 5 minutes while the LAN session is up, so this normally means
something is wrong (see Troubleshooting).

### A ready-made dashboard

[docs/dashboard.yaml](docs/dashboard.yaml) is a complete **Water Heater**
view with three sections:

- **Water heater:** the thermostat with mode buttons, hot water available, power
  and the heating element.
- **Tank:** top, bottom and average temperatures, heat deficit, the last app
  write, Wi-Fi and line voltage.
- **Last 24 hours:** tank temperatures against the active target (vacation
  shows as a drop to 50 °F), hot water available, and power.

To add it:

1. Open a dashboard, click the **pencil** (Edit dashboard), then **+** to add
   a view.
2. In the view dialog, open the **⋮** menu, choose **Edit in YAML**, and replace
   everything with the contents of `docs/dashboard.yaml`.
3. **Save**.

It uses the integration's default entity IDs. If you renamed any entities,
change them in the YAML to match. A test checks that every entity the file
references exists on a fresh install.

## Troubleshooting

Check the add-on log first, then the relay's state from the HA host:
`curl -s http://127.0.0.1:8124/state | jq '{online, fresh, lan, peers, health}'`

| Symptom | Likely cause | Fix |
|---|---|---|
| Add-on log shows no requests from the heater at all | NAT rules not matching | Check the router's DNAT counter; check the heater IP; break the old connection (STEERING.md: *Checking it works*) or power-cycle the heater |
| Log: `plain-proxying ... from <ip>: not a trusted peer`, and no telemetry in HA | the module arrives from an address the relay does not recognize (e.g. the SNAT source is not the host's default gateway). The heater keeps its cloud connection but gets no local features | If `<ip>` is your router/gateway, add it to the `trusted_peers` option. If it is the heater, set `module_ip` |
| `lan.last_registration_result: PUT 404` / `POST 404` | Local Connect not armed yet | Power-cycle the heater (install step 3) |
| `PUT 412` | armed, waiting for the key | Wait a minute; if it persists, power-cycle |
| Repair: *Water heater data is frozen* | reports keep arriving but their contents haven't changed for 90 minutes (30 with a power sensor showing the heater running). The controller's live reporting lapses hours after the A.O. Smith app was last opened | Heating isn't affected. **Open the A.O. Smith app**: it re-arms reporting within seconds. Or turn on the add-on's `reporting_mask_renew` option (or `POST /lan/reporting-mask` on the state API, once). Optional: set a power sensor for the heater's circuit under the integration's **Configure** to catch it sooner |
| `lan.needs_key_push: true` | the module holds a key this relay did not issue | Automatic: the relay pushes its key on the next cloud poll. See [docs/LAN_MODE.md](docs/LAN_MODE.md) |
| Integration setup: *Could not reach the relay* | add-on not running, or a different URL | Start the add-on; use `http://127.0.0.1:8124` |
| App shows the heater offline | relay stopped while the NAT rules are active | Start the add-on, or disable the NAT rules |
| The A.O. Smith/iCOMM app keeps logging you out | A.O. Smith allows **one login session per account**. Home Assistant's built-in *A. O. Smith* cloud integration on the same account logs the app out, and the app logs it out in turn. Every reload or HA restart is a new login. iCOMM Local uses no account, so it is not the cause | Disable the built-in A. O. Smith integration (iCOMM Local covers its entities locally), or give it a separate account through the app's device sharing |
| A write shows `not_applied` | the tank accepted the frame but did not apply it | Retry later. A transient window of this was observed once; it cleared on its own |

## Important: the NAT rules make the relay mandatory

While the rules are active, the relay is the water heater's **only** route to
the cloud. Whenever the add-on is stopped, restarting or updating, the app shows
the heater offline and cloud commands wait. The tank keeps heating to its last
setpoint regardless.

## Uninstall

Do it in this order:

1. **Disable or delete the router NAT rules.** After this, the heater talks to
   the cloud directly again. Then break its existing connection, or power-cycle
   it.
2. Remove the *iCOMM Local* integration (and its HACS entry).
3. Uninstall the add-on. This deletes `/data/lanip.json`. The module keeps that
   now-orphaned Local Connect key, which is harmless: the app does not use
   Local Connect. If you reinstall later, the relay detects the foreign key and
   pushes its new one on the next cloud poll. Power-cycle the heater to force
   that poll. Until then, writes go through the cloud path.

## Without the add-on (HA Container, Core, or another host)

```bash
cd relay
./gen-cert.sh                                   # self-signed leaf + DH params
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
QUEUE_FILE=$PWD/state/pending.json STATE_HOST=0.0.0.0 .venv/bin/python icomm_relay.py
```

Python 3.10+, on the same subnet as the heater. On a host without
`/proc/net/route` (not Linux), the gateway cannot be detected: set
`TRUSTED_PEERS=<gateway-ip>`. Use `STATE_HOST=0.0.0.0` only if
Home Assistant runs on another machine: the state API is unauthenticated, so
keep it on a trusted LAN. A systemd unit is in `relay/icomm-relay.service`.

## Configuration

Add-on options, or environment variables when running the relay directly:

| Option / env | Default | Meaning |
|---|---|---|
| `listen_port` / `LISTEN_PORT` | 8444 | port the NAT rule sends the module's HTTPS to |
| `state_port` / `STATE_PORT` | 8124 | state and write API for the integration |
| `STATE_HOST` | 127.0.0.1 | state API bind address |
| `upstream` / `UPSTREAM` | `proxy-ingress-usfield.aylanetworks.com` | the real Ayla endpoint every module request is forwarded to (US field) |
| `lan_mode` / `LAN_MODE` | on | Local Connect; off means writes wait for a cloud poll |
| `lan_port` / `LAN_PORT` | 8888 | Local Connect callback port |
| `module_ip` / `MODULE_IP` | learned | heater IP. Normally learned: from its registration with NAT steering, or pinned when Ayla accepts its config fetch with DNS steering |
| `relay_ip` / `RELAY_IP` | detected | the address the module should call back on |
| `trusted_peers` / `TRUSTED_PEERS` | — | extra addresses allowed on the module-facing ports; `*` disables the check |
| `device_dsn` / `DEVICE_DSN` | — | shown in `/state`; with DNS steering, only this DSN's module is pinned (otherwise the first one seen) |
| `CONTENTS_STALE_AFTER` | 5400 | seconds of byte-identical reports before `/state` says `contents_stale` |
| `dns_server` / `DNS_SERVER` | off | serve DNS on the host's port 53: the heater's cloud name resolves to this host (the relay also listens on 443), everything else is forwarded. See [docs/DNS_STEERING.md](docs/DNS_STEERING.md) |
| `dns_upstream` / `DNS_UPSTREAM` | the gateway | where `dns_server` forwards other queries (`ip` or `ip:port`) |
| `reporting_mask_renew` / `REPORTING_MASK_RENEW` | off | re-send the app's periodic-reporting mask over LAN when the contents have been frozen for `REPORTING_MASK_RENEW_AFTER` (1800) seconds, at most once per that interval |
| `firmware_updates` / `FIRMWARE_UPDATES` | hold | `hold`: firmware updates wait for you to allow each one (a Home Assistant repair); `allow`: pass them through |
| `QUEUE_MAX_AGE_S` | 86400 | queued writes older than this are dropped |
| `LAN_REFRESH_INTERVAL` | 300 | seconds between keep-alive telemetry reads; 0 disables |
| `LANIP_PUSH` | off | force the LAN-key push (normally automatic) |
| `DEBUG_REQUESTS` | off | log every module request path |
| `CERT_DIR`, `RELAY_CERT`, `RELAY_KEY`, `RELAY_DHPARAMS` | `$DATA_DIR/certs/...` | TLS material locations |

## Security

- The module-facing ports (8444, 8888) bind all interfaces but trust only the
  module: the gateway's address (the NAT rules make it arrive from there), the
  module's own address, and loopback. On 8444, anyone else is just forwarded to
  Ayla, with no key, telemetry, writes or rewrites. On 8888, anyone else gets 403.
- The state API is unauthenticated and on loopback by default.
- The relay's TLS certificate is self-signed. The module does not validate it.
  The relay *does* verify Ayla's real certificate on the upstream side: the
  chain must end at the Entrust Root G2 shipped in the image (Ayla's Sectigo
  certificate is cross-signed by it) and the name must match. There is no
  fallback to an unverified connection.
- **Firmware updates are held by default.** Ayla delivers them as a command
  through the relay, and the agent does not check a signature on a cloud
  update. A new firmware could also add the certificate check this project
  relies on. Each held update shows up as a repair in Home Assistant; allow
  it there, or ignore the repair to keep it held. Only the allowed image's
  exact URL is fetched, from its real host, with the certificate verified.
- The LAN key is generated locally, never logged, and stored at
  `/data/lanip.json` (0600). Nothing here uses or stores A.O. Smith credentials.
- The module's own Local Connect registration endpoint is unauthenticated by
  Ayla's design, so treat the heater's LAN segment as trusted.

## More

- [docs/STEERING.md](docs/STEERING.md): router rules, reboot behavior, checking it works
- [docs/LAN_MODE.md](docs/LAN_MODE.md): the Local Connect protocol, write status, key repair
- [docs/BEHAVIOR.md](docs/BEHAVIOR.md): measured appliance behavior and write-delivery details
- [docs/NETWORK_ISOLATION.md](docs/NETWORK_ISOLATION.md): the heater on its own VLAN, reaching only the relay
- [docs/DNS_STEERING.md](docs/DNS_STEERING.md): steering with a DNS rewrite instead of router NAT
- Research and protocol notes: [ericboehs/aosmith-water-heater](https://github.com/ericboehs/aosmith-water-heater)

## Development

```bash
pip install --require-hashes -r relay/requirements.txt   # the add-on's exact lock
pip install -r requirements-dev.txt                      # test and lint tools
ruff check . && python -m pytest                         # relay + unit tests
pip install pytest-homeassistant-custom-component
python -m pytest tests_ha -p no:cacheprovider -o asyncio_mode=auto   # inside real HA
```

Add-on builds are reproducible. The Dockerfile pins the base image by tag and
digest, and `relay/requirements.txt` pins every package with hashes. To change
a dependency, edit `relay/requirements.in` and regenerate the lock with the
command in that file's header. Dependabot bumps the base image and the lock
weekly, each as one grouped PR.

Releasing: add a section to `CHANGELOG.md`, copy it with
`cp CHANGELOG.md relay/`, set the same version in `relay/config.yaml` and
`custom_components/icomm_local/manifest.json`, then
`git tag vX.Y.Z && git push origin vX.Y.Z`. The release workflow checks that
all of these agree and that the tests pass, then publishes the GitHub release
from the changelog.

## License

MIT; see [LICENSE](LICENSE).
