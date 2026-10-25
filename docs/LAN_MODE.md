# LAN mode: the local doorbell

How the relay gives an iCOMM module local writes and local telemetry without an
A.O. Smith account, and without touching the cloud path. Proven end to end on
the real hardware (research repo, captures INDEX, phases 28–29): setpoint writes
delivered, acked, and confirmed in decrypted LAN telemetry in 6–7 seconds.

## The moving parts

| Piece | Where it runs | What it does |
|---|---|---|
| DSN rewrite | relay, on the module's TLS connection | flips `lan-enabled` / `local-access-enabled` in Ayla's registration reply; the rest is byte-identical |
| `lan.json` answer | relay, same connection | supplies the shared key the agent asks for once armed |
| LAN server | relay, plain HTTP on `lan_port` (default 8888) | key exchange, encrypted command replies, encrypted telemetry |
| Registration heartbeat | relay → module `PUT /local_reg.json` every ~10 s | keeps the session alive; the module polls us per heartbeat |

The module's cloud conversation continues unchanged in parallel. The relay
still forwards everything upstream verbatim, and a cloud poll can still carry a
queued write if the LAN session is down.

## Why the key can be ours

Ayla's device agent (`libada/client.c`) fetches `GET /devices/<key>/lan.json`
from *its configured cloud* when `lan-enabled` is true **and it has no stored
key**. Behind the relay, that cloud is us. It accepts the key in the answer and
persists it; there is no OEM signature on it. The agent only asks when it has
no key, and nothing in the protocol wipes a key over the network — which is why
the relay persists its own key (`/data/heaters/<DSN>/lanip.json`, mode 0600;
`/data/lanip.json` before 0.6.0, still kept as a copy) and why you should not
delete that file after the module has learned it.

## The protocol (as implemented)

- **Registration:** `POST`/`PUT http://<module>/local_reg.json` with
  `{"local_reg":{"ip":<relay>,"port":<lan_port>,"uri":"/local_lan","notify":1}}`.
  `202` = accepted. `404` = LAN not armed (the module has not taken the
  rewritten registration reply, or LAN mode is factory-disabled and the rewrite
  has not happened yet). `412` = armed but no key yet. `403` = the advertised
  address is not on the module's subnet (set `relay_ip`).
- **Key exchange:** the module POSTs `/local_lan/key_exchange.json` with
  `{"key_exchange":{"ver":1,"proto":1,"random_1":…,"time_1":…,"key_id":…}}`.
  We answer `{"random_2":…,"time_2":…}` and derive session keys:
  `K(msg) = HMAC-SHA256(secret, HMAC-SHA256(secret, msg) + msg)`;
  signing key `K(root+"0")`, AES key `K(root+"1")`, IV `K(root+"2")[:16]`;
  module→relay root `random_2|random_1|time_2|time_1`, relay→module root
  `random_1|random_2|time_1|time_2`.
- **Framing:** `{"enc": base64(AES-CBC(zero-padded JSON))),
  "sign": base64(HMAC-SHA256(sign_key, JSON))}`.
- **Commands:** the module GETs `/local_lan/commands.json`; the reply is an
  encrypted `{"seq_no":N,"data":{…}}`. A queued write goes out as
  `data.properties[0].property = {base_type:"string", name:"setBlockParameter",
  value:"BD 40 …", id:<8 chars>}` — the same golden frames the cloud path uses.
  A delivered write is not re-sent every poll: if telemetry has not confirmed
  it after 60 s, it is retried, up to three deliveries in total across the LAN
  and cloud paths (see *Write status*).
- **Telemetry:** the module POSTs encrypted updates to
  `/local_lan/property/datapoint.json`. `superBlockDyn1` is decoded exactly
  like the cloud POST; `ack_status` is logged as a delivery ack.
- **Active refresh:** after a successful write ack, and whenever a LAN session
  starts, the next command poll carries a read-only reverse-REST request:
  `GET property.json?name=superBlockDyn1`. Ayla's handler calls
  `ada_prop_mgr_get()`, obtaining the current value from the tank controller;
  the encrypted response returns to the normal datapoint endpoint. Measured
  response time: **0.3 s**. This avoids waiting for the appliance's potentially
  hours-long spontaneous reporting interval.
