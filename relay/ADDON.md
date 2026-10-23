# iCOMM Local Relay

See the [repository README](../README.md) for what this does, why the module
accepts a self-signed certificate, and how to steer the water heater here.

Two things need doing outside Home Assistant:

1. The relay only sees the module if the water heater's traffic is steered to
   this host (`iptables` DNAT + `conntrack` flush on the router — a DNS override
   does **not** work; the module resolves its own endpoints via 1.1.1.1).
2. Point that DNAT at this host's `listen_port` (default 8444). There is no need
   to take port 443 from whatever already owns it — DNAT chooses the port.

Keys are generated into `/data/certs` on first start and never leave the box.
