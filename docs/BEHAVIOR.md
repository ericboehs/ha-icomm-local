# Appliance behavior and write delivery: the measured details

Background for the README, kept out of the install path. All of this was measured
on an HPS10-50H45DV. The full research, including frame formats and the register
atlas, is in [ericboehs/aosmith-water-heater](https://github.com/ericboehs/aosmith-water-heater).

## Write delivery

A queued write is delivered on the module's next `commands.xml` poll, replacing
whatever Ayla had queued for that reply — verified by queueing 139 °F locally while
a cloud write asked for 142 °F: the tank took 139. Delivery is *not* success; the
write stays `pending` until telemetry shows it took effect, because a frame can be
delivered and ignored (the BLE map's mode register at block 26 is exactly that).

Queued writes are **persisted** (`/data/heaters/<DSN>/pending.json` in the add-on; `QUEUE_FILE` names the data directory), so
a restart no longer drops them — it used to, which was demonstrated while waiting on
an idle appliance. The file is written atomically, a corrupt one is ignored rather
than fatal, and a write older than `QUEUE_MAX_AGE_S` (default 24 h) is dropped on
load instead of being delivered as a surprise. Restored writes report
`restored: true` and `queue_restored` in the state API, so it is visible that a
value came back from before the restart.

Safety rules, all tested: setpoints clamp to the ceiling the appliance advertises
(and to a hard cap, so a garbage `max_setpoint` cannot become an unbounded write);
only the newest write per register survives; a stale appliance **holds** writes
rather than injecting into a device that ignores them.

## Silence is usually idleness, not a fault

This appliance reports on events and on its own schedule — the configuration it
fetches at registration says `superBlockDynDataUpdateRate = 21601`, about six
hours — and it only polls for commands when Ayla says a command exists. A tank
holding temperature therefore goes completely quiet on HTTPS for long stretches
while remaining perfectly healthy: its ops keepalives (UDP, straight through the
router) keep flowing the whole time.

That makes silence ambiguous, and the relay's health model refuses to guess:

| Observation | Verdict |
|---|---|
| quiet, tank satisfied, within its advertised interval | `idle` — normal |
| quiet while calling for heat (20 F below setpoint) | `degraded` — intervene |
| quiet longer than its own reporting interval + 15 min | `degraded` — intervene |
| a write queued and the appliance not asking for commands | `degraded` — wait or reconnect |
| one request per dial, minutes apart, no polls | `degraded` — the researched signature |
| Local Connect delivering changing reports, no cloud request for 30+ min | `degraded` (`code: cloud_idle`) — power-cycle; HA raises a repair |

An earlier version of this file claimed a relay restart costs 20+ minutes of
silence. That was wrong as stated: the silence was the module being idle, and the
claim was an inference from two samples that happened to be quiet for unrelated
reasons.

**A relay restart can, however, stop the cloud side entirely** (measured
2026-10-25).
- **What happened:** the relay was unreachable for a host reboot and then an
  add-on restart. Afterwards the module sent changing telemetry over Local
  Connect for 80+ minutes and not one packet to Ayla; the router captured no SYN
  to :443 and conntrack had no entry.
- **The fix:** a power cycle restored it within seconds.
- **The mechanism, in libada's `client.c`:** a failed connect clears
  `ads_listen` and drops the cloud from the destinations, and with the UDP
  notification channel up the module doesn't poll.
- **How the relay tells it from idleness:** the reports were *changing*, which a
  healthy module would also post to the cloud. The
symptom that actually distinguishes a wedged module — ops keepalives disappearing —
is not visible to the relay at all, which is why it is listed as unobservable
instead of being approximated from timing.

**The honest limitation, now measured rather than suspected:** polls happen only when
Ayla notifies the module that a command exists, so a queued write needs something to
ring that bell. Three candidate local triggers have been tested and all three are
dead:

| Candidate trigger | Result |
|---|---|
| drop the connection (force a re-dial) | re-dials and posts telemetry, **never** fetches `commands.xml` |
| 401 on a GET | an idle module issues no GETs at all, so it never fires |
| **401 on a telemetry POST** | module retries **+6 s**, then resumes its cadence; **zero** `regtoken` fetches, **zero** polls — no re-registration |

That dead end is what LAN mode bypasses: instead of making the module poll the
*cloud*, the relay becomes a *local* command source the module polls every ~10 s
(see "The local doorbell" below). Note
that a *healthy idle* module shows the same trace as the researched "degraded" one —
telemetry-only dials, 60–240 s apart, no polls — so the relay does not claim to
detect a wedge from HTTPS shape alone.


