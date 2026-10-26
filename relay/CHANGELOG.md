# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). The add-on (`relay/`) and the
integration (`custom_components/icomm_local/`) share one version.

`relay/CHANGELOG.md` is a copy of this file: the Home Assistant add-on update
dialog reads it from the add-on folder. CI fails if the two differ.

## [Unreleased]

### Changed
- **"Heat deficit" is now "Energy to setpoint".**
  - **The entity ID stays `…_heat_deficit`,** on fresh installs too, so
    dashboards and automations keep working.
  - **What it is:** the controller's estimate of the **electric** energy still
    needed to reach the setpoint in the current mode. It counts down at the
    heater's nominal power (356 W → 6 Wh/min, 4500 W → 75 Wh/min), so it isn't
    heat in the water. Heating 50 gal by 19 °F takes ~2.3 kWh of heat; the
    sensor read 420 Wh.
- **Energy capacity has a simple shape:** 30 Wh per °F of setpoint above
  ~70 °F in heat pump mode (1812 Wh at 130 °F, 2412 at 150), and about 3×
  that in electric mode.
- **Hot water level's docs no longer say it's "more cautious than the app".**
  Against the 33 app-labelled captures it doesn't track availability at all:
  - high in electric with 2,850–5,100 Wh to go;
  - medium at 120–300 Wh in heat pump;
  - both low and high for one 906 Wh state.

  *Hot water available %* matches the app in heat pump mode too (18 of those
  captures), so it stays as is.

- **Frozen data is caught sooner while heating.** If the last report said
  heating and nothing has changed for 20 minutes, the data is frozen. Idle
  is still 90 minutes. The longest healthy gap seen while heating was
  10 minutes; idle reached 52.
- **While frozen, power, operating state and heating element show as
  unknown.** The heater's module keeps resending a cached snapshot. On
  10-26 it claimed "heating, 356 W" for 1 h 51 m after the compressor had
  stopped. Tank temperatures stay, as the last known values.
- **The frozen-data repair no longer promises** that the relay's
  reporting-mask renewal always brings data back: 5 renewals didn't on 10-26.

### Added
- **Ambient temperature,** confirmed against the heater's maintenance display
  (hold Up+Down for 3 s).
  - **Source:** P06 read 61 → 62 °F while block 1 offset 14 read 58 → 63.
    It's a new key, `ambient`; "Air at unit" stays retired, since it was the
    energy capacity.
  - **Behaviour:** it reads the room while the compressor is off (67 °F
    overnight), and runs a few degrees low while the compressor draws air
    past it.
  - **The same read confirmed** the expansion valve (P07 = 480), the tank top
    and bottom thermistors, and the element flags.
- **`experimental_freeze_reconnect` add-on option** (off by default). When 2
  reporting-mask renewals haven't unfrozen the data, it reconnects the LAN
  session once per freeze (new registration and key exchange), then keeps
  renewing. The state API reports `freeze_reconnect`.
  - **Why try it:** the one reconnect seen during a freeze was followed by
    live data within 11 minutes.
  - **Why it should be safe:** none of 17 relay reconnects outside a freeze
    started one. The 10-26 freeze began after the heater restarted its own
    session.
- **Time to recover:** minutes of heating left, energy to setpoint ÷ power.
  It's 0 once satisfied, and unknown between cycles, when there's no rate.

## [0.7.4] - 2026-10-25

**"Air at unit" is removed: it was never the air temperature.** It showed the
heater's energy capacity (Wh) as °F, and now appears as *Energy capacity*.
The upgrade removes the old entity; dashboards and automations that used
`sensor.icomm_water_heater_air_at_unit` need updating. The real ambient
temperature will return once its field is confirmed.

