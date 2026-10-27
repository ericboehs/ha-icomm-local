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
- Sensors:
  - **Tank:** water temperature at the top and bottom, plus the controller's
    blended tank average.
  - **Hot water:** **hot water available %**, the same 0/50/100 figure the
    A.O. Smith app shows, computed locally in every mode. Also the heater's
    own **hot water level** (low/medium/high), whose meaning is still open:
    it doesn't track the app.
  - **Energy:**
    - **energy to setpoint:** the controller's estimate of the electric
      energy still needed, in Wh;
    - **time to recover:** energy to setpoint ÷ power, in minutes;
    - **energy capacity**, in Wh;
    - **lifetime energy,** which works in HA's Energy dashboard. It counts the
      heater's nominal power, so it reads above a meter.
  - **Setpoints and mode:** setpoint, active setpoint (what the tank is heating
    to: 50 °F in vacation), max setpoint, mode, and **operating state** (idle
    or heating).
  - **Ambient temperature:** the heat pump's air thermistor (the service
    menu's P06). It reads the room while the compressor is off, and a few
    degrees under it while the compressor draws air past it.
  - **Power:** **live power draw** (4500 W element, ~350 W compressor, 0 W
    idle), and **which heating element is on** (upper or lower).
  - **Status:** the **fault** code with its title, and the heater's last four
    faults (the service menu's H1–H4) as an attribute; expansion valve position,
    line voltage, Wi-Fi RSSI, relay health, and the pending write's status.
- Services: `icomm_local.set_setpoint`, `icomm_local.set_mode` (including
  `vacation` with an optional `days`), `icomm_local.cancel_writes`.
- Writes go over the LAN and are usually confirmed by the tank's own telemetry
  within a few seconds. Writes made in the A.O. Smith app are recorded too
  (*Last app write*), so app and Home Assistant changes can be told apart. The mobile app and A.O. Smith cloud keep working in
  parallel.

## How it works

The water heater's Wi-Fi module talks to Ayla's IoT cloud over HTTPS. That
traffic is steered to a small relay (a Home Assistant add-on), in one of two
ways:

- **DNS:** the add-on's built-in DNS server answers the module's cloud name
  with Home Assistant's address. No router rules are needed, only the router's
  DHCP DNS setting.
- **NAT:** two rules on a router that supports them.

The relay forwards everything to the real cloud unchanged, so the app keeps
working, and decodes the telemetry as it passes. It also flips the
module's built-in Ayla **Local Connect** feature on, with an encryption key the
relay generates itself. After that, the module polls the relay on your LAN every
~10 seconds, and writes ride that poll. No A.O. Smith account or password is
used anywhere. Details: [docs/LAN_MODE.md](docs/LAN_MODE.md). The manufacturer's service
handbook and Use & Care guide, with what each page confirmed:
[docs/REFERENCES.md](docs/REFERENCES.md).

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
    If you already run AdGuard or Pi-hole, add one rewrite there instead and
    set the add-on's `listen_port` to `443`: the relay only listens on 443 by
    itself while `dns_server` is on. See
    [docs/DNS_STEERING.md](docs/DNS_STEERING.md).
- The Home Assistant host and the water heater on the **same subnet/VLAN**
  (Local Connect calls back to the relay directly). Across VLANs it works,
  but the gateway needs more setup:
  - a DNAT rule for the Local Connect callback and an SNAT rule for the
    relay's calls to the module;
  - `relay_ip` set to the gateway's address on the heater's VLAN;
  - firewall policies that let the heater reach the relay, and the relay
    reach the heater.

  See [docs/NETWORK_ISOLATION.md](docs/NETWORK_ISOLATION.md).
- Access to the water heater's breaker for one power cycle during setup.

## Install

### 0. Before you start

1. Find the water heater's IP address in your router's client list (an
   Ayla/"Espressif"/unnamed device, or match the MAC shown in the iCOMM app's
   device info).
2. **Reserve that IP** (a DHCP reservation/fixed IP). The NAT rules match on
   it. With DNS steering it's optional but recommended: a fixed address is
   easier to firewall and to recognise in the logs.
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

With DNS steering you already did this in step 4 of
[docs/DNS_STEERING.md](docs/DNS_STEERING.md#built-in-dns_server). If the log
lines below are there, skip to step 4.

Turn the water heater's breaker off for about 30 seconds, then back on. The
tank keeps its heat and its thermostat. On boot, the module re-registers through
the relay, takes the relay's LAN key, and starts polling it locally. Within a
minute or two the add-on log shows:

```
DSN rewrite: flipped lan-enabled, local-access-enabled in config reply
LAN mode: module fetched lan.json; answered with our key
LAN mode: registered with the module
```

With DNS steering, `DNS: steering …` and `LAN mode: module <DSN> is at …
(its own address: steered by DNS, not NAT)` come first. The full sequence is
in [docs/DNS_STEERING.md](docs/DNS_STEERING.md#built-in-dns_server).

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
view, showing every entity the integration creates:

- **Water heater:**
  - badges for relay health, mode and heating element;
  - the thermostat with mode buttons;
  - hot water available, power, the element and any pending write.
- **Tank:**
  - top, average and bottom temperatures, the energy to setpoint and the
    time to recover;
  - the setpoint, active target and maximum, and the energy capacity;
  - how much the average weighs the top sensor, vacation days left, and the
    last write's status.
- **Connection:**
  - Wi-Fi, line voltage and the last app write;
  - why relay health says what it does, and whether Local Connect is
    registered.
- **Device:** model, serial number, DSN, controller and Wi-Fi module firmware,
  and the module hardware.
- **Last 24 hours:**
  - tank temperatures against the active target (vacation shows
    as a drop to 50 °F);
  - power, the energy to setpoint and hot water available;
  - a timeline of mode and heating element.

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
| Add-on log shows no requests from the heater at all | the steering isn't reaching the relay | **DNS:** check that the log shows `DNS: steering … for <heater-ip>`. If it doesn't, the heater isn't using Home Assistant for DNS: check the router's DHCP DNS setting (no secondary), then power-cycle the heater. **NAT:** check the router's DNAT counter and the heater IP; break the old connection (STEERING.md: *Checking it works*) or power-cycle the heater |
| Log: `plain-proxying ... from <ip>: not a trusted peer`, and no telemetry in HA | the module arrives from an address the relay does not recognize (e.g. the SNAT source is not the host's default gateway). The heater keeps its cloud connection but gets no local features | If `<ip>` is your router/gateway, add it to the `trusted_peers` option. If it is the heater, power-cycle it so Ayla can confirm it (DNS steering); if that doesn't help, set `module_ip` |
| `lan.last_registration_result: PUT 404` / `POST 404` | Local Connect not armed yet | Power-cycle the heater (install step 3) |
| `PUT 412` | armed, waiting for the key | Wait a minute; if it persists, power-cycle |
| Repair: *Water heater data is frozen* | reports keep arriving but their contents haven't changed for 90 minutes, or 20 while the last report said it was heating (30 with a power sensor showing the heater running). Power, operating state and heating element then show as unknown, because the snapshot's claims about activity can be wrong (seen: "heating" for nearly 2 hours after the compressor stopped). The controller's live reporting lapses hours after the A.O. Smith app was last opened, and after a reboot | Heating isn't affected. The relay tries to re-arm it by itself (`reporting_mask_renew`, on by default). That has brought live data back within minutes, and has also failed to; `experimental_freeze_reconnect` adds a LAN reconnect after 2 failed tries. If the repair stays, **open the A.O. Smith app**, or `POST /lan/reporting-mask` on the state API once. Optional: set a power sensor for the heater's circuit under the integration's **Configure** to catch it sooner |
| `lan.needs_key_push: true` | the module holds a key this relay did not issue | Automatic: the relay pushes its key on the next cloud poll. See [docs/LAN_MODE.md](docs/LAN_MODE.md) |
| Integration setup: *Could not reach the relay* | add-on not running, or a different URL | Start the add-on; use `http://127.0.0.1:8124` |
| App shows the heater offline | relay stopped while the steering is active | Start the add-on, or undo the steering (Uninstall step 1) |
| The A.O. Smith/iCOMM app keeps logging you out | A.O. Smith allows **one login session per account**. Home Assistant's built-in *A. O. Smith* cloud integration on the same account logs the app out, and the app logs it out in turn. Every reload or HA restart is a new login. iCOMM Local uses no account, so it is not the cause | Disable the built-in A. O. Smith integration (iCOMM Local covers its entities locally), or give it a separate account through the app's device sharing |
| A write shows `not_applied` | the tank accepted the frame but did not apply it | Retry later. A transient window of this was observed once; it cleared on its own |

## More than one water heater

One add-on and one integration entry serve up to `max_heaters` (default 4).
The full guide, including a two-heater dashboard, is
[docs/MULTI_HEATER.md](docs/MULTI_HEATER.md).
- **Adding one:** steer it like the first one. With DNS steering that already
  happens; with NAT, add its IP to the rules. Leave `module_ip` and
  `device_dsn` empty, then power-cycle it. The relay adds it once Ayla
  confirms its config fetch.
- **In HA:** each heater is its own device, "iCOMM Water Heater",
  "iCOMM Water Heater 2" and so on, with its own entities, such as
  `water_heater.icomm_water_heater_2`.
- **Services:** `icomm_local.set_setpoint`, `set_mode` and `cancel_writes`
  take a target (a water heater, its device or an area). With one heater the
  target is optional; with several it's required.
- **Upgrading from a single-heater install:** your entities keep their entity
  IDs and history. Only their internal unique IDs move to the heater's DSN.
- **Removing a heater:** delete its device in HA (the relay forgets it too).
  To replace one and keep its entities for the new heater, see the guide.

## When Ayla is down

Local control doesn't need Ayla once the heater is running: telemetry, writes
and the HA entities keep working through an outage (the A.O. Smith app won't).
The weak point is a reboot, because the heater asks Ayla for its configuration
first. With `offline_fallback` on (the default), the relay answers that itself
from the last real reply, so a power cut during an outage doesn't take local
control down with it. When Ayla comes back, the heater signs in to it again on
its own. Details: `docs/LAN_MODE.md`, *When Ayla is down*.

## Important: the relay is the water heater's route to the cloud

While the steering is active (NAT rules or DNS), the relay is the water heater's
**only** route to the cloud. Whenever the add-on is stopped, restarting or
updating, the app shows the heater offline and cloud commands wait. The tank
keeps heating to its last setpoint regardless.

**After an add-on update or restart, or a reboot of the Home Assistant host,**
the heater's cloud side may not come back by itself.
- **What happens:** its Wi-Fi module gives up on the cloud after a failed
  connection and doesn't retry while Local Connect works (seen live: over 80
  minutes, until a power cycle).
- **What still works:** Home Assistant keeps live data and control.
- **What doesn't:** the A.O. Smith app shows stale data, and app changes don't
  arrive.
- **How you'll know:** after 30 minutes the relay's health turns `degraded`
  ("cloud side idle"), and Home Assistant raises the repair *Water heater isn't
  reaching Ayla*.
- **The fix:** flip the heater's breaker off for about 30 seconds, then on.
- **To avoid it:** if you use the app, update the add-on when a power cycle is
  convenient, and check the repair afterwards.

## Uninstall

Do it in this order:

1. **Undo the steering first,** so the heater talks to the cloud directly
   again:
   - **DNS (`dns_server`):** set the router's DHCP DNS server back to what it
     was, typically the router itself. Do this **before** removing the add-on:
     while the router still hands out Home Assistant as DNS, removing the
     add-on cuts DNS for every device that uses it. Devices pick up the change
     when their DHCP lease renews; reconnecting one or rebooting it is faster.
   - **NAT:** disable or delete the two router rules.
   - **Either way:** then power-cycle the heater, or break its existing
     connection.
2. Remove the *iCOMM Local* integration (and its HACS entry).
3. Uninstall the add-on. This deletes its key (`/data/heaters/<DSN>/lanip.json`). The module keeps that
   now-orphaned Local Connect key, which is harmless: the app does not use
   Local Connect. If you reinstall later, the relay detects the foreign key and
   pushes its new one on the next cloud poll. Power-cycle the heater to force
   that poll. Until then, writes go through the cloud path.

## Without the add-on (HA Container, Core, or another host)

```bash
cd relay
./gen-cert.sh                                   # self-signed leaf + DH params
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
QUEUE_FILE=$PWD/state/pending.json STATE_HOST=0.0.0.0 .venv/bin/python icomm_hub.py
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
| `listen_port` / `LISTEN_PORT` | 8444 | port the NAT rule sends the module's HTTPS to. Set it to 443 with your own DNS resolver; with `dns_server` the relay also listens on 443 by itself |
| `state_port` / `STATE_PORT` | 8124 | state and write API for the integration |
| `STATE_HOST` | 127.0.0.1 | state API bind address |
| `upstream` / `UPSTREAM` | `proxy-ingress-usfield.aylanetworks.com` | the real Ayla endpoint every module request is forwarded to (US field) |
| `lan_mode` / `LAN_MODE` | on | Local Connect; off means writes wait for a cloud poll |
| `lan_port` / `LAN_PORT` | 8888 | Local Connect callback port |
| `module_ip` / `MODULE_IP` | learned | heater IP. Normally learned: from its registration with NAT steering, or pinned when Ayla accepts its config fetch with DNS steering. Primary heater only; leave empty with several |
| `relay_ip` / `RELAY_IP` | detected | the address the module should call back on |
| `trusted_peers` / `TRUSTED_PEERS` | — | extra addresses allowed on the module-facing ports; `*` disables the check |
| `device_dsn` / `DEVICE_DSN` | — | shown in `/state`; with DNS steering, only this DSN's module is pinned (otherwise the first one seen). Primary heater only |
| `CONTENTS_STALE_AFTER` | 5400 | seconds of byte-identical reports before `/state` says `contents_stale` |
| `CLOUD_IDLE_AFTER` | 1800 | seconds with no cloud request from the module, while Local Connect still delivers changing reports, before health turns `degraded` (`cloud_idle`) |
| `dns_server` / `DNS_SERVER` | off | serve DNS on the host's port 53: the heater's cloud name resolves to this host (the relay also listens on 443), everything else is forwarded. See [docs/DNS_STEERING.md](docs/DNS_STEERING.md) |
| `dns_upstream` / `DNS_UPSTREAM` | the gateway | where `dns_server` forwards other queries (`ip` or `ip:port`) |
| `DNS_PORT` | 53 | the port `dns_server` listens on |
| `DNS_HOST` | all interfaces | the address `dns_server` listens on (UDP and TCP) |
| `DNS_ANSWER` | detected | the address `dns_server` answers for the heater's cloud name. By default, the host's own address on the route to the asking client |
| `DNS_REWRITE` | `[a-z0-9-]+-71538015-device\.aylanetworks\.com` | the names `dns_server` rewrites, as a case-insensitive regular expression matched against the whole name. Change it if your model uses another hostname (see [docs/DNS_STEERING.md](docs/DNS_STEERING.md#1-the-dns-rewrite)) |
| `max_heaters` / `MAX_HEATERS` | 4 | how many water heaters the relay serves (#18). Each new one is added when Ayla accepts its config fetch; the relay's API has `/heaters`, `/heaters/<dsn>/...`, and `DELETE /heaters/<dsn>?confirm=<dsn>` to forget one. Each heater is its own device in HA |
| `offline_fallback` / `OFFLINE_FALLBACK` | on | if Ayla is unreachable (or answers 5xx) when the heater boots, answer its config fetch from the last real reply, so local control survives a power cut during an outage. See `docs/LAN_MODE.md`, *When Ayla is down* |
| `reporting_mask_renew` / `REPORTING_MASK_RENEW` | on | re-send the app's periodic-reporting mask over LAN when the contents have been frozen for `REPORTING_MASK_RENEW_AFTER` (1800) seconds, or `CONTENTS_STALE_HEATING_AFTER` (1200) while the last report said heating, at most once per that interval |
| `experimental_freeze_reconnect` / `FREEZE_RECONNECT` | off | experimental: when 2 renewals haven't unfrozen the data, reconnect the LAN session once per freeze (a new registration and key exchange), then keep renewing. Seen to help once; relay reconnects haven't been seen to cause freezes (0 of 17) |
| `firmware_updates` / `FIRMWARE_UPDATES` | hold | `hold`: firmware updates wait for you to allow each one (a Home Assistant repair); `allow`: pass them through |
| `QUEUE_MAX_AGE_S` | 86400 | queued writes older than this are dropped |
| `LAN_REFRESH_INTERVAL` | 300 | seconds between keep-alive telemetry reads; 0 disables |
| `LANIP_PUSH` | off | force the LAN-key push (normally automatic) |
| `LANIP_FILE` | `$DATA_DIR/heaters/<DSN>/lanip.json` | where the primary heater's LAN key is kept. For development |
| `LANIP_KEY_ID` | 1 | the key ID used when a new LAN key is generated. A saved key keeps its own ID |
| `LANIP_KEY` | generated | development only: use this LAN key instead of the saved one. Nothing is written to disk |
| `DSN_LAN_REWRITE` | off | flip `lan-enabled` and `local-access-enabled` in the config reply even with `lan_mode` off. An experiment switch; with `lan_mode` on it changes nothing |
| `DEBUG_REQUESTS` | off | log every module request path |
| `CERT_DIR`, `RELAY_CERT`, `RELAY_KEY`, `RELAY_DHPARAMS` | add-on: `/data/certs/...`; run directly: `./certs/...` | TLS material locations. `run.sh` (the add-on) sets `CERT_DIR` to `$DATA_DIR/certs`; the relay and `gen-cert.sh` default to `certs` in the working directory |

## Security

- The module-facing ports bind all interfaces but trust only a few addresses:
  - **8444 (and 443 with `dns_server`):** loopback, the host's default
    gateway (the NAT rules make the module arrive from there), the module's
    own address, and `trusted_peers`. Anyone else is just forwarded to Ayla,
    with no key, telemetry, writes or rewrites.
  - **8888 (Local Connect):** loopback, the module's own address, and
    `trusted_peers`. The gateway is not trusted here. Anyone else gets 403.
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
  `/data/heaters/<DSN>/lanip.json` (0600). Nothing here uses or stores A.O. Smith credentials.
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
pip install --require-hashes -r requirements-dev.txt    # test and lint tools (locked)
ruff check . && python -m pytest                         # relay + unit tests
pip install -r requirements-ha.txt                       # pinned HA test harness
python -m pytest tests_ha -p no:cacheprovider -o asyncio_mode=auto   # inside real HA
```

Releasing: see [docs/RELEASING.md](docs/RELEASING.md).

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
