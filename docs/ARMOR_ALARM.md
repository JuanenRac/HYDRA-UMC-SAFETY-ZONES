# The alarm of A.R.M.O.R. in the safety state of a cell

A house alarm (A.R.M.O.R.) that sounds, a fire for example, must **keep new jobs from starting** in a cell and **be shown**. It must **never stop a machine that
is running**: stopping a router in mid-cut because a door sensor tripped is a hazard of its own. This service reads the state of the alarm panel and turns
it into the safety verdict it already signs, which the HIL bridge and the semantic planner read.

**It has not been tried with a real alarm, a real panel or a real broker.** It is tested with built messages, a simulated clock and a fake MQTT client.

## Where it lives, and why

The reading lives in this service (`armor_alarm.py`, the MQTT link in `armor_alarm_mqtt.py`) and not in the MQTT bridge of HYDRA-UMC-SERVER:

- this service is the one that makes the verdict the consumers trust (signed, with a counter, checked for age), so a second, parallel source of "state" would
  give a consumer two answers to reconcile; here the alarm is one more reason inside the same verdict;
- it already has the fail-closed rules (an expired verdict, a silent observer, a bad configuration are INHIBITED) and the safe heartbeat on CAN, which has to
  know that the house alarm must not silence it;
- the bridge of the server only relays commands and publishes the global stop; it has no verdict, no signature and no notion of a cell.

## The switch and the settings

Off unless asked: `"armorAlarm": { "enabled": true }` in the zones file (and `pip install "hydra-umc-safety-zones[mqtt]"` for the broker link). Nothing is
hard-coded; every value is in the `armorAlarm` object:

| field | default | meaning |
|-------|---------|---------|
| `enabled` | `false` | the switch |
| `topic` | `armor/alarm/+/state` | the MQTT filter (one panel: `armor/alarm/<node_id>/state`) |
| `nodes` | `[]` | the panels that count (empty: any node id the topic carries); a panel listed here that was never heard from is `unknown` |
| `phaseTable` | `disarmed`, `exit_delay`, `armed`, `entry_delay` -> `READY`; `alarm` -> `INHIBITED` | phase of the panel -> SDK state; the one place where a meaning is chosen |
| `maxAgeS` | `60` | seconds of silence after which a panel is `unknown` |
| `hysteresisS` | `10` | seconds a "no alarm" must last before the inhibition is lifted |
| `unknownInhibits` | `true` | an unknown panel inhibits too (false: it is only shown) |
| `safeStop` | `false` | the switch of the safe stop (see below) |
| `auditLog` | `""` | file for the audit lines (`-`: standard error; empty: kept in memory and on `GET /armor-alarm`) |
| `broker` | `127.0.0.1:1883` | `host`, `port`, `usernameFile`, `passwordFile`, `tls`, `caFile`, `internal`, `clientId` |

The conversion is the one of `armor_alarm_to_safety_state` in HYDRA-UMC-SDK (`docs/ARMOR_ALIGNMENT.md`), kept here without importing the SDK; a test compares
the two when the SDK is next to this repository.

## What it does

- **Alarm sounding -> INHIBITED.** A verdict that would be READY becomes INHIBITED, with the reason, and the answer carries `armorAlarm` (state, nodes, ages).
  A DANGER stays DANGER. `GET /armor-alarm` (same token as `GET /state`) shows the state and the last audit lines for Studio and the apps.
- **No news is not "no alarm".** A panel never heard from, or silent for `maxAgeS` (the broker stopped sending, the link fell), is `unknown`: it is shown
  as such and, by default, inhibits.
- **Lifting.** Only when the panel itself says "no alarm" and `hysteresisS` seconds pass with nothing else: a new alarm, a corrupt message, a contradiction or
  silence restarts the wait. The very first "no alarm" after the start also waits.
- **Fail safe.** A corrupt or contradictory message inhibits and holds the inhibition: invalid JSON or UTF-8, `NaN`, a message over 64 KiB, a `node_id` that is
  not the one of the topic, another `kind`, a phase the table does not know, a `timestamp_ms` that goes backwards (a replay), a siren that sounds in a phase the
  table calls clear. A retained message is never applied.
- **A running machine is never stopped.** The safe heartbeat on CAN goes on while the verdict is INHIBITED only because of the house alarm (the verdict
  carries `armorInhibitOnly`); any other INHIBITED still silences it, as before.
- **Safe stop, only if the installation wants it.** `SAFE_STOP` happens only when `safeStop` is `true` AND the table maps a phase to `SAFE_STOP`. With the
  switch off, a table that says `SAFE_STOP` is read as `INHIBITED` and the downgrade is audited. With it on, the verdict is DANGER (SDK `SAFE_STOP`), signed
  like any other, and the safe heartbeat goes silent. The switch must be offered in Studio, off by default.
- **Audit.** Every change of the answer, every corrupt message, every ignored message and every downgrade is a JSON line (`at`, `source`, `event`, `node`,
  `detail`).

## What is not solved (pending)

- **The messages of the panel are not authenticated.** The contract of A.R.M.O.R. signs only the zone feed that goes to the panel, not the state it publishes.
  Until the panel signs its state, anyone who can publish on the broker can say "no alarm". What this service does: a broker that is not on this machine is
  refused unless the installation declares it internal (`broker.internal`), credentials and TLS are supported, `nodes` limits whose messages count, a replayed
  older message is refused, and a retained one is ignored. Run it on the internal broker only.
- A panel whose clock goes back (a restart without time) is refused as a replay until its new timestamps pass the old ones; it shows as `unknown`/fault, which
  inhibits. A way to reset the baseline is pending.
- The switches (`enabled`, `safeStop`, `unknownInhibits`, times) and the display of `armorAlarm` have to be offered in Studio, in the suite and in the apps: the
  contract for that is the `armorAlarm` object of the answer and `GET /armor-alarm`.
- A CAN stop request that names the alarm (today the safe stop is the silence of the heartbeat).