### Added
- **New sensors** from a cross-check with
  [stumpylog/aosmith-ble](https://github.com/stumpylog/aosmith-ble)'s map of
  the controller's internal blocks (research phase 49):
  - **Energy:** the heater's lifetime total, which works in HA's Energy
    dashboard. It's the running total of the nominal power it reports, so it
    reads above a meter on the circuit.
  - **Energy capacity:** what "Air at unit" really was. It steps with the
    setpoint and mode (2412 → 1812 Wh at 150 → 130 °F).
  - **Operating state:** idle or heating.
  - **Hot water level:** the heater's own low/medium/high. It's more cautious
    than the app: "high" only at a zero deficit, and "medium" where the app
    shows 100 %. *Hot water available %* still matches the app.
  - **Expansion valve position,** a diagnostic (handbook display P7).
  - **Fault:** the controller's fault code with the manufacturer's title
    ("Air filter is dirty", "Compressor error", ...), from the `faultCode`
    property.
- **Relay `/state` fields:** `energy_wh`, `energy_capacity_wh`,
  `operating_state` (and `_raw`), `hot_water_level`, `eev_pulses`, and the
  device fact `fault_code`.

### Removed
- **Relay `/state` field `air` and the *Air at unit* sensor** (see above).

## [0.7.3] - 2026-10-25

**Upgrade both the add-on and the integration.** Updating the add-on restarts
the relay, and that is exactly what can leave the water heater's cloud side
idle (below). If the A.O. Smith app matters to you, update when a breaker
cycle is convenient. Thirty minutes later this version tells you whether one
is needed.

### Added
- **Relay health: "cloud side idle"** (`degraded`, `code: cloud_idle`).
  - **When:** Local Connect has delivered reports whose contents changed, and
    the heater hasn't contacted Ayla for 30 minutes (`CLOUD_IDLE_AFTER`).
  - **Why:** seen live after the relay was unreachable for a host reboot and
    an add-on restart. The heater's Wi-Fi module stopped contacting Ayla
    entirely (no packets to :443 at the router) for over 80 minutes, until a
    breaker cycle. Home Assistant was fine; the app went stale.
  - **Before:** health showed `unknown`, "too few dials observed yet".
  - **No false alarms when idle:** unchanged reports (a satisfied tank) don't
    count.
- **Cloud history in every health report:** `cloud_dials_24h`,
  `cloud_quiet_s` and `longest_cloud_gap_24h_s`. They're also attributes on
  the *Relay health* sensor, so a recurrence shows in history and logs, not
  only in the repair. They're kept in memory, so a relay restart starts them
  over and the gap counts from the start.
- **A Home Assistant repair, per heater:** *Water heater isn't reaching
  Ayla*. It explains that HA is unaffected, gives the breaker fix, and clears
  by itself once the heater signs in again.

### Changed
- **`docs/dashboard.yaml` shows everything the integration has.** New:
  - badges for relay health, mode and heating element;
  - the setpoint, active target and maximum, and any pending write;
  - model, serial number, DSN, and both firmware versions;
  - why relay health says what it does, and whether Local Connect is
    registered;
  - the tank average's weight on top;
  - a 24-hour timeline of mode and heating element.

  Vacation days show only in vacation mode, and the last write's status only
  while a write is pending. A new test checks that every attribute row names
  an attribute the entity really has.
- **Docs:** the README section *Important: the relay is the water heater's
  route to the cloud* (formerly about NAT rules only), the add-on page and
  `docs/BEHAVIOR.md` cover the restart risk and the fix. `BEHAVIOR.md` also
  corrects its earlier claim that a relay restart has no lasting effect.

## [0.7.2] - 2026-10-25

### Added
- **Removing a heater** (#18).
  - **In HA:** deleting a heater's device (device page, **⋮ → Delete**) has
    the relay forget it too, then removes its entities and frees its number.
    It's refused while the heater is still reporting, since it would come
    back on its next boot.
  - **Relay:** `DELETE /heaters/<dsn>?confirm=<dsn>` deletes the heater's
    directory (key, saved config, queue). For the primary it also deletes the
    `PRIMARY` marker and the single-heater files kept for downgrades, which
    used to rebuild it. The relay then restarts in-process from what's left
    on disk.
    - 409 (unless `force=1`) while the heater is reporting or has a LAN
      session, or within ten minutes of a relay restart if it hasn't reported
      yet.
    - 409 if `module_ip` or `device_dsn` names the primary.
    - The single-heater files and the marker are deleted only if they're the
      removed heater's own.
    - A stop signal during the restart still stops the relay.
    - The state API answers 503 until the restart finishes.
  - **To replace the primary heater and keep its entities**, forget it on the
    relay only, without deleting the HA device. The new heater then takes
    over the old one's entities. See `docs/MULTI_HEATER.md`.

### Added
- **Multi-heater guide**, [docs/MULTI_HEATER.md](docs/MULTI_HEATER.md) (#18,
  phase 4): adding a heater, telling heaters apart, service targets, the
  primary heater and the relay's per-heater API, and limits.
- **[docs/dashboard-multi.yaml](docs/dashboard-multi.yaml)**: a two-heater
  view with a column per heater and shared 24-hour graphs. A test checks it
  against a two-heater install.

## [0.7.1] - 2026-10-25

### Changed
- **The water heater's current temperature is now the tank average**, not the
  top thermistor. The average is the controller's own tank temperature, the
  one it heats against, so the card now agrees with when the heater runs.
  The top temperature is still its own sensor. The card falls back to the top
  temperature if a report has no average.

## [0.7.0] - 2026-10-25

**Upgrade both the add-on and the integration.** The integration migrates its
entities' unique IDs on first start, with no action needed. Entity IDs,
customised names, history, dashboards and automations are unchanged.

### Added
- **Several water heaters behind one relay** (#18, phase 2, relay only).
  - Each heater gets its own engine: write queue, telemetry, LAN session and
    key, saved config and offline fallback, kept in `/data/heaters/<DSN>/`.
  - How requests are matched to a heater:
    - a config fetch by its DSN;
    - otherwise by the heater's own address (DNS steering);
    - otherwise by Ayla's device ID (NAT steering, where all heaters arrive
      from the gateway);
    - Local Connect callbacks by the caller's address.
  - A new heater is added only when Ayla answers its config fetch with 200,
    and only if it came from the heater itself, the gateway or a
    `trusted_peers` address. The limit is `max_heaters` (default 4); past it,
    a heater works through Ayla only and never touches the others.
  - A heater that takes over another's address (DHCP) is re-pinned on Ayla's
    200, and the address is released from the other heater.
  - New state API: `GET /heaters`, plus `/heaters/<dsn>/{state,raw}` and
    `POST /heaters/<dsn>/{write,write/cancel,lan/reporting-mask,nudge}`.
  - The existing endpoints act on the **primary** heater (the first one,
    recorded in `heaters/PRIMARY`). With two or more heaters, writes through
    them are refused with 409 and the list of heaters.
  - `/state` gains `heaters`.
  - Single-heater installs see no change.
- **One Home Assistant device per heater** (#18, phase 3).
  - Each heater gets its own device and entities: "iCOMM Water Heater",
    "iCOMM Water Heater 2", and so on.
  - A heater that appears later gets its entities without a reload.
  - Writes from each card go to that heater's `/heaters/<dsn>/write`.
  - The stale and frozen-data repairs are per heater.
  - The services `set_setpoint`, `set_mode` and `cancel_writes` take a target
    (entity, device or area). It's required once there are several heaters,
    and the reply then has a section per heater.
  - **Upgrading keeps entity IDs and history.** Once the relay names the
    heater's DSN, the unique IDs move from `icomm_local_<key>` to
    `icomm_local_<DSN>_<key>`, and the device from
    `(icomm_local, water_heater)` to `(icomm_local, <DSN>)`. It's the same
    device, so device-based automations still match.
  - With an older relay that names no DSN, the old keys stay until it does.
  - **A replaced heater** (its old DSN gone from the relay) takes over the old
    one's entities, device and number. Dashboards keep working, and there's no
    dead device beside it.
  - Heater numbers are kept in the config entry, so they don't shift when a
    heater leaves.

### Fixed
- The integration no longer reloads itself when its stored data changes; it
  reloads only when an option changes.

## [0.6.0] - 2026-10-25

### Changed
- **Storage layout: one directory per heater** (#18, phase 1, groundwork for
  multiple heaters; the relay still serves one).
  - The heater's files now live in `/data/heaters/<DSN>/`: its LAN key
    (`lanip.json`), write queue (`pending.json`), device facts and saved config
    reply.
  - Relay-wide files (`firmware.json`, `certs/`) stay in `/data`.
  - It migrates by itself on first start:
    - the key, facts and config reply are **copied**, so downgrading to 0.5.x
      still finds the key and the heater's Local Connect carries on;
    - the queue is **moved**, so an older relay can't replay writes this one
      already delivered.
  - A heater whose DSN isn't known yet lives in `heaters/default/` until its
    first config fetch.
  - `/state` gains `heater: {dsn, dir}`, and `queue_file` points to the new
    location.
  - No other behavior changes.

## [0.5.9] - 2026-10-25

### Changed
- **`reporting_mask_renew` is now on by default.** Phase 47 proved the
  mask alone revives frozen data. After a reboot, with Ayla unreachable and no
  app involved, the data went live about 3 minutes after the relay re-sent the
  app's reporting mask over LAN. So the relay now re-arms reporting by itself
  after 30 minutes frozen, at most once every 30 minutes.
  - **Upgrading:** if you've ever saved the add-on's options, the old `false`
    is stored. Turn `reporting_mask_renew` on in the add-on's Configuration tab.
- The *Water heater data is frozen* repair says the relay re-arms reporting
  automatically, and how many times it has tried. It points to the A.O. Smith
  app only if the warning stays, or if renewal is off.
- `/state.reporting_mask`: `renew`, `after_s`, `sent`.

## [0.5.8] - 2026-10-25

### Added
- **Offline fallback** (add-on option `offline_fallback`, on by default). If
  Ayla is unreachable or answers 5xx when the heater boots, the relay answers
  the boot config fetch itself, the way Ayla does:
  1. a 401 with the server time;
  2. then the last real reply, with a fresh `<unix-time>` and a made-up auth key.

  LAN mode arms as usual, so a power cut during an Ayla outage no longer takes
  local control down until Ayla returns. Once Ayla is back, the heater signs in
  to it again by itself. It only answers the trusted module, for the DSN the
  relay knows. `/state.offline` counts the replies.
- **Model and serial number** on the HA device page and as attributes of the
  controller firmware sensor. They are decoded from `superBlockStatic1`, whose
  ASCII is stored with each 16-bit pair of bytes swapped, and read once over
  LAN, so no reboot is needed.

## [0.5.7] - 2026-10-25

### Fixed
- Firmware and identity no longer wait for the heater's next boot after an
  upgrade or a relay restart:
  - the identity fields are seeded from the last config reply the relay saved;
  - the DSN comes from the saved pin or `device_dsn`;
  - the controller firmware is read once over LAN (a read-only property GET,
    queued after any pending telemetry refresh).

## [0.5.6] - 2026-10-25

### Added
- **Firmware and identity in Home Assistant.**
  - **Controller firmware**: the version the A.O. Smith app shows as
    "Firmware Version", e.g. 2.14. It's reported by the module at each boot,
    over LAN or as a cloud upload.
  - **Wi-Fi module firmware**: the Ayla agent's version, from the config reply.
    Its attributes carry the module hardware, the Ayla model, the DSN and the MAC.
  - Both are diagnostic sensors, so a firmware change gets history. The HA
    device page now shows the firmware and model too.
  - The relay keeps these facts in `device.json`, with no secrets, so they
    survive restarts. `/state` reports them as `device`.
  - `/raw` now also includes the static blocks, where the app's model and
    serial numbers probably live; they aren't decoded yet.

### Fixed
- With DNS steering, the module's first config fetch at every boot (before
  Ayla has checked its credentials) logged a misleading warning: the wrong
  port (`:8444` for a request that came in on 443) and "set module_ip". It's
  now an info line, "waiting for Ayla to confirm the module", and is counted
  in `/state` as `peers.awaiting_pin`. Other untrusted requests are still
  warned about, with the port they actually arrived on and DNS-aware advice.

## [0.5.5] - 2026-10-25

### Added
- **Built-in DNS steering (`dns_server`, off by default)** for routers that
  can't do the NAT rules, which is most mesh and ISP routers. With it on:
  - The relay serves DNS on the host's port 53. It answers the A.O. Smith
    module's cloud name (`<model>-71538015-device.aylanetworks.com`) with the
    host's own address, returns no IPv6 answer for it, and forwards every
    other query byte for byte to `dns_upstream` (default: the host's gateway,
    to avoid a loop through itself).
  - The relay also listens on 443, the port the module dials, alongside
    `listen_port`, so NAT steering keeps working during a switch-over.
  - Setup: point the router's DHCP DNS server at Home Assistant and
    power-cycle the heater once. The 0.5.3 pinning trusts the module's own
    address without `module_ip`.
  - Home Assistant raises an error repair if port 53 or 443 is taken on the
    host (AdGuard, Pi-hole, an SSL proxy).

## [0.5.4] - 2026-10-25

### Added
- **Reporting-mask renewal (opt-in).** Frozen data has a cause: the
  controller's live reporting lapses some hours after the A.O. Smith app was
  last opened, and opening the app re-arms it by sending a
  `periodicParameterSetting` mask. Measured: data frozen for 4+ hours came
  back the second the app opened. The relay can now send the same mask over
  the LAN session:
  - by hand: `POST /lan/reporting-mask` on the state API;
  - automatically: the add-on option `reporting_mask_renew`
    (`REPORTING_MASK_RENEW=1`). After 30 minutes of frozen contents it sends
    the mask, at most once every 30 minutes. Off until confirmed on more
    tanks.
  - The relay learns a newer mask whenever the app sends one, and `/state`
    shows `lan.masks_sent`.

### Changed
- The *Water heater data is frozen* repair now says to open the A.O. Smith app
  (or turn on `reporting_mask_renew`) instead of power-cycling the breaker.

## [0.5.3] - 2026-10-25

### Added
- **"Water heater data is frozen" repair.** The module can keep sending
  reports whose contents never change, re-sending the controller's last
  snapshot. Measured: 3+ hours frozen while the heat pump ran at ~260 W
  (seen on an independent power monitor) and iCOMM said 0 W. A healthy idle
  tank still changes some bytes; the longest identical run seen was 35 minutes.
  - The relay reports `contents_unchanged_s` and `contents_stale` in `/state`
    (stale after 90 minutes of identical reports; `CONTENTS_STALE_AFTER`).
  - Home Assistant raises a warning repair while it's stale, and clears it on
    the next change.
  - Optional: choose a power sensor for the heater's circuit under the
    integration's **Configure**. Then 30 minutes unchanged while it reads
    over 150 W is enough to raise the repair.
- **DNS steering without `module_ip`.** With DNS steering the module connects
  from its own address. The relay now trusts a private IPv4 peer once Ayla
  accepts its `GET /dsns/<DSN>.xml`, which needs the device's own
  credentials. It pins that address to that DSN (or `device_dsn`), persists
  it, and trusts it over Ayla's possibly stale `lan-ip`. A configured
  `module_ip` always wins. `/state` shows `lan.module_ip_source`.
- [docs/DNS_STEERING.md](docs/DNS_STEERING.md): steering with an AdGuard (or
  any DNS) rewrite instead of router NAT, tested end to end.

### Fixed
- Docs: retracted "DNS override does not work, the module uses 1.1.1.1". The
  test network's DHCP was handing out 1.1.1.1; the module follows DHCP DNS.

## [0.5.2] - 2026-10-24

### Changed
- **"Water, tank lower" is now "Water, tank average"**: it was never a
  thermistor. The tank has two, top and bottom. This value is the
  controller's blend of them: (top + bottom) / 2 in electric mode (67 of 67
  reports within 0.5 °F, even with the two 65 °F apart), and about
  (3 × top + bottom) / 4 with the heat pump.
  - The sensor's attributes say so: `computed`, `weighting`, and the live
    `weight_on_top`.
  - Default entity IDs migrate with history: `..._water_tank_lower` →
    `..._water_tank_average`, and a 0.4.12 install's `..._water_tank_bottom`
    goes straight there. **Dashboards and automations aren't rewritten by
    HA**, so point them at `..._water_tank_average`.
  - The water heater's `water_lower` attribute is now `water_average`.
  - The demo dashboard shows Top, Bottom and Average.

### Added
- **A repair when the relay can't verify Ayla's certificate.** To the module,
  a TLS verification failure looks like any outage: it gets synthetic
  replies and carries on. Meanwhile its data stops reaching Ayla, and the
  app goes stale without anything saying why. The relay now tracks these
  failures apart from other errors (`upstream_tls` in `/state`: count, last
  error, last success, `failing`) and logs a warning at most hourly. Home
  Assistant raises an error-level repair while it's failing, and clears it
  on the next verified reply.

## [0.5.1] - 2026-10-24

### Fixed
- **A configured `module_ip` now wins over Ayla's copy.** The heater was
  moved to its own VLAN. At its next registration, Ayla's reply still listed
  the old address, and the relay switched Local Connect back to it. Ayla's
  value is now only used when `module_ip` isn't set.

### Added
- `docs/NETWORK_ISOLATION.md`: putting the heater on its own VLAN so it can
  reach only the relay, DNS and Ayla's notification channel. Tested on a UDM
  Pro. The non-obvious part: Ayla's agent accepts a Local Connect callback
  only on its own subnet, so `relay_ip` is set to the gateway's address on
  the heater's VLAN, forwarded to the relay.

## [0.5.0] - 2026-10-24

### Added
- **Firmware updates wait for your OK.** Ayla sends an update as a command
  (`PUT ota.json`) inside the module's command poll, which passes through
  the relay. The relay now drops that command and records the offer: which
  part it is for (the Wi-Fi module or the heater's controller), the
  version, the size and the download host. Ayla keeps the command queued, so
  it comes back on every poll until you decide.
  - Home Assistant raises a fixable repair for each held update. Submitting
    it allows that exact update; ignoring it keeps the update held.
  - An allowed update's image is fetched from its real host through the
    relay, with the certificate verified. Nothing else is: the steering rule
    sends all of the module's HTTPS to the relay, so without this the
    download could never work.
  - Why hold by default: the agent does not check a signature on a cloud
    update, and new firmware could add the certificate check this project
    relies on.
  - Add-on option `firmware_updates`: `hold` (default) or `allow` (pass
    updates through, as before).
  - `/state` gains `firmware`: the policy, held offers, allowed ones, the
    agent's version from its DSN reply, and the module's last update status
    reports. `POST /firmware/allow {"key": "<type>:<version>"}` allows one.

- **Heating element sensor:** `upper`, `lower` or `off`, from block 1 byte 61
  (`0x04` upper, `0x08` lower). Both elements draw 4500 W, so power alone
  can't tell them apart. Five switches during electric recoveries matched the
  tank sensors every time: the upper element runs until the top of the tank
  is satisfied, then hands over to the lower one. The heat pump's bits
  (`0x01`, `0x02`) stay unnamed until measured; the raw byte is the `flags`
  attribute.

### Changed
- The README's security section now says how the relay verifies Ayla.

## [0.4.14] - 2026-10-24

### Added
- **The Supervisor watchdog now catches a hung relay, not just a crashed
  one.** The image declares a Docker `HEALTHCHECK` that asks the relay's
  loopback `GET /health` every 30 s. After three misses in a row Docker marks
  the add-on unhealthy, and the Supervisor restarts it. There is a 60 s grace
  period at first start.
  - Turn on *Watchdog* on the add-on's Info tab; without it the Supervisor
    ignores the health status.
  - The check follows a changed `state_port`, reading it the same way
    `run.sh` does.
  - It is the native replacement for the add-on `watchdog:` URL, which the
    add-on linter now rejects as obsolete. That URL would not have worked
    here anyway: the Supervisor probes from the hassio network, and the state
    API listens only on loopback.

## [0.4.13] - 2026-10-24

### Fixed
- **Tank sensors relabelled by their measured order.** The sensor shipped as
  "Water, tank middle" is the **bottom-most**: during a shower draw it fell
  132 → 77 °F while the one called "bottom" fell only 133 → 117, because cold
  inlet water arrives at the bottom. It also runs hottest while the heat
  pump's condenser, which wraps the lower shell, is heating. The sensors are
  now **Top / Lower / Bottom**.
  - Existing installs on the default entity IDs are migrated, with history:
    `..._water_tank_bottom` → `..._water_tank_lower`, then
    `..._water_tank_middle` → `..._water_tank_bottom`. Customised IDs are left
    alone.
  - **Dashboards and automations are not rewritten by HA.** A card that showed
    `..._water_tank_middle` must point at `..._water_tank_bottom`, and one that
    showed `..._water_tank_bottom` now shows the true bottom. Use
    `..._water_tank_lower` for the sensor that used to be called bottom.
  - The water heater's attributes are now `water_lower` and `water_bottom`
    (the true bottom).
  - The relay's JSON keys are unchanged (`water_bottom` is the lower sensor,
    `water_mid` the bottom-most) so older and newer halves still agree.

### Added
- `docs/dashboard.yaml`: a ready-made **Water Heater** dashboard view. It
  shows the thermostat, hot water available, power, the three tank
  temperatures, heat deficit, the last app write, and 24-hour graphs. The
  temperature graph includes the active target, so vacation is visible. The
  README explains how to paste it. A test checks that every entity it
  references exists on a fresh install.

### Security
- Every GitHub Action is pinned by full commit SHA, with its release in a
  comment that Dependabot updates along with the SHA. hacs/action and hassfest
  have no maintained releases (2022, 2020), so they are pinned to their branch
  head, and Dependabot is told to ignore them.
- New `weekly` workflow:
  - **`image-cves`** rebuilds the latest release from its tag and scans it with
    Trivy. It keeps one `security` issue open while fixable CVEs exist and
    closes it once a scan is clean. Run it by hand with a `tag` input to scan
    any release. Tested end to end: v0.4.11 (which still shipped pip) opened
    #17, and the clean v0.4.12 closed it.
  - **`branch-pins`** opens an issue when hacs's or hassfest's branch moves
    past the pinned commit.
- Workflows that relied on the repository's default token permissions now
  declare `contents: read`.

## [0.4.12] - 2026-10-24

### Security
- **Log injection (CodeQL `py/log-injection`):** an untrusted LAN peer could
  forge relay log lines by putting `%0a` in a request path, since aiohttp
  decodes the path before it is logged. Client-chosen values (path, method, key
  id) have CR/LF stripped or escaped before logging, and other external strings
  (query strings, LAN property names and values, cloud frames) are logged with
  `%r`.
- **Proxy target (CodeQL `py/partial-ssrf`):** the upstream URL is built with
  yarl from the configured host plus the client's path and query, so no path
  (`//host`, `/@host`, encoded slashes) can change the host. It never could
  in practice, because aiohttp's paths start with `/`; tests now pin it.
- **pip removed from the add-on image** after installing the hash-checked
  lock. The latest pip (26.2.2) bundles urllib3 2.7.0 (CVE-2026-97687,
  CVE-2026-97689, CVE-2026-97688), msgpack 1.1.2 (GHSA-6v7p-g79w-8964) and
  setuptools 70.3.0 (CVE-2025-47273 and two more). The relay never used pip at
  runtime, but the image no longer ships them.
- CI scans the built image with Trivy (HIGH and CRITICAL with a fix fail the
  build) and checks that pip is gone. `trivy-action` is pinned by commit SHA
  (v0.36.0, Trivy v0.76.1) after the 2026 Trivy tag-hijack advisory.
- Repository: Dependabot alerts, Dependabot security updates, secret scanning
  and push protection turned on. The remaining CodeQL notes (test idioms,
  deliberate best-effort `except` blocks) are dismissed with reasons.

## [0.4.11] - 2026-10-24

### Added
- **Active setpoint**: the target the tank is heating to right now, decoded
  from telemetry (block 1, offset 80). In vacation it drops to 50 °F while the
  setpoint keeps the value to return to. The water heater's dial now shows
  50 °F during vacation, as the A.O. Smith app does. A new *Active setpoint*
  sensor and an `active_setpoint` attribute expose it everywhere.

## [0.4.10] - 2026-10-24

### Added
- `GET /raw` on the relay's local state port: the last `superBlockDyn1`
  report as hex, one string per block, with its time and mode pair. For
  mapping registers the decoder doesn't know yet, such as vacation's
  effective target.

## [0.4.9] - 2026-10-24

### Changed
- The add-on runs **Python 3.15** (`python:3.15.1-alpine3.24`, pinned by
  digest), up from 3.12. Package versions are unchanged; the lock stays
  compiled for 3.12 so one hash-checked file serves every Python CI tests.
- CI runs the relay's test suite inside the built add-on image on both amd64
  and arm64, replacing the 3.15 canary.

### Confirmed
- Vacation's first byte is the number of days: the app's "3 days" sent
  `03 02`, the frame the relay had predicted byte for byte, and the tank
  reported `3/2`.

## [0.4.8] - 2026-10-24

### Fixed
- The relay ignored SIGTERM. It runs as PID 1 in the add-on container, and the
  kernel drops SIGTERM for PID 1 unless the process installs a handler, so every
  add-on stop or update waited out Docker's timeout and ended in SIGKILL (exit
  137, seen on the 0.4.7 update). It now stops cleanly on SIGTERM/SIGINT,
  closing the LAN server, its HTTP client and both listeners. Pending writes
  and the LAN key were already saved on every change, so nothing was lost.

