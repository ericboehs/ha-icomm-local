# Using iCOMM Local

Everything the integration gives you, and what to expect day to day. Setup is in the [README](../README.md#install).

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

## Changing the temperature or mode

Set the temperature or mode from the water heater card, or call the services.
A write moves through `queued → delivered → acknowledged → confirmed`, shown
by the *Pending write* sensor and the water heater's `write_status` attribute.
Only the tank's own telemetry counts as **confirmed**. A write gets up to three
deliveries. If the tank accepts it three times but keeps reporting the old
value, it ends as `not_applied`; an error ack ends it as `rejected`. Fifteen
minutes later it leaves the queue. Setpoints are clamped to the maximum the
tank advertises. All statuses: [LAN_MODE.md](LAN_MODE.md#write-status).

If the tank has not reported for 10+ minutes, the entities keep their
last-known values with `stale: true`, and a warning appears under
**Settings → Repairs**. Writes are still accepted. The relay requests fresh
telemetry every 5 minutes while the LAN session is up, so this normally means
something is wrong (see [TROUBLESHOOTING.md](TROUBLESHOOTING.md)).

## A ready-made dashboard

[dashboard.yaml](dashboard.yaml) is a complete **Water Heater**
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

## More than one water heater

One add-on and one integration entry serve up to `max_heaters` (default 4).
The full guide, including a two-heater dashboard, is
[MULTI_HEATER.md](MULTI_HEATER.md).
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
its own. Details: [LAN_MODE.md](LAN_MODE.md), *When Ayla is down*.

## The relay is the water heater's route to the cloud

While the steering is active (NAT rules or DNS), the relay is the water heater's
**only** route to the cloud. Whenever the add-on is stopped, restarting or
updating, the app shows the heater offline and cloud commands wait. The tank
keeps heating to its last setpoint regardless.

**After a reboot of the Home Assistant host** (or any relay outage of a few
minutes), the heater's cloud side may not come back by itself. A quick add-on
restart or update is usually fine.
- **What happens:** its Wi-Fi module stops connecting to the cloud. It still
  looks up the cloud's name, but opens no connection, for hours, until its
  network connection drops (seen live: 8.5 hours).
- **What still works:** Home Assistant keeps live data and control.
- **What doesn't:** the A.O. Smith app shows stale data, and app changes don't
  arrive.
- **How you'll know:** the relay checks. When the cloud has been quiet a while
  (5 minutes after the relay starts, then 30), it sends the heater the
  reporting request the app sends. A healthy heater passes it on to the cloud
  before it confirms it locally; one that confirms it without passing it on
  has lost its cloud side. Health turns `degraded` (`cloud_down`), and Home
  Assistant raises the repair *Water heater isn't reaching Ayla*.
- **The fix:** turn the heater's network access off for about 90 seconds, then
  on. It rejoins and reconnects to the cloud, just as after a power cycle. A
  quick "reconnect" isn't enough: the module has to notice it was gone.
  Flipping its breaker off for about 30 seconds also works.
- **Automatic fix:** under the integration's **Configure**, choose a *network
  switch*: a switch that cuts the heater's network access while it's off. With
  UniFi, add the heater to the UniFi integration's *block client* list (its
  options, *Client controls*) and pick the switch that creates. Home Assistant
  then turns it off for 90 seconds and on again whenever the cloud side is
  down, at most once every 6 hours. If Home Assistant restarts mid-way, it
  turns the switch back on when it starts. With several heaters, the switch
  is used for the first heater only.

## Uninstalling, in detail

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
