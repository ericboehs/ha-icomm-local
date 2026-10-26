# More than one water heater

One add-on (relay) and one integration entry can serve several A.O. Smith
iCOMM heaters: up to `max_heaters`, 4 by default. Each heater gets its own
Home Assistant device and entities, its own write queue, telemetry, LAN
session and key, and its own offline fallback.

> **Status:** tested with two simulated modules, not yet with two real
> heaters. If you have two, please report how it goes on
> [#18](https://github.com/ericboehs/ha-icomm-local/issues/18).

## Adding a second heater

Install and set up the first heater as in the README. Then:

1. **Steer the new heater to the relay**, the same way as the first:
   - **NAT steering:** both rules match the heater's IP address, so either
     add the new heater's IP to the existing rules (an address group, if
     your router has them) or make a second pair of rules. Give it a DHCP
     reservation first, so the rules keep matching. See
     [STEERING.md](STEERING.md).
   - **DNS steering:** nothing to do if the new heater uses the same DNS
     server (for example, it's on the same network or VLAN). See
     [DNS_STEERING.md](DNS_STEERING.md).
2. **Leave `module_ip` and `device_dsn` empty.** They describe the first
   (primary) heater only. Each heater's address is learned on its own.
   Other heaters are still added if you did set them. But on a fresh install
   with `module_ip` set and no `device_dsn`, whichever heater boots first
   becomes the primary and is tied to that address. So set both, or neither.
3. **Power-cycle the new heater** (breaker off for about 30 seconds). It
   fetches its configuration through the relay, and the relay adds it as soon
   as Ayla confirms that fetch. A minute or so later, its LAN session starts.
4. **In Home Assistant**, the heater's device and entities appear on their own;
   there's no reload or restart. They're named "iCOMM Water Heater 2", with
   entity IDs such as `water_heater.icomm_water_heater_2` and
   `sensor.icomm_water_heater_2_water_tank_average`.

To check from the HA host:

```sh
curl -s http://127.0.0.1:8124/heaters | jq
```

Each heater is listed with its DSN, whether it's the `primary`, its address
(`module_ip`), Ayla `device_id`, `online` and `fresh`, mode, setpoint, tank
top, pending writes, model and serial number, and whether its LAN session is
verified (`lan_verified`). `refused` lists heaters turned away because
`max_heaters` was reached.

### Which heater is which

The device page shows each heater's model and serial number, as printed on
its nameplate. Rename the devices to something that makes sense (say,
"Garage" and "Basement"). When HA offers to rename the entity IDs too,
either answer is fine: history follows the entity either way, but
dashboards and automations use the entity IDs.

Numbers stay with their heaters. The primary is 1, and each other heater
gets the lowest number from 2 up that has never been used. A number is never
handed to a different heater, so if heater 2 goes away, heater 3 stays heater
3 and a new heater gets 4.

## Services and automations

`icomm_local.set_setpoint`, `set_mode` and `cancel_writes` take a target:
a water heater entity, its device, or an area. With one heater, the target is
optional. Once the relay lists several heaters (counting one that's
unreachable), it's required, so a write never reaches the wrong tank. It's
also required if you have more than one relay.

```yaml
action: icomm_local.set_setpoint
target:
  entity_id: water_heater.icomm_water_heater_2
data:
  temperature: 140
```

Several targets in one call each get their own write. The response has one
entry per heater under `heaters`, keyed by DSN. If one write fails, the
earlier ones stay queued and the call returns an error.

```yaml
action: icomm_local.set_mode
target:
  entity_id:
    - water_heater.icomm_water_heater
    - water_heater.icomm_water_heater_2
data:
  mode: heat_pump
```

HA's own `water_heater.set_temperature` and
`water_heater.set_operation_mode` work as usual, including with several
entities: each heater's write goes to that heater.

## Dashboard

[dashboard-multi.yaml](dashboard-multi.yaml) is a **Water Heaters** view
for two heaters. Each heater gets a column with its thermostat, mode buttons,
tank temperatures and power, and a shared graph compares the two tanks.
Add it the same way as the single-heater dashboard (README, *A ready-made
dashboard*). The *Relay health* badge is per heater: it describes that
heater's connection through the shared relay.
- **A third heater:** copy a column and change `_2` to `_3`.
- **Renamed entities:** change the YAML to match.

[dashboard.yaml](dashboard.yaml), the single-heater view, still works for the
first heater.

## Repairs

The *stale data* and *data frozen* repairs are per heater. They name the
heater by its number ("iCOMM Water Heater 2"), even if you've renamed the
device. The relay's own repairs are relay-wide: firmware hold, upstream
certificate, and port in use.

The power-sensor option (the integration's **Configure**) is shared by all
heaters, so it only helps the frozen-data check if every heater is on that
circuit. With several heaters, it's usually best left empty.

## The primary heater, and older tools

The relay's original endpoints (`/state`, `/write` and so on) act on the
**primary**: the first heater it served, recorded in
`/data/heaters/PRIMARY`. Each heater also has its own endpoints:
`/heaters/<dsn>/state`, `/heaters/<dsn>/raw`, and `POST`s to
`/heaters/<dsn>/write`, `/heaters/<dsn>/write/cancel`,
`/heaters/<dsn>/lan/reporting-mask` and `/heaters/<dsn>/nudge`.

**Once the relay holds two heaters, the original POST endpoints (`/write`,
`/write/cancel`, `/lan/reporting-mask`, `/nudge`) are refused with 409**,
together with the list of heaters. That includes a heater that's offline but
still saved on the relay. Scripts that call the relay directly must then use
the `/heaters/<dsn>/...` endpoints. The integration already does. Reads
(`/state`, `/raw`) keep answering for the primary.

## Limits and gotchas

- **`max_heaters` reached:** the add-on log says `not added, max_heaters (N)
  reached; it keeps working through Ayla only`. That heater keeps its cloud
  connection and the A.O. Smith app, but gets no local control. Raise
  `max_heaters` (1 to 8) and power-cycle it.
  - Lowering it below the number of saved heaters drops the extras at startup,
    in DSN order rather than by number.
- **Behind NAT, every heater arrives from the gateway.** The relay tells
  heaters apart by their Ayla device ID, so this works. If you route through
  a proxy or a second router, add it to `trusted_peers`.
- **A heater's IP changes:**
  - **DNS steering:** handled. On its next boot, the heater fetches its
    configuration from the new address. Once Ayla confirms, the relay moves
    its pin there and releases the address from any other heater.
  - **NAT steering:** update the router rules to the new address. Better,
    give each heater a DHCP reservation.
  - **Either way:** a primary with `module_ip` set stays on that address.
- **A heater replaced with a new one:** the new heater joins as an extra
  heater (the next number), and the old one's entities stay, unavailable.
  The relay keeps its first heater as the primary for good.
  - **Single-heater setup:** to have the new heater take over the old one's
    entities and device instead, so dashboards and automations keep working:
    1. Uninstall the add-on and install it again. That clears its data.
    2. Power-cycle the new heater.

    Don't just delete the old heater's directory under `/data/heaters/`: the
    relay rebuilds it from the files it keeps for downgrades.
  - **With several heaters:** reinstalling would make every heater start over,
    so leave the old one's entities until removal is supported.
- **Removing a heater** isn't supported yet. Its entities stay unavailable in
  HA, its directory stays in the relay's `/data/heaters/`, and it still
  counts toward the service-target and 409 rules above.
- **Each heater must have booted once through the relay with Ayla
  reachable** before the offline fallback can cover it.