## [0.4.7] - 2026-10-24

### Added
- **Vacation mode.** The water heater's away mode sends the app's vacation mode
  (mode bytes `01 02`, captured from the app as "1 day"), and its operation
  reads `off` while vacation is on, as in Home Assistant's own A.O. Smith
  integration. Turning away mode off restores the mode the tank was in before,
  remembered across HA restarts. Heat pump is the fallback when no earlier mode
  is known.
- The mode sensor gains a `vacation` state; it and the water heater expose
  `vacation_days`. `icomm_local.set_mode` accepts `vacation` with an optional
  `days` (1–99). Only 1 day has been seen from the app, and whether the tank
  leaves vacation by itself when the days run out has not been observed.
- The relay decodes vacation in telemetry (`mode_raw` `N/2`) and in app writes
  (*Last app write* shows `vacation` and its days).
- CI tests Python 3.15. A canary job builds the add-on image on
  `python:3.15-alpine` and runs the relay's tests inside it.

### Changed
- Add-on dependency floors raised to what CI and image builds already
  installed: aiohttp 3.14.5, pycryptodome 4.1.0. pycryptodome 4.0's breaking
  changes don't touch AES-CBC, the only primitive the relay uses.
- CI actions: checkout 7, CodeQL 4, setup-python 7, gitleaks-action 3, docker build-push 7,
  setup-buildx 4, setup-qemu 4. Dev tools: pytest 9, pytest-asyncio 1.4,
  ruff 0.17.
