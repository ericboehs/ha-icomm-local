# iCOMM Local Relay

Setup is in the [repository README](https://github.com/ericboehs/ha-icomm-local/blob/main/README.md).
How it works, including why the module accepts a self-signed certificate:
[docs/HOW_IT_WORKS.md](https://github.com/ericboehs/ha-icomm-local/blob/main/docs/HOW_IT_WORKS.md).
Every option: [docs/CONFIGURATION.md](https://github.com/ericboehs/ha-icomm-local/blob/main/docs/CONFIGURATION.md).

Two things need doing outside Home Assistant:

1. The relay only sees the module if the water heater's HTTPS is steered to
   this host. Either:
   - **NAT on the router** to this host's `listen_port` (default 8444).
     Recipes, including native UniFi NAT rules that survive reboots, are in
     [docs/STEERING.md](https://github.com/ericboehs/ha-icomm-local/blob/main/docs/STEERING.md).
     DNAT chooses the port, so 443 can stay with whatever already owns it.
   - **Or DNS, with no router rules:** turn on `dns_server` and set the
     router's DHCP DNS server to this host. That needs ports 53 and 443 free
     here; Home Assistant raises a repair if they aren't. Check it with `dig`
     before power-cycling the heater, and remember the router setting before
     you uninstall. An existing AdGuard or Pi-hole can do the rewrite instead,
     with `listen_port: 443`. See
     [docs/DNS_STEERING.md](https://github.com/ericboehs/ha-icomm-local/blob/main/docs/DNS_STEERING.md).
2. Power-cycle the water heater once so it registers through the relay.

Keys are generated on first start and never leave the box:

- the TLS certificate and key in `/data/certs`;
- each heater's LAN key in `/data/heaters/<DSN>/lanip.json`.

**A reboot of the Home Assistant host** (a relay outage of a few minutes) can
leave the water heater's cloud side down afterwards.
- **Home Assistant is unaffected,** but the A.O. Smith app goes stale.
- **Within minutes** the relay notices, and Home Assistant raises *Water
  heater isn't reaching Ayla*.
- **The fix:** turn the heater's network access off for about 90 seconds, then
  on, or flip its breaker. The integration can do the first for you: choose a
  network switch under its **Configure**.

See [*The relay is the water heater's route to the cloud*](https://github.com/ericboehs/ha-icomm-local/blob/main/docs/USING.md#the-relay-is-the-water-heaters-route-to-the-cloud).
