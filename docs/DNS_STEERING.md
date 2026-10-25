# Steering with DNS (no NAT-capable router)

[STEERING.md](STEERING.md) steers the water heater with two NAT rules on the
router. Most mesh and ISP routers can't do that. This page does the same job
with DNS instead: a local DNS server answers the module's cloud hostname with
the relay's address, so the module connects to the relay on port 443 by
itself.

**Tested** on the real tank (2026-10-25), with AdGuard Home and the relay on a
Linux host and no NAT in the HTTPS path. From the breaker coming back on to a
verified Local Connect session took about 30 seconds. Details are under
*Measured* below.

## Why it has to be on every boot

The module asks the cloud whether Local Connect is enabled **each time it
boots**, and Ayla's real answer is "no". The relay only switches it on by
answering that question itself. So the steering has to stay in place: one
boot that talks straight to Ayla turns Local Connect off until the next boot
through the relay (measured; see [LAN_MODE.md](LAN_MODE.md)). A Wi-Fi
reconnect doesn't trigger this; only a power cycle does.

## What you need

| | |
|---|---|
| A DNS server you control | the AdGuard Home add-on, Pi-hole, dnsmasq, or your router's own local DNS records if it has them |
| The water heater using it | the module uses whatever DNS server **DHCP** hands it, so point your router's DHCP DNS setting at your DNS server (this usually applies to every device on the network, which is fine: only one hostname is changed) |
| Port 443 free on the relay host | the module only dials 443. On Home Assistant, check that no other add-on owns it (the NGINX SSL proxy add-on, and some device bridges, do). If it's taken, run the relay on another host ([README](../README.md#without-the-add-on-ha-container-core-or-another-host)) |
| The relay and heater on the same subnet | Local Connect calls back to the relay directly. Across VLANs you need the gateway rule from [NETWORK_ISOLATION.md](NETWORK_ISOLATION.md) |
| A DHCP reservation for the heater | recommended: the relay follows a move, but a stable address is easier to firewall |

## 1. The DNS rewrite

Rewrite **exactly one name**, the module's device host, to the relay host's IP:

```
iCOMMNextGenHP-71538015-device.aylanetworks.com  →  <relay-host-ip>
```

That is the name an iCOMM HPS10 heat pump water heater uses. Other iCOMM
models may use a different `…-device.aylanetworks.com` name. To find yours,
turn on your DNS server's query log, power-cycle the heater, and look for the
`-device.aylanetworks.com` lookup from its IP.

**Don't rewrite anything else:**

- `proxy-ingress-usfield.aylanetworks.com` is the relay's own upstream. The
  device name is a CNAME for it, so a wildcard such as `*.aylanetworks.com`
  would point the relay back at itself.
- `ans-field.aylanetworks.com` carries the module's UDP 55055 notifications.
  The relay doesn't handle them, so they must still reach Ayla.

### AdGuard Home

Filters → **DNS rewrites** → Add: domain
`iCOMMNextGenHP-71538015-device.aylanetworks.com`, answer `<relay-host-ip>`.

**Gotcha, measured on v0.107.79:** rewrites only apply while **filtering is
enabled** (Settings → General settings → *Block domains using filters and
hosts files*). With filtering off, AdGuard silently answers with the real
address. If you only want the rewrite, leave filtering on and disable the
blocklists.

Check from any machine (it should answer with the relay host's IP, and
`ans-field` with a public one):

```sh
dig +short @<dns-server-ip> iCOMMNextGenHP-71538015-device.aylanetworks.com
dig +short @<dns-server-ip> ans-field.aylanetworks.com
```

Then set your router's DHCP DNS server to `<dns-server-ip>`. If the router
can't set DHCP DNS at all, turn off its DHCP server and use AdGuard's.

## 2. The relay

Add-on options (or the matching environment variables when the relay runs as
a service):

| Option | Value | Why |
|---|---|---|
| `listen_port` | `443` | the module connects to the rewritten name on 443; no NAT moves it to 8444 |
| `module_ip` | leave empty (0.5.3+) | with DNS the module arrives from its own address. The relay trusts it once Ayla accepts that peer's config fetch, which needs the device's own credentials, and pins it to that one DSN (or `device_dsn`, if set). Relays before 0.5.3 need `module_ip` set here |
| `relay_ip` | the relay host's IP | the address the module calls back for Local Connect |

## 3. Power-cycle the heater once

Breaker off for about 30 seconds, then on. Within a minute the add-on log shows
the config rewrite, and then Local Connect:

```
DSN rewrite: flipped lan-enabled, local-access-enabled in config reply
LAN mode: registered with the module
LAN mode: key exchange accepted; session keys derived
```

## Risks to know

- **The DNS server is now in the path at every boot.** If it's down, or
  answers with the real address, when the heater boots, the module reaches
  Ayla directly and Local Connect stays off until a later boot goes through
  the relay. The heater keeps heating, and the app keeps working.
- **Nothing stops the module reaching the internet directly.** It only goes
  where DNS sends it. A router firewall rule that only lets the heater reach
  the relay host and DNS is stronger; [NETWORK_ISOLATION.md](NETWORK_ISOLATION.md)
  shows one.
- **Same black hole as NAT:** while the rewrite is in place, the relay is the
  heater's only route to the cloud. Remove the rewrite before uninstalling the
  relay.

## Measured

Test host: Debian with the relay (`listen_port 443`, the same LAN key as the
Home Assistant add-on) and AdGuard Home v0.107.79. The heater was on its
isolated VLAN, with DHCP DNS pointed at the test host and the router's 443 NAT
turned off. The VLAN still needs the gateway rule for the 8888 callback, which
a flat network wouldn't. Breaker cycled:

| Time | Event |
|---|---|
| 12:53:13 | the heater looked up `icommnextgenhp-71538015-device.aylanetworks.com` on AdGuard: **rewritten** to the relay host |
| 12:53:19 | `GET /dsns/<DSN>.xml` arrived at the relay on 443, **from the heater's own IP** |
| 12:53:25 | the relay rewrote the config reply (`lan-enabled`, `local-access-enabled`); `ans-field` resolved normally |
| 12:53:26–29 | boot datapoints forwarded to Ayla, upstream TLS verified |
| 12:53:34 | Local Connect: registration `202`, key exchange accepted, telemetry flowing |

No firmware was offered; the hold stayed in place.