- Dependabot opens one grouped PR per ecosystem.
- **Reproducible add-on builds:** the base image is pinned to
  `python:3.12.15-alpine3.24` by digest, and `relay/requirements.txt` is now a
  hash-checked lock of every package, compiled from `relay/requirements.in`
  with `uv pip compile`. CI tests that exact lock, and Dependabot bumps it
  (uv ecosystem) and the base image. Before, each user's build installed
  whatever was newest that day.

## [0.4.6] - 2026-10-24

### Fixed
- During an Ayla outage, every module command poll got a 500 instead of the
  relay's synthetic empty reply (a bad `web.Response` keyword, present since
  the first release). Queued writes could not ride the cloud path while Ayla was
  down. LAN delivery was unaffected.
- A non-JSON reply from the relay (a proxy's HTML error page, a relay
  mid-restart) now gives a readable error in services, setup and the config
  flow instead of a raw `JSONDecodeError`.

### Removed
- The read-back retry from 0.4.4. Live, the module ignored every retried read
  as well (3 of 6 writes, 6 retries), and confirmation still arrived from its
  own report at 4–5 s. The reasoning is in `docs/LAN_MODE.md`.

### Added
- `CHANGELOG.md`, mirrored to `relay/CHANGELOG.md` for the add-on update dialog.
- Test coverage raised to 99% (relay) and 100% (integration); the new tests
  found both fixes above.
