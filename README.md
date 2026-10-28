# ha-icomm-local

Local Home Assistant control of an A.O. Smith iCOMM heat-pump water heater.
Live tank data, and temperature and mode changes over your own network, while
the A.O. Smith app keeps working. No A.O. Smith account or password is used.

<img width="875" height="688" alt="image" src="https://github.com/user-attachments/assets/c74289d4-25b1-4823-bea8-66a4780efdde" />

> **Status: early.** Tested on one **A.O. Smith HPS10-50H45DV**. Other iCOMM
> models (A.O. Smith, State, American, Lowe's Signature 900) probably work,
> but they're untested: if you try changing settings on one, watch the tank.
> Reports are welcome as [issues](https://github.com/ericboehs/ha-icomm-local/issues).

## What you get

- **A water heater entity:** set the temperature and mode (electric, heat
  pump, hybrid) from the thermostat card. Away mode is the app's vacation mode.
- **Sensors:** tank temperatures, hot water available %, live power, energy
  to setpoint, time to recover, heating element, faults and more.
- **Changes confirmed in seconds** by the tank's own data, over your LAN.
- **The A.O. Smith app keeps working** alongside Home Assistant.
- **A ready-made dashboard,** and support for up to 4 water heaters.

The full list is in [docs/USING.md](docs/USING.md).

## How it works

The heater's Wi-Fi module talks to A.O. Smith's cloud (run by Ayla Networks).
You point that traffic at a small Home Assistant add-on. The add-on passes it
on unchanged, reads the tank's data on the way through, and turns on the
module's built-in local connection, so Home Assistant controls the heater
directly. Details: [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).

## What you need

- **Home Assistant OS or Supervised,** for the add-on. On Container or Core,
  run the relay yourself: see
  [docs/CONFIGURATION.md](docs/CONFIGURATION.md#without-the-add-on-ha-container-core-or-another-host).
- **A router where you can change the DHCP DNS server** (most can), or one
  that can do NAT rules.
- **The heater and Home Assistant on the same network.** Separate VLANs work
  with extra router setup: see [docs/NETWORK_ISOLATION.md](docs/NETWORK_ISOLATION.md).
- **The heater's breaker,** to switch it off and on once.

## Install

### 1. Install the add-on

[![Open your Home Assistant instance and show the iCOMM Local Relay add-on.](https://my.home-assistant.io/badges/supervisor_addon.svg)](https://my.home-assistant.io/redirect/supervisor_addon/?addon=3f70756d_icomm_relay&repository_url=https%3A%2F%2Fgithub.com%2Fericboehs%2Fha-icomm-local)

Click the button, then **Install** and **Start**. The defaults are fine.

<details>
<summary>Without the button</summary>

Settings → Add-ons → Add-on Store → ⋮ → **Repositories** → add
`https://github.com/ericboehs/ha-icomm-local`, then install
**iCOMM Local Relay** and start it.

</details>

### 2. Point the heater at Home Assistant

Pick one:

- **DNS (easiest, works with most routers):** turn on the add-on's
  `dns_server` option and set your router's DHCP DNS server to Home
  Assistant. Follow the 3-step
  [quick start](docs/DNS_STEERING.md#quick-start), which covers step 3 too.
- **NAT rules on the router:** give the heater a fixed IP address, then add
  the two rules in [docs/STEERING.md](docs/STEERING.md). Tested on UniFi;
  recipes for other routers are included.

### 3. Power-cycle the water heater once

Switch the heater's breaker off for about 30 seconds, then back on. The tank
keeps its heat and settings. Within a minute or two the add-on's log shows:

```
LAN mode: registered with the module
```

### 4. Add the integration

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ericboehs&repository=ha-icomm-local&category=integration)

Download **iCOMM Local** in [HACS](https://hacs.xyz) and restart Home
Assistant. Then:

[![Open your Home Assistant instance and start setting up iCOMM Local.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=icomm_local)

Keep the suggested URL, `http://127.0.0.1:8124`.

<details>
<summary>Without the buttons, or without HACS</summary>

- **HACS by hand:** HACS → ⋮ → **Custom repositories** → add
  `https://github.com/ericboehs/ha-icomm-local`, type **Integration** →
  download **iCOMM Local** → restart.
- **Without HACS:** copy `custom_components/icomm_local/` into
  `config/custom_components/` and restart.
- **Then:** Settings → Devices & services → **Add integration** →
  *iCOMM Local*.

</details>

For a dashboard, see
[docs/USING.md](docs/USING.md#a-ready-made-dashboard).

## Good to know

- **After a reboot of the Home Assistant host,** the A.O. Smith app can go
  stale. Home Assistant keeps working, and raises *Water heater isn't reaching
  Ayla*. Turn the heater's network access off for 90 seconds, then on, or let
  Home Assistant do it: choose a network switch under the integration's
  **Configure** ([details](docs/USING.md#the-relay-is-the-water-heaters-route-to-the-cloud)).
- **Firmware updates are held** until you allow each one from a repair in
  Home Assistant.
- **If A.O. Smith's cloud is down,** local control keeps working.
- **More than one water heater:** see [docs/MULTI_HEATER.md](docs/MULTI_HEATER.md).

Something not working? See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Uninstall

1. **Undo step 2 first.** With DNS, set the router's DHCP DNS server back to
   what it was **before** removing the add-on, or every device using it loses
   DNS. With NAT, delete the two rules. Then power-cycle the heater.
2. Remove the *iCOMM Local* integration, and its HACS entry.
3. Uninstall the add-on.

The heater keeps an unused local key afterwards, which is harmless. Details:
[docs/USING.md](docs/USING.md#uninstalling-in-detail).

## Documentation

- [USING.md](docs/USING.md): every entity, writes, the dashboard, outages
- [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md): symptoms and fixes
- [CONFIGURATION.md](docs/CONFIGURATION.md): every option, and running without the add-on
- [DNS_STEERING.md](docs/DNS_STEERING.md) and [STEERING.md](docs/STEERING.md): DNS or NAT setup
- [NETWORK_ISOLATION.md](docs/NETWORK_ISOLATION.md): the heater on its own VLAN
- [MULTI_HEATER.md](docs/MULTI_HEATER.md): several water heaters
- [HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md): the design and security
- [LAN_MODE.md](docs/LAN_MODE.md) and [BEHAVIOR.md](docs/BEHAVIOR.md): the Local Connect protocol, and measured appliance behavior
- [REFERENCES.md](docs/REFERENCES.md): the manufacturer's manuals
- [DEVELOPMENT.md](docs/DEVELOPMENT.md) and [RELEASING.md](docs/RELEASING.md): for contributors
- Research and protocol notes: [ericboehs/aosmith-water-heater](https://github.com/ericboehs/aosmith-water-heater)

## License

MIT; see [LICENSE](LICENSE).
