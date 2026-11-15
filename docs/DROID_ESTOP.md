# The emergency stop of a droid in the safety state of a cell

A droid (a Spot walking in the cell) that has been cut by its bridge must **keep new jobs from starting** in the cell and **be shown**. It must **never stop a machine
that is running**: a cut robot is not a reason to stop a router in mid-cut unless the installation decides so. This service reads the state of the droid bridge and turns it into
the safety verdict it already signs, the same way it does for the alarm of A.R.M.O.R. (`docs/ARMOR_ALARM.md`).

**It has not been tried with a real droid or a real broker.** It is tested with built messages, a simulated clock and a fake MQTT client.

## What it reads

HYDRA-UMC-BRIDGE-DROIDS cuts the session of the robot (the software e-stop endpoint stops being checked in) when an operator asks for it on `hydra/bridges/droids/cmd/estop`,
when the global stop is announced and when the link is lost, and says so on two retained topics:

| topic | payload | used for |
|-------|---------|----------|
| `hydra/bridges/droids/state` | JSON with `session_ready` (true/false), `cut_reason` (text, empty while nothing is cut), `link_lost` (true/false) | a non-empty `cut_reason` or `link_lost: true` is a cut |
| `hydra/bridges/droids/availability` | `online` / `offline` (the Last Will of the bridge) | `offline` makes the bridge `unknown` |

The bridge publishes its state when an e-stop command is handled or when it is asked (`cmd/status`), not on a timer, so the retained message IS applied here (unlike the alarm,
where a retained message is ignored): a retained cut inhibits at once and a retained clear counts like any other clear, behind the hysteresis.

## The switch and the settings

Off unless asked: `"droidEstop": { "enabled": true }` in the zones file (and `pip install "hydra-umc-safety-zones[mqtt]"` for the broker link). Nothing is hard-coded:

| field | default | meaning |
|-------|---------|---------|
| `enabled` | `false` | the switch |
| `topic` | `hydra/bridges/droids/state` | the retained state topic of the bridge (`hydra/bridges/<name>/state`, no wildcard); the availability topic is its sibling |
| `maxAgeS` | `0` | seconds of silence after which the bridge is `unknown` (`0`: not checked, because the bridge speaks on events) |
| `hysteresisS` | `10` | seconds a "no cut" must last before the inhibition is lifted |
| `unknownInhibits` | `true` | a bridge never heard from, offline or silent inhibits too (false: it is only shown) |
| `safeStop` | `false` | a cut droid becomes SAFE_STOP of the cell (see below) |
| `auditLog` | `""` | file for the audit lines (`-`: standard error; empty: kept in memory and on `GET /droid-estop`) |
| `stateFile` | `""` | a file that makes `enabled` changeable while the service runs (see below); empty: the switch is only the zones file |
| `broker` | `127.0.0.1:1883` | `host`, `port`, `usernameFile`, `passwordFile`, `tls`, `caFile`, `internal`, `clientId` |

The messages of the bridge are not signed, so a broker that is not on this machine is accepted only when the installation declares it `internal`.

## What it does

- **Cut or lost link -> INHIBITED.** A verdict that would be READY becomes INHIBITED, with the reason, and the answer carries `droidEstop` (state, reason, age). A DANGER stays
  DANGER. `GET /droid-estop` (same token as `GET /state`) shows the state and the last audit lines for Studio and the apps.
- **No news is not "no cut".** A bridge never heard from, offline, or (with `maxAgeS`) silent is `unknown` and, by default, inhibits.
- **Lifting.** Only when the bridge itself says the session has no cut reason and no lost link, and `hysteresisS` seconds pass with nothing else; a corrupt message, a new cut or
  an `offline` restarts the wait. A session that was never started (`session_ready: false` with no reason) is not a cut.
- **The machines that run.** A verdict that is INHIBITED only because of a cut droid carries `droidInhibitOnly`, and the safe heartbeat on CAN keeps going: it stops new starts,
  never a running machine. Any other inhibition (an observer that is down, an expired verdict) still silences the heartbeat.
- **SAFE_STOP only by the switch.** With `safeStop` on, a cut droid is a DANGER of the cell (SAFE_STOP) and the heartbeat stops.

## The switch while the service runs

With `droidEstop.stateFile` set, `GET /settings/droid-estop` shows the switch (`enabled`, `default` false, `running`) to any caller the service accepts, and `POST /settings/droid-estop` with
`{"enabled": true|false, "confirm": true}` changes it. Only an administrator may change it (with the identity of the server on, a token of the role `admin`; a caller with the token of the
service itself counts as one), a body of another shape is a 400, a missing `confirm: true` is a 400 with the code `CONFIRMATION_REQUIRED`, and a service with neither a token nor the
identity of the server refuses with a 403. The change is all or nothing: switching on starts the broker link, switching off stops it and forgets what was heard (so switching on again starts from
"nothing heard yet"), and the choice is written to the state file; if the link cannot start or the file cannot be written, the earlier state is put back and the answer is a 502. At start-up the
value in the state file wins over `enabled` of the zones file; a state file that is damaged stops the start. Every change is audited with who made it. HYDRA-UMC-SERVER carries the change
from Studio and the Suite (`/api/droid-estop/settings`).

## Not done yet

The bridge publishes its state at every cut only from the version of HYDRA-UMC-BRIDGE-DROIDS that carries that change; before it, only when it handles an e-stop command or a status request. Bench: cut the session of a real Spot through the bridge and read `GET /droid-estop` and the verdict
of `/check` (it must be INHIBITED within a second and the heartbeat must go on).