- CI: coverage gate (95%) for the relay and the integration, a release workflow
  driven by tags, gitleaks, CodeQL, Dependabot, and multi-arch builds of the
  add-on image.

## [0.4.5] - 2026-10-24

### Fixed
- Stalled LAN polling: if the module keeps accepting heartbeats but stops
  polling for commands (seen once, right after an app write: 5+ minutes with no
  polls), the relay re-registers after `LAN_STALL_AFTER` (default 60 s; `0`
  disables). The fresh session resumes polling.

## [0.4.4] - 2026-10-24

### Added
- A read-back retry at +1.5 s / +3.0 s after a write ack. Removed in 0.4.6
  because it did not work.

## [0.4.3] - 2026-10-24

### Fixed
- A write queued while the module was re-keying its LAN session waited a full
  ~10 s heartbeat. The relay now wakes the module as soon as the new session
  has been verified.

## [0.4.2] - 2026-10-24

### Added
- Wake on write: queueing a write sends an immediate heartbeat with
  `notify=1`, so the module fetches commands at once. Delivery dropped from up
  to 10 s to ~0.1 s.
- *Last app write* sensor: writes made through the A.O. Smith app or its API
  are logged and exposed, including whether a queued local write displaced one.

### Fixed
- Since 0.3.1, writes injected on the cloud fallback path were silently
  discarded unless a LAN-key push was armed, though they still counted as
  delivered.