- **Wake on write:** queueing a write (or receiving its ack) sends an
  immediate out-of-band `PUT local_reg.json` with `notify: 1`. libada's
  `client_lan_refresh` sets the session's pending flag and wakes the agent,
  so the module fetches commands at once instead of on the next ~10 s
  heartbeat. Measured: delivery about 0.1 s after the HA service call, and
  confirmation in 1–2 s when the read-back is answered.
- **The read-back after an ack is sometimes ignored, and retrying does not
  help.** In about half of live writes, the module did not answer the
  read-back sent right after the ack. Confirmation then came from its own
  report about 4–5 s after the write. 0.4.4 retried the read at +1.5 s and
  +3.0 s. In every such case (3 of 6 writes, 6 retries) the retries were
  ignored too, and confirmation still arrived at 4–5 s. The module appears
  busy for a few seconds after applying some writes (it is also posting the
  change to the cloud). 0.4.6 removed the retry. Do not reintroduce it
  without new evidence.
- **Stalled polling:** once, right after an app (cloud) write, the module kept
  answering `PUT local_reg.json` with 202 but stopped polling for 5+
  minutes. A fresh `POST` registration (new session, key exchange) resumed
  it within a second. The relay now re-registers after `LAN_STALL_AFTER`
  (default 60 s) of accepted heartbeats without a poll.
- **Keep-alive reads:** while a LAN session is up, the same read is issued
  whenever telemetry is more than 5 minutes old (`LAN_REFRESH_INTERVAL`, `0`
  disables). Without it an idle tank goes 10+ minutes without reporting, the
  relay calls the data stale, and Home Assistant marks the entity unavailable
  and **silently drops service calls** (measured twice on the live tank).

  **Confirmation is still the tank's own telemetry, never the ack.** An ack can
  be followed by a fresh report that still contains the old setpoint: that
  means the appliance did not apply the write, not that confirmation should be
  inferred from delivery. Measured: over about an hour, four writes (LAN and
  cloud, 135/138/140 °F) were acked but not applied. Afterwards, a full sweep
  of every integer from 95–150 °F applied, all values within about 5 s. The
  failures were a transient appliance state, not bad values, and the relay
  keeps no block-list. If it happens again, the write shows `not_applied`.

## Write status

Every pending write carries `status`, and the Home Assistant *Pending write*
sensor shows it (`setpoint 140.0: acknowledged`):

| Status | Evidence |
|---|---|
| `queued` | accepted by the relay, not yet sent |
| `delivered` | sent in a LAN (`delivery_path: lan`) or cloud command reply |
| `acknowledged` | the module acked *this* delivery (LAN ack id matches the property id we sent) |
| `confirmed` | the tank's own telemetry reports the value; the write leaves `pending` and lands in `last_write` |
| `not_applied` | acked on all three attempts, yet fresh telemetry still reports the old value (`observed_after_ack`) |
| `rejected` | the module answered the property with an error `ack_status`; not retried |

`reports_after_ack` / `observed_after_ack` show what the tank said after the
ack, so "acked but ignored" is visible instead of looking stuck. Cloud
deliveries have no per-command ack, so they go straight from `delivered` to
`confirmed`.

A write gets **three deliveries in total, across both paths**, at least 60 s
apart. LAN copies are only sent into a session that has produced one
authenticated update. A module holding a foreign key completes key exchanges
but can never decrypt our replies, so copies sent into it would only use up the
cloud fallback's attempts. Fifteen minutes after its last delivery, an
exhausted write leaves `pending` and is recorded in `last_write` with outcome
`not_applied`, `rejected` or `unconfirmed` (cloud only, no ack). A write still
queued after `QUEUE_MAX_AGE_S` is retired as `expired`. If the tank later
reports the retired value anyway, `last_write` is upgraded to `confirmed`, with
`earlier_outcome` recording the first verdict.

## What `/state` tells you

`lan` in the state API: `registered`, `session`, `module_ip`, `port`, `key_id`,
`key_served` (we have answered `lan.json` since startup), `updates`,
`writes_delivered`, `last_heartbeat_age`, `last_update_age`,
`last_registration_result`, `registration_failures`, `needs_key_push`,
`refresh_pending`, `telemetry_requests`.

## Arming and recovery

