# Isolating the water heater on its own VLAN

Optional, and recommended. The module's firmware accepts any TLS certificate
from whoever answers as Ayla's cloud, and cloud firmware updates carry no
signature it checks. This project depends on the first flaw. The VLAN makes
sure neither can be used against the rest of your network:

- **The heater reaches only the relay.** It gets HTTPS (steered) and Local
  Connect to the Home Assistant host, plus DNS and DHCP from the gateway.
  Nothing else on your LAN, and not the gateway's admin pages.
- **The relay is the only way out.** The heater has no direct internet
  except Ayla's notification channel (UDP 55055). With firmware updates held
  (the default since 0.5.0), the relay decides what crosses.
- **Your LAN reaches the heater only where needed**: the relay's Local
  Connect calls (TCP 80), plus ping.

Tested on a UDM Pro (UniFi OS 5.1, Network 10.6.109), zone-based firewall,
2026-10-24. The heater kept its SSID: UniFi's per-client *Virtual Network
Override* moved it.

## The one non-obvious constraint

Ayla's agent refuses a Local Connect registration whose callback address is
not on **its own subnet** (`libada/lan.c`, `net_ipv4_is_local`, answered with
403). With the heater on its own VLAN, the Home Assistant host is not on that
subnet. The fix is to give the module a callback address that is: **the
gateway's address on the heater's VLAN**, forwarded to the relay. Set the
add-on's `relay_ip` to it.

## Layout

| | Value (example) |
|---|---|
| Heater VLAN | `IoT Isolated`, VLAN 20, `10.0.20.1/24`, DHCP, mDNS off |
| Heater | fixed IP `10.0.20.187` on that network |
| Home Assistant (relay) | `10.0.1.113` on the main LAN |
| Main LAN gateway | `10.0.1.1` |
| Add-on options | `module_ip: 10.0.20.187`, `relay_ip: 10.0.20.1` |

## Firewall (zone-based)

Put the VLAN in its own zone (UniFi puts a new network in *Internal* by
default, which reaches everything). A new zone's defaults block it from the
LAN but **allow all internet and all gateway access**, so add these. Within
each zone pair, create the allow before the block: the controller orders
them by creation.

| Zone pair | Action | Match |
|---|---|---|
| heater → Internal | allow | TCP to `10.0.1.113` ports `8444,8888` (after the DNAT below) |
| Internal → heater | allow | TCP from `10.0.1.113` to `10.0.20.187:80` (Local Connect) |
| Internal → heater | allow | ICMP echo to `10.0.20.187` (ping) |
| heater → Internal | allow | ICMP echo *reply*, established only (see below) |
| heater → External | allow | UDP port `55055` (Ayla's notifications, so app commands arrive) |
| heater → External | block | everything else |
| heater → Gateway | allow | TCP/UDP ports `53,67` (DNS, DHCP) |
| heater → Gateway | block | everything else |

Use "allow return traffic" on the first three. For ping, that isn't enough:
the return rule UniFi generates copies the ICMP type, so it allows echo
*request* (type 8) back and drops the replies (type 0). Hence the explicit
echo-reply rule. The controller refuses it for
the External and Gateway pairs, which allow return traffic already.

Blocking UDP 55055 as well would leave the heater no direct internet at all,
at a cost: Ayla could no longer ring the module, so app commands and firmware
offers would stop arriving. Telemetry still reaches the app through the
relay. The module retried harder during an earlier UDP blackout (research
phase 2), so measure before relying on it.

## NAT

| Type | Interface | Match | Translate to | Why |
|---|---|---|---|---|
| DNAT | in: heater VLAN | from `10.0.20.187`, TCP any:443 | `10.0.1.113:8444` | HTTPS to the relay |
| SNAT | out: main LAN | `10.0.20.187` → `10.0.1.113:8444` | `10.0.1.1` | the relay trusts the module on 8444 as the gateway |
| DNAT | in: heater VLAN | `10.0.20.187` → `10.0.20.1:8888` | `10.0.1.113:8888` | the Local Connect callback (see above) |
| SNAT | out: heater VLAN | `10.0.1.113` → `10.0.20.187:80` | `10.0.20.1` | the module sees a caller on its own subnet |

**Don't** SNAT the heater's traffic to 8888. The relay trusts its LAN port
only to the module's own address, and across VLANs the replies route back
without a hairpin rule. (Done anyway, the module's key exchange gets 403.)

The DNAT/SNAT JSON in [STEERING.md](STEERING.md) works for these too: change
`in_interface`/`out_interface` to the network's `_id`, and give each rule its
own `rule_index`.

## Moving the heater

1. Create the network, zone, policies and NAT rules. None of them matches
   anything until the heater has its new address.
2. Set the add-on's `module_ip` and `relay_ip`.
3. Give the heater the override and its fixed IP (client → Settings →
   Virtual Network Override; in the API, `virtual_network_override_enabled`,
   `virtual_network_override_id`, `network_id`, `use_fixedip`, `fixed_ip` on
   `rest/user/<id>`), then reconnect it (`kick-sta`).
4. Restart the add-on.

Measured: the heater was back with telemetry within about 30 s, and Local
Connect was verified within a minute. **Set `module_ip`**: right after the
move, Ayla's registration reply still listed the old address, and relays
before 0.5.1 believed it over the setting.

Rollback: turn the override off, set the fixed IP back, clear `module_ip` and
`relay_ip`, and restart the add-on. The old NAT rules for the main LAN can
stay in place until you're sure.

## Checking it

```sh
# On the gateway: per-rule packet counters climb as the heater dials
iptables -t nat -L UBIOS_PREROUTING_USER_HOOK -v -n | grep DNAT
# On the HA host: telemetry fresh, Local Connect verified
curl -s http://127.0.0.1:8124/state | jq '{fresh, lan: .lan | {session, session_verified}}'
```

A ping from the gateway itself goes unanswered: the "heater → Gateway:
block" rule also drops the echo replies. Ping from a LAN host instead.