## [0.4.1] - 2026-10-24

### Fixed
- Heat deficit uses the `energy_storage` device class (`energy` with
  `state_class: measurement` is invalid in Home Assistant).

## [0.4.0] - 2026-10-24

### Added
- *Hot water available* (%), the app's 0/50/100 figure computed locally from
  the controller's energy-deficit counter.
- *Heat deficit* (Wh) and *Water, tank middle* sensors.

## [0.3.9] - 2026-10-24

### Fixed
- Untrusted peers can no longer influence the learned reporting interval.
- Rejected writes retire after 15 minutes even below the delivery cap.
- Late confirmation applies only to writes delivered within the last hour.

## [0.3.8] - 2026-10-24

### Security
- Peer allowlist on the module-facing ports. Any LAN host could previously
  fetch the relay's Local Connect key from `lan.json`. New `trusted_peers`
  option.
- The LAN CBC chain advances only after the HMAC verifies.

### Fixed
- Write queue: oldest-first selection, 3 deliveries in total across LAN and
  cloud, retirement of exhausted writes, new `rejected` status.
- The LAN heartbeat loop survives any exception.

### Changed
- README rewritten for new users.

## [0.3.7] - 2026-10-24

### Changed
- Stale data shows a Repairs warning instead of making the entities
  unavailable. Home Assistant silently drops writes to unavailable entities.

