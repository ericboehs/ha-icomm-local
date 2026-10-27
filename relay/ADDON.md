# iCOMM Local Relay

See the [repository README](../README.md) for what this does, why the module
accepts a self-signed certificate, and how to steer the water heater here.

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

Keys are generated into `/data/certs` on first start and never leave the box.

**Updating or restarting this add-on** can leave the water heater's cloud side
idle afterwards.
- **Home Assistant is unaffected,** but the A.O. Smith app goes stale.
- **After 30 minutes** Home Assistant raises *Water heater isn't reaching Ayla*.
- **The fix:** flip the heater's breaker off for about 30 seconds, then on.

See the README, *Important: the relay is the water heater's route to the cloud*.
