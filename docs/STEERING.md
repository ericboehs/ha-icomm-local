# Steering the water heater to the relay

The relay only sees the module if the gateway redirects the module's HTTPS to it.
This page covers what has to happen, how to make it **survive a router reboot**, and
how to check it is working. Only the recipes marked *tested* have been run against
real hardware; the others are the same two rules expressed for other routers.

## What has to happen

Two NAT rules on the gateway, both scoped to the water heater's IP address:

| Rule | Match | Action | Why |
|---|---|---|---|
| **Destination NAT** | from `<module-ip>`, TCP, destination port 443 (any address) | send to `<relay-host>:8444` | the module dials Ayla's cloud on 443; this lands it on the relay instead |
| **Source NAT** | from `<module-ip>` to `<relay-host>:8444` | rewrite source to `<gateway-ip>` | hairpin: when relay and module share a subnet, without this the relay replies straight to the module, which drops a reply from an address it never dialled |

Then break the module's existing connection once, so it re-dials through the new
rules (`conntrack -D -s <module-ip> -p tcp --dport 443`, or just wait — it re-dials
every few minutes while active, but can stay quiet for hours when idle).

**No NAT-capable router?** Use DNS instead: see [DNS_STEERING.md](DNS_STEERING.md)
(tested). An earlier version of this page said DNS override couldn't work
because the module "uses 1.1.1.1 directly". **That was wrong:** the test
network's DHCP was handing out 1.1.1.1, and the module simply uses the DNS
server DHCP gives it (measured 2026-10-25).

What does **not** work:

- **Plain port forwarding** (WAN → LAN). That is for inbound traffic from the
  internet; this traffic starts on the LAN.

## Black-hole warning — read this before making the rules permanent

While the rules are active, the water heater's *only* route to the cloud is the
relay. If the relay is stopped, uninstalled, or its host is down, the water heater
silently has no cloud at all: the app shows it offline, and cloud-side commands do
not arrive. The tank itself keeps heating to its last setpoint.

A persistent rule makes this easier to forget. When you stop using the relay,
**disable the NAT rules first**. Re-enabling them is one toggle.

## UniFi gateways (UniFi Network 9.3+) — *tested*

Native NAT rules are stored in the controller's configuration and re-rendered by
UniFi itself, so they come back after reboots, re-provisioning, and firmware
updates. A rule added with `iptables` by hand does not: UniFi rewrites its rules on
every provision, and a reboot clears them.

What was tested: with the hand-made `iptables` rules removed, the native rules alone
carried the module's traffic (both counters 0 → 1 on the next dial, fresh telemetry
at the relay, zero upstream errors).

**Router reboot, measured (UDM Pro, 2026-10-23):** the fixed-IP reservation came
back in dnsmasq immediately; the two NAT rules were re-rendered by UniFi Network
about **10 minutes** after the reboot, once the Network application had finished
starting (the SNAT rule rendered twice, which is harmless). The LAN-mode session
between module and relay **never dropped**: it is same-subnet traffic that does
not need the gateway, and heartbeats kept returning 202 throughout. Cloud
traffic (app, A.O. Smith) is what pauses until the rules are back.

Tested on a UDM Pro, UniFi OS 5.1, Network 10.6.106. Menu paths per Ubiquiti:
Network 9.4+ **Settings → Policy Table → Create New Policy → NAT**; Network 9.3
**Settings → Policy Engine → NAT → Create New**. Source-address matching on
Destination NAT needs 9.4.19 or newer.

**Rule 1 — Dest. NAT**

| Field | Value |
|---|---|
| Type | Dest. NAT |
| Name | `icomm-relay: water heater HTTPS to HA relay` |
| Interface | the LAN network the water heater is on (e.g. *Default*) |
| Translated IP address | `<relay-host>` (your Home Assistant IP) |
| Translated port | `8444` |
| Protocol | TCP |
| Source | IP address `<module-ip>` |
| Destination | any address, port `443` |

**Rule 2 — Src. NAT**