## [0.3.6] - 2026-10-24

### Added
- Write status lifecycle: queued → delivered → acknowledged → confirmed /
  not_applied (0.3.5).
- Keep-alive telemetry reads every 5 minutes while a LAN session is up.

## [0.3.4] - 2026-10-24

### Added
- Automatic repair of a module holding a foreign LAN key (Ayla's
  `PUT lanip.json`).
- Active telemetry read-back after write acks and at session start.

## [0.3.0] - 2026-10-23

### Added
- Local writes and telemetry over Ayla Local Connect, with no A.O. Smith
  account.

### Removed
- The cloud doorbell and its A.O. Smith credentials.

## [0.2.3] - 2026-10-23

### Fixed
- The relay forwards every module path verbatim. `/dsns/` previously got a 404.

## [0.2.2] - 2026-10-23

### Added
- Mode enum sensor and the router steering guide.

[Unreleased]: https://github.com/ericboehs/ha-icomm-local/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/ericboehs/ha-icomm-local/compare/v0.5.9...v0.6.0
[0.5.9]: https://github.com/ericboehs/ha-icomm-local/compare/v0.5.8...v0.5.9
[0.5.8]: https://github.com/ericboehs/ha-icomm-local/compare/v0.5.7...v0.5.8
[0.5.7]: https://github.com/ericboehs/ha-icomm-local/compare/v0.5.6...v0.5.7
[0.5.6]: https://github.com/ericboehs/ha-icomm-local/compare/v0.5.5...v0.5.6
[0.5.5]: https://github.com/ericboehs/ha-icomm-local/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/ericboehs/ha-icomm-local/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/ericboehs/ha-icomm-local/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/ericboehs/ha-icomm-local/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/ericboehs/ha-icomm-local/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.14...v0.5.0
[0.4.14]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.13...v0.4.14
[0.4.13]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.12...v0.4.13
[0.4.12]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.11...v0.4.12
[0.4.11]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.10...v0.4.11
[0.4.10]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.9...v0.4.10
[0.4.9]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.8...v0.4.9
[0.4.8]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.7...v0.4.8
[0.4.7]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.6...v0.4.7
[0.4.6]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.5...v0.4.6
[0.4.5]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/ericboehs/ha-icomm-local/compare/v0.3.9...v0.4.0
[0.3.9]: https://github.com/ericboehs/ha-icomm-local/compare/v0.3.8...v0.3.9
[0.3.8]: https://github.com/ericboehs/ha-icomm-local/compare/v0.3.7...v0.3.8
[0.3.7]: https://github.com/ericboehs/ha-icomm-local/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/ericboehs/ha-icomm-local/compare/v0.3.4...v0.3.6
[0.3.4]: https://github.com/ericboehs/ha-icomm-local/compare/v0.3.0...v0.3.4
[0.3.0]: https://github.com/ericboehs/ha-icomm-local/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/ericboehs/ha-icomm-local/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/ericboehs/ha-icomm-local/releases/tag/v0.2.2
