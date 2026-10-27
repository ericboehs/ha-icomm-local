# Configuration

Add-on options and the relay's environment variables. The defaults suit almost everyone.

## Options

Add-on options, or environment variables when running the relay directly:

| Option / env | Default | Meaning |
|---|---|---|
| `listen_port` / `LISTEN_PORT` | 8444 | port the NAT rule sends the module's HTTPS to. Set it to 443 with your own DNS resolver; with `dns_server` the relay also listens on 443 by itself |
| `state_port` / `STATE_PORT` | 8124 | state and write API for the integration |
| `STATE_HOST` | 127.0.0.1 | state API bind address |
| `upstream` / `UPSTREAM` | `proxy-ingress-usfield.aylanetworks.com` | the real Ayla endpoint every module request is forwarded to (US field) |
| `lan_mode` / `LAN_MODE` | on | Local Connect; off means writes wait for a cloud poll |
| `lan_port` / `LAN_PORT` | 8888 | Local Connect callback port |
| `module_ip` / `MODULE_IP` | learned | heater IP. Normally learned: from its registration with NAT steering, or pinned when Ayla accepts its config fetch with DNS steering. Primary heater only; leave empty with several |
| `relay_ip` / `RELAY_IP` | detected | the address the module should call back on |
| `trusted_peers` / `TRUSTED_PEERS` | — | extra addresses allowed on the module-facing ports; `*` disables the check |
| `device_dsn` / `DEVICE_DSN` | — | shown in `/state`; with DNS steering, only this DSN's module is pinned (otherwise the first one seen). Primary heater only |
| `CONTENTS_STALE_AFTER` | 5400 | seconds of byte-identical reports before `/state` says `contents_stale` |
| `CLOUD_IDLE_AFTER` | 1800 | seconds with no cloud request from the module, while Local Connect still delivers changing reports, before health turns `degraded` (`cloud_idle`) |
| `dns_server` / `DNS_SERVER` | off | serve DNS on the host's port 53: the heater's cloud name resolves to this host (the relay also listens on 443), everything else is forwarded. See [DNS_STEERING.md](DNS_STEERING.md) |
| `dns_upstream` / `DNS_UPSTREAM` | the gateway | where `dns_server` forwards other queries (`ip` or `ip:port`) |
| `DNS_PORT` | 53 | the port `dns_server` listens on |
| `DNS_HOST` | all interfaces | the address `dns_server` listens on (UDP and TCP) |
| `DNS_ANSWER` | detected | the address `dns_server` answers for the heater's cloud name. By default, the host's own address on the route to the asking client |
| `DNS_REWRITE` | `[a-z0-9-]+-71538015-device\.aylanetworks\.com` | the names `dns_server` rewrites, as a case-insensitive regular expression matched against the whole name. Change it if your model uses another hostname (see [DNS_STEERING.md](DNS_STEERING.md#1-the-dns-rewrite)) |
| `max_heaters` / `MAX_HEATERS` | 4 | how many water heaters the relay serves (#18). Each new one is added when Ayla accepts its config fetch; the relay's API has `/heaters`, `/heaters/<dsn>/...`, and `DELETE /heaters/<dsn>?confirm=<dsn>` to forget one. Each heater is its own device in HA |
| `offline_fallback` / `OFFLINE_FALLBACK` | on | if Ayla is unreachable (or answers 5xx) when the heater boots, answer its config fetch from the last real reply, so local control survives a power cut during an outage. See [LAN_MODE.md](LAN_MODE.md), *When Ayla is down* |
| `reporting_mask_renew` / `REPORTING_MASK_RENEW` | on | re-send the app's periodic-reporting mask over LAN when the contents have been frozen for `REPORTING_MASK_RENEW_AFTER` (1800) seconds, or `CONTENTS_STALE_HEATING_AFTER` (1200) while the last report said heating, at most once per that interval |
| `experimental_freeze_reconnect` / `FREEZE_RECONNECT` | off | experimental: when 2 renewals haven't unfrozen the data, reconnect the LAN session once per freeze (a new registration and key exchange), then keep renewing. Seen to help once; relay reconnects haven't been seen to cause freezes (0 of 17) |
| `firmware_updates` / `FIRMWARE_UPDATES` | hold | `hold`: firmware updates wait for you to allow each one (a Home Assistant repair); `allow`: pass them through |
| `QUEUE_MAX_AGE_S` | 86400 | queued writes older than this are dropped |
| `LAN_REFRESH_INTERVAL` | 300 | seconds between keep-alive telemetry reads; 0 disables |
| `LANIP_PUSH` | off | force the LAN-key push (normally automatic) |
| `LANIP_FILE` | `$DATA_DIR/heaters/<DSN>/lanip.json` | where the primary heater's LAN key is kept. For development |
| `LANIP_KEY_ID` | 1 | the key ID used when a new LAN key is generated. A saved key keeps its own ID |
| `LANIP_KEY` | generated | development only: use this LAN key instead of the saved one. Nothing is written to disk |
| `DSN_LAN_REWRITE` | off | flip `lan-enabled` and `local-access-enabled` in the config reply even with `lan_mode` off. An experiment switch; with `lan_mode` on it changes nothing |
| `DEBUG_REQUESTS` | off | log every module request path |
| `CERT_DIR`, `RELAY_CERT`, `RELAY_KEY`, `RELAY_DHPARAMS` | add-on: `/data/certs/...`; run directly: `./certs/...` | TLS material locations. `run.sh` (the add-on) sets `CERT_DIR` to `$DATA_DIR/certs`; the relay and `gen-cert.sh` default to `certs` in the working directory |

## Without the add-on (HA Container, Core, or another host)

```bash
cd relay
./gen-cert.sh                                   # self-signed leaf + DH params
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
QUEUE_FILE=$PWD/state/pending.json STATE_HOST=0.0.0.0 .venv/bin/python icomm_hub.py
```

Python 3.10+, on the same subnet as the heater. On a host without
`/proc/net/route` (not Linux), the gateway cannot be detected: set
`TRUSTED_PEERS=<gateway-ip>`. Use `STATE_HOST=0.0.0.0` only if
Home Assistant runs on another machine: the state API is unauthenticated, so
keep it on a trusted LAN. A systemd unit is in `relay/icomm-relay.service`.
