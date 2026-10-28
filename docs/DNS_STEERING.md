# Steering with DNS (no NAT-capable router)

[STEERING.md](STEERING.md) steers the water heater with two NAT rules on the
router. Most mesh and ISP routers can't do that. This page does the same job
with DNS instead: a local DNS server answers the module's cloud hostname with
the relay's address, so the module connects to the relay on port 443 by
itself.

There are two ways to run the DNS side:

- **Built in (0.5.5+):** turn on the add-on's `dns_server` option. The relay
  serves DNS on the Home Assistant host. Most people want this.
- **Your own resolver:** if you already run AdGuard Home, Pi-hole or similar,
  add one rewrite there (below) and leave `dns_server` off.

## Built in: `dns_server`

**Tested** on the real tank: this has been the author's live setup since
2026-10-25. Local Connect was verified 23 seconds after the heater's first
DNS lookup, with no `module_ip` set (see *Measured: built in* below). A
fresh install was also dry-run on 0.7.6: the add-on image with an empty
`/data` and only `dns_server` turned on.

1. In the add-on's configuration, turn on **`dns_server`** and restart the
   add-on. The relay then:
   - answers DNS on port 53 of the HA host;
   - resolves the A.O. Smith module's cloud name
     (`<model>-71538015-device.aylanetworks.com`) to the HA host, with no IPv6
     answer for it;
   - forwards every other query unchanged to `dns_upstream`, by default your
     router (the host's default gateway);
   - also listens on 443 for the module.
2. In your router, set the **DHCP DNS server** to your Home Assistant host's IP
   address. Leave the secondary DNS empty: if the heater picks the secondary
   when it boots, it reaches Ayla directly and local control stays off until
   the next boot.
3. Before touching the heater, check from any computer on the network. The
   heater's name should come back as the Home Assistant host's IP, its IPv6
   lookup should be empty, and other names should resolve normally:

   ```console
   $ dig +short @<ha-ip> iCOMMNextGenHP-71538015-device.aylanetworks.com
   <ha-ip>
   $ dig +short @<ha-ip> AAAA iCOMMNextGenHP-71538015-device.aylanetworks.com
   $ dig +short @<ha-ip> ans-field.aylanetworks.com
   34.45.30.160
   ```

   The add-on log shows the setup and each rewrite:

   ```
   DNS: serving on :53; rewriting [a-z0-9-]+-71538015-device\.aylanetworks\.com, forwarding the rest to <gateway-ip>
   DNS: steering icommnextgenhp-71538015-device.aylanetworks.com for <your-computer> to <ha-ip>
   ```

   If `ans-field` or other names don't resolve, your gateway doesn't answer
   DNS (some routers hand out a separate resolver instead). Set
   **`dns_upstream`** to a resolver that works, such as your ISP's or
   `1.1.1.1`, and restart the add-on. The dry run hit exactly this: forwarding
   to a gateway with no DNS server fails for every name except the heater's.
   Don't skip this check. Once the router hands out Home Assistant as DNS,
   every device on the network depends on that forwarding.
4. Power-cycle the water heater once (breaker off for about 30 seconds). Within
   a minute the add-on log shows:

   ```
   DNS: steering icommnextgenhp-71538015-device.aylanetworks.com for <heater-ip> to <ha-ip>
   LAN mode: module <DSN> is at <heater-ip> (its own address: steered by DNS, not NAT)
   DSN rewrite: flipped lan-enabled, local-access-enabled in config reply
   LAN mode: module fetched lan.json; answered with our key
   LAN mode: registered with the module
   ```

   An earlier line, `… waiting for Ayla to confirm the module`, is normal.
   The heater's first request at each boot comes before Ayla has checked its
   credentials. The iCOMM Local integration's *Relay health* then turns
   healthy once the heater has dialled the cloud a few times.

   **With the built-in server, you're done here.** The *Your own resolver*
   section below is only for people who leave `dns_server` off.

**Heater on its own VLAN?** Local Connect only calls back to an address on the
heater's own subnet. Set **`relay_ip`** to the gateway's address on the
heater's VLAN. The gateway then needs a DNAT rule for the callback, an SNAT
rule for the relay's calls to the module, and firewall policies between the
VLANs. See [NETWORK_ISOLATION.md](NETWORK_ISOLATION.md), which is the author's
setup.
On a flat network, leave `relay_ip` empty.

Home Assistant raises a repair if port 53 or 443 is already taken on the host,
for example by an AdGuard, Pi-hole or NGINX SSL add-on. In that case use the
resolver you already have, as below.

**The trade-off:** with the router handing out Home Assistant as DNS, every
device on the network resolves through it. While Home Assistant is down, name
lookups on the network fail. That's the price of not needing a router that can
do NAT steering. To limit it to the water heater, put the heater on its own
network or VLAN and set only that network's DHCP DNS to Home Assistant, if your
router can.

## Why it has to be on every boot

The module asks the cloud whether Local Connect is enabled **each time it
boots**, and Ayla's real answer is "no". The relay only switches it on by
answering that question itself. So the steering has to stay in place: one
boot that talks straight to Ayla turns Local Connect off until the next boot
through the relay (measured; see [LAN_MODE.md](LAN_MODE.md)). A Wi-Fi
reconnect doesn't trigger this; only a power cycle does.

## Your own resolver

This section is for an existing AdGuard Home, Pi-hole or similar, with
`dns_server` off. If you use the built-in server, skip it.

**Tested** on the real tank (2026-10-25), with AdGuard Home and the relay on a
Linux host and no NAT in the HTTPS path. From the breaker coming back on to a
verified Local Connect session took about 30 seconds. Details are under
*Measured: your own resolver* below.

### What you need

| | |
|---|---|
| A DNS server you control | the AdGuard Home add-on, Pi-hole, dnsmasq, or your router's own local DNS records if it has them |
| The water heater using it | the module uses whatever DNS server **DHCP** hands it, so point your router's DHCP DNS setting at your DNS server (this usually applies to every device on the network, which is fine: only one hostname is changed) |
| Port 443 free on the relay host | the module only dials 443. On Home Assistant, check that no other add-on owns it (the NGINX SSL proxy add-on, and some device bridges, do). If it's taken, run the relay on another host ([CONFIGURATION.md](CONFIGURATION.md#without-the-add-on-ha-container-core-or-another-host)) |
| The relay and heater on the same subnet | Local Connect calls back to the relay directly. Across VLANs the gateway needs a DNAT and an SNAT rule for Local Connect, `relay_ip`, and firewall policies; see [NETWORK_ISOLATION.md](NETWORK_ISOLATION.md) |
| A DHCP reservation for the heater | recommended: the relay follows a move, but a stable address is easier to firewall |

### 1. The DNS rewrite

Rewrite **exactly one name**, the module's device host, to the relay host's IP:

```
iCOMMNextGenHP-71538015-device.aylanetworks.com  →  <relay-host-ip>
```

That is the name an iCOMM HPS10 heat pump water heater uses. Other iCOMM
models may use a different `…-device.aylanetworks.com` name. To find yours,
turn on your DNS server's query log, power-cycle the heater, and look for the
`-device.aylanetworks.com` lookup from its IP. The built-in `dns_server`
rewrites any `<model>-71538015-device.aylanetworks.com` name. If your model's
name doesn't match that, set the `DNS_REWRITE` environment variable to a
regular expression that does (see [CONFIGURATION.md](CONFIGURATION.md)).

**Don't rewrite anything else:**

- `proxy-ingress-usfield.aylanetworks.com` is the relay's own upstream. The
  device name is a CNAME for it, so a wildcard such as `*.aylanetworks.com`
  would point the relay back at itself.
- `ans-field.aylanetworks.com` carries the module's UDP 55055 notifications.
  The relay doesn't handle them, so they must still reach Ayla.

#### AdGuard Home

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

### 2. The relay

Add-on options (or the matching environment variables when the relay runs as
a service):

| Option | Value | Why |
|---|---|---|
| `listen_port` | `443` | the module connects to the rewritten name on 443; no NAT moves it to 8444 |
| `module_ip` | leave empty (0.5.3+) | with DNS the module arrives from its own address. The relay trusts it once Ayla accepts that peer's config fetch, which needs the device's own credentials, and pins it to that one DSN (or `device_dsn`, if set). Relays before 0.5.3 need `module_ip` set here |
| `relay_ip` | leave empty | the address the module calls back for Local Connect. The relay detects it: its own address on the route to the module. Set it only if the heater is on another VLAN, to the gateway's address on that VLAN ([NETWORK_ISOLATION.md](NETWORK_ISOLATION.md)) |

### 3. Power-cycle the heater once

Breaker off for about 30 seconds, then on. Within a minute the add-on log shows
the config rewrite, and then Local Connect:

```
DSN rewrite: flipped lan-enabled, local-access-enabled in config reply
LAN mode: module fetched lan.json; answered with our key
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

## Measured: built in

Live on Home Assistant OS (relay 0.5.5), 2026-10-25. The heater was on its own
VLAN, with that VLAN's DHCP DNS pointed at Home Assistant. `module_ip` was
cleared, the add-on restarted, and then the breaker cycled:

| Time | Event |
|---|---|
| 16:13:49 | the heater looked up its name on Home Assistant: **rewritten** |
| 16:14:01 | its first `GET /dsns/<DSN>.xml` arrived on 443 from its own IP; Ayla answered 401, so the relay doesn't trust it yet |
| 16:14:07 | the authenticated retry got 200; the relay pinned the module to that address, then rewrote the config reply |
| 16:14:12 | Local Connect: registered, key exchange OK, telemetry flowing |

## Measured: your own resolver

Test host: Debian with the relay (`listen_port 443`, the same LAN key as the
Home Assistant add-on) and AdGuard Home v0.107.79. The heater was on its
isolated VLAN, with DHCP DNS pointed at the test host and the router's 443 NAT
turned off. The VLAN still needed the gateway's Local Connect DNAT and SNAT
rules (the 8888 callback, and the relay's calls to the module), which a flat
network wouldn't. Breaker cycled:

| Time | Event |
|---|---|
| 12:53:13 | the heater looked up `icommnextgenhp-71538015-device.aylanetworks.com` on AdGuard: **rewritten** to the relay host |
| 12:53:19 | `GET /dsns/<DSN>.xml` arrived at the relay on 443, **from the heater's own IP** |
| 12:53:25 | the relay rewrote the config reply (`lan-enabled`, `local-access-enabled`); `ans-field` resolved normally |
| 12:53:26–29 | boot datapoints forwarded to Ayla, upstream TLS verified |
| 12:53:34 | Local Connect: registration `202`, key exchange accepted, telemetry flowing |

No firmware was offered; the hold stayed in place.
