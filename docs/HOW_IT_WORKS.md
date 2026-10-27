# How it works

The water heater's Wi-Fi module talks to Ayla's IoT cloud over HTTPS. That
traffic is steered to a small relay (a Home Assistant add-on), in one of two
ways:

- **DNS:** the add-on's built-in DNS server answers the module's cloud name
  with Home Assistant's address. No router rules are needed, only the router's
  DHCP DNS setting.
- **NAT:** two rules on a router that supports them.

The relay forwards everything to the real cloud unchanged, so the app keeps
working, and decodes the telemetry as it passes. It also flips the
module's built-in Ayla **Local Connect** feature on, with an encryption key the
relay generates itself. After that, the module polls the relay on your LAN every
~10 seconds, and writes ride that poll. No A.O. Smith account or password is
used anywhere. Details: [LAN_MODE.md](LAN_MODE.md). The manufacturer's service
handbook and Use & Care guide, with what each page confirmed:
[REFERENCES.md](REFERENCES.md).

## Security

- The module-facing ports bind all interfaces but trust only a few addresses:
  - **8444 (and 443 with `dns_server`):** loopback, the host's default
    gateway (the NAT rules make the module arrive from there), the module's
    own address, and `trusted_peers`. Anyone else is just forwarded to Ayla,
    with no key, telemetry, writes or rewrites.
  - **8888 (Local Connect):** loopback, the module's own address, and
    `trusted_peers`. The gateway is not trusted here. Anyone else gets 403.
- The state API is unauthenticated and on loopback by default.
- The relay's TLS certificate is self-signed. The module does not validate it.
  The relay *does* verify Ayla's real certificate on the upstream side: the
  chain must end at the Entrust Root G2 shipped in the image (Ayla's Sectigo
  certificate is cross-signed by it) and the name must match. There is no
  fallback to an unverified connection.
- **Firmware updates are held by default.** Ayla delivers them as a command
  through the relay, and the agent does not check a signature on a cloud
  update. A new firmware could also add the certificate check this project
  relies on. Each held update shows up as a repair in Home Assistant; allow
  it there, or ignore the repair to keep it held. Only the allowed image's
  exact URL is fetched, from its real host, with the certificate verified.
- The LAN key is generated locally, never logged, and stored at
  `/data/heaters/<DSN>/lanip.json` (0600). Nothing here uses or stores A.O. Smith credentials.
- The module's own Local Connect registration endpoint is unauthenticated by
  Ayla's design, so treat the heater's LAN segment as trusted.