- **First arming needs one re-registration.** Power-cycle the module once
  (breaker, ~10 s) after enabling LAN mode. The tank's own thermostat is
  unaffected. After that the module persists both the enable flag and the key,
  so subsequent reboots need nothing **as long as the relay stays in the
  path**. Measured (research phase 39): one boot talking to the real Ayla and
  registration returns 404, because Ayla's reply turns the flag back off. A
  Wi-Fi reconnect (`kick-sta`) does not re-register; only a power cycle does.
- **If `registered` stays false:** check `last_registration_result`. 404 means
  the rewritten config was never fetched — re-register again. 412 means the
  module is armed but has no key — it should then fetch `lan.json`; watch for
  `key_served: true`.
- **If the module holds a key this relay did not issue** (an earlier
  experiment, or a deleted `lanip.json`): it will not ask for a
  replacement — `lan.json` is only fetched when keyless. Measured on the real
  tank: Wi-Fi re-provisioning does **not** clear the stored key, and neither
  does the Wi-Fi radio factory reset (handbook p. 69, 10-second hold).
  The symptom is registration succeeding (202, not 412) while key exchanges
  repeat every heartbeat and no telemetry arrives.

  **The relay repairs this by itself.** It recognises the signature (three
  key exchanges with no update between them; `/state` → `lan.needs_key_push`)
  and answers with Ayla's own reverse-REST command, `PUT lanip.json`, inside
  the next `commands.xml` reply. That command sets *and saves* a new key — the
  cloud-pushed rotation mechanism. The module acks it
  (`PUT …/lanip.json?cmd_id=N&status=200`, answered locally, never forwarded to
  Ayla), and the next heartbeat's key exchange holds. Measured on the real
  tank: acked 50 ms after delivery, LAN telemetry flowing 7 s later.

  The push rides a *cloud* poll, which an idle module makes rarely; a
  power-cycle forces one immediately (the boot sequence always polls).
  `LANIP_PUSH=1` forces the push regardless, for testing.
- **After a relay restart:** the module's address is persisted next to the
  key, so LAN mode resumes on the next heartbeat instead of waiting for a
  re-registration that can be hours away.
- **Opting out:** `lan_mode: false` (or `LAN_MODE=0`) disables the server and
  the DSN rewrite; writes then wait for a cloud poll, and nothing about the
  module's configuration changes back. To disarm the module itself you would
  have to make it re-register against the *real* Ayla without the relay in the
  path (remove the NAT steering first).

## When Ayla is down

| Situation | What happens |
|---|---|
| Ayla down, heater keeps running | Local control unaffected. Telemetry and write acks are answered 200 locally, polls get an empty `commands.xml` (which can still carry a queued write) |
| Ayla down across a heater reboot | `offline_fallback` (default on) answers the boot config fetch: a 401 with `x-ayla-server-time`, then 200 with the saved reply (fresh `<unix-time>`, LAN switches on) and a made-up `x-ayla-auth-key`. LAN mode arms as usual |
| Ayla back | The made-up key gets Ayla's 401 without a server time; libada clears it and signs in again with its device key |

Conditions: the request comes from the trusted module, for the DSN the relay
knows, and `dsn-original.xml` (saved at the last real boot) exists. A new
install still needs Ayla once: pinning a DSN-steered module needs Ayla's real
200. `/state.offline` counts the challenges and replies served.

## Security notes

- The shared key is generated locally and never sent to A.O. Smith. It is not
  logged. It is stored unencrypted at `/data/heaters/<DSN>/lanip.json` with mode 0600 —
  protect `/data` accordingly.
- The LAN endpoints speak only the encrypted protocol, and since 0.3.8 they
  answer only the module's address (plus loopback and `TRUSTED_PEERS`). The
  impersonated-cloud port answers only the module or the gateway, so
  `lan.json` no longer hands the key to any LAN host. A message that fails its
  signature no longer advances the CBC chain, so junk cannot desync a session. The registration endpoint on the
  *module* is unauthenticated by design (that is Ayla's protocol, not our
  choice), so treat the LAN segment as trusted, as with the rest of this
  project.
- The module's cloud traffic is still forwarded to A.O. Smith. LAN mode removes
  the cloud from the *control* path, not from the device.