| Field | Value |
|---|---|
| Type | Src. NAT |
| Name | `icomm-relay: hairpin return path` |
| Interface | the same LAN network |
| Translated IP address | `<gateway-ip>` (the gateway's address on that network) |
| Protocol | TCP |
| Source | IP address `<module-ip>` |
| Destination | IP address `<relay-host>`, port `8444` |

Give the water heater a **fixed IP** (client → Settings → Fixed IP Address), or the
rules stop matching the next time its lease changes.

The same rules through the controller API, for scripting (`POST
/proxy/network/v2/api/site/default/nat` with a logged-in session and its
`X-CSRF-Token`; values are lower-case where shown):

```json
{"type": "DNAT", "description": "icomm-relay: water heater HTTPS to HA relay",
 "ip_version": "IPV4", "enabled": true, "in_interface": "<network _id>",
 "protocol": "tcp", "ip_address": "<relay-host>", "port": "8444",
 "source_filter": {"filter_type": "ADDRESS_AND_PORT", "address": "<module-ip>"},
 "destination_filter": {"filter_type": "ADDRESS_AND_PORT", "port": "443"},
 "setting_preference": "manual", "logging": false, "exclude": false}
```

```json
{"type": "SNAT", "description": "icomm-relay: hairpin return path",
 "ip_version": "IPV4", "enabled": true, "out_interface": "<network _id>",
 "protocol": "tcp", "ip_address": "<gateway-ip>", "rule_index": 2,
 "source_filter": {"filter_type": "ADDRESS_AND_PORT", "address": "<module-ip>"},
 "destination_filter": {"filter_type": "ADDRESS_AND_PORT", "address": "<relay-host>", "port": "8444"},
 "setting_preference": "manual", "logging": false, "exclude": false}
```

Two API details that cost a retry each: `setting_preference` must be lower-case
(`manual`), and a second rule needs its own `rule_index` (the API rejects a
duplicate). The network `_id` comes from `GET /proxy/network/api/s/default/rest/networkconf`.

UniFi renders these into its own chains, which is how to confirm they are live:

```sh
iptables -t nat -L UBIOS_PREROUTING_USER_HOOK -v -n | grep DNAT     # packet counter climbs per dial
iptables -t nat -L UBIOS_POSTROUTING_USER_HOOK -v -n | grep SNAT
```

## Any Linux/iptables gateway — *tested (UDM shell), not persistent*

```sh
iptables -t nat -I PREROUTING 1 -s <module-ip> -p tcp --dport 443 \
  -m comment --comment icomm-relay -j DNAT --to-destination <relay-host>:8444
iptables -t nat -I POSTROUTING 1 -s <module-ip> -d <relay-host> -p tcp --dport 8444 \
  -m comment --comment icomm-relay -j SNAT --to-source <gateway-ip>
conntrack -D -s <module-ip> -p tcp --dport 443
```

Good for an experiment; gone after a reboot (and, on UniFi, after any provision).
To keep them on a generic Linux router, put the same two rules wherever that
distribution persists NAT (`iptables-save` + `iptables-restore` at boot, or the
`nft` equivalent). Remove by matching the `icomm-relay` comment.

## Other routers — *untested*

The two rules translate directly. Look for "destination NAT" or "port forward" that
can match on a **LAN source address** and **any destination**, plus "outbound /
source NAT" for the hairpin. Notes from other users welcome as issues.

- **OpenWrt (fw4):** a `redirect` with `src lan`, `src_ip <module-ip>`,
  `src_dport 443`, `dest_ip <relay-host>`, `dest_port 8444`, plus an SNAT rule
  (`target SNAT`, `src_ip <module-ip>`, `dest_ip <relay-host>`, `snat_ip <gateway-ip>`).
- **pfSense / OPNsense:** a LAN-interface port forward (source `<module-ip>`,
  destination any:443 → `<relay-host>:8444`) and a hybrid outbound NAT rule for
  the same flow translating to the LAN interface address.
- If the router has no `conntrack`, power-cycle the water heater instead to make
  it re-dial through the new rules.

### Routers that cannot do this

Most mesh systems and ISP gateways (Eero, Google/Nest Wifi, typical ISP
routers) only offer WAN→LAN port forwarding, which does not apply to traffic
that starts on the LAN. They cannot steer the module, and this project does not
work behind them. Do not use a DNS override: the module ignores local DNS.

## Undoing it

Disable or delete both rules first, then break the module's connection
(`conntrack -D -s <module-ip> -p tcp --dport 443`, or power-cycle the heater).
The heater goes straight back to Ayla's cloud. See the README's *Uninstall*.

## Checking it works

1. The gateway's DNAT counter climbs when the module dials (every 1–4 min while
   heating, every ~6 h when idle).
2. The relay's state API shows telemetry:

   ```sh
   curl -s http://127.0.0.1:8124/state | jq '{online, fresh, setpoint, water_top, upstream_errors}'
   ```

   (on the HA host; from Home Assistant, the *Relay health* sensor says the same).
3. The mobile app still shows the water heater online — the relay forwards
   everything upstream, so nothing on the cloud side should change.
