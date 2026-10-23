# LAN mode: the local doorbell

How the relay gives an iCOMM module local writes and local telemetry without an
A.O. Smith account, and without touching the cloud path. Proven end to end on
the real hardware (research repo, captures INDEX, phase 28): a setpoint write
delivered, acked, and confirmed in decrypted LAN telemetry in about six seconds.

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
the relay persists its own key (`/data/lanip.json`, mode 0600) and why you
should not delete that file after the module has learned it.

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
  A delivered write is not re-sent every poll: it is retried only if telemetry
  has not confirmed it after 60 s.
- **Telemetry:** the module POSTs encrypted updates to
  `/local_lan/property/datapoint.json`. `superBlockDyn1` is decoded exactly
  like the cloud POST; `ack_status` is logged as a delivery ack.
  **Confirmation is still the tank's own telemetry, never the ack.**

## What `/state` tells you

`lan` in the state API: `registered`, `session`, `module_ip`, `port`, `key_id`,
`key_served` (we have answered `lan.json` since startup), `updates`,
`writes_delivered`, `last_heartbeat_age`, `last_update_age`,
`last_registration_result`, `registration_failures`.

## Arming and recovery

- **First arming needs one re-registration.** Power-cycle the module once
  (breaker, ~10 s) after enabling LAN mode. The tank's own thermostat is
  unaffected. After that the module persists both the enable flag and the key,
  so subsequent reboots need nothing.
- **If `registered` stays false:** check `last_registration_result`. 404 means
  the rewritten config was never fetched — re-register again. 412 means the
  module is armed but has no key — it should then fetch `lan.json`; watch for
  `key_served: true`.
- **If key exchanges fail with "bad LAN signature":** the module holds a key
  this relay did not issue (e.g. from an earlier experiment whose key was never
  saved). It will not ask for a replacement on its own. The known-clean
  recovery is a fresh relay key *plus* getting the module back to a keyless
  state, and the clean way to force that is not yet established (the agent
  wipes its key only on local config changes, never over the network). Until
  it is, the cloud poll path remains the fallback for such a module. Treat
  deleting `/data/lanip.json` on an armed system as the same problem in
  miniature: don't.
- **Opting out:** `lan_mode: false` (or `LAN_MODE=0`) disables the server and
  the DSN rewrite; writes then wait for a cloud poll, and nothing about the
  module's configuration changes back. To disarm the module itself you would
  have to make it re-register against the *real* Ayla without the relay in the
  path (remove the NAT steering first).

## Security notes

- The shared key is generated locally and never sent to A.O. Smith. It is not
  logged. It is stored unencrypted at `/data/lanip.json` with mode 0600 —
  protect `/data` accordingly.
- The LAN endpoints speak only the encrypted protocol; a client without the
  key cannot read telemetry or issue writes. The registration endpoint on the
  *module* is unauthenticated by design (that is Ayla's protocol, not our
  choice), so treat the LAN segment as trusted, as with the rest of this
  project.
- The module's cloud traffic is still forwarded to A.O. Smith. LAN mode removes
  the cloud from the *control* path, not from the device.
