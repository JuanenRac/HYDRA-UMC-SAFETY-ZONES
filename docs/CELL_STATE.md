# The state file of the cell (`cellState`)

The bridges (HYDRA-UMC-BRIDGE-LASER, -CNC and -PRINTER3D) take no job unless the cell is `READY`. With `[cell_state] source = "file"` each of them reads the first word of a small file (for example `/run/hydra-umc/cell_state`) and reads a missing, unknown or older-than-`max_age_seconds` file as `SAFE_STOP`. This service writes that file from its own verdicts, so the state of the safety zones reaches the bridges by a path that is implemented and tested.

It is **off by default**. Switch it on in the zones file:

```json
{ "cellState": { "enabled": true, "path": "/run/hydra-umc/cell_state", "refreshS": 2.0, "verdictMaxAgeS": 5.0, "maxAgeS": 30.0 } }
```

Every value is in the file, none in the code. `maxAgeS` must be the `max_age_seconds` the bridges use; the file is rewritten every `refreshS` seconds, which has to fit three times in `maxAgeS`, and `verdictMaxAgeS` may not be above it. A value outside its range, an unknown field or a path that is not absolute is refused and the service does not start.

## What is written

One word and a newline: `READY`, `INHIBITED`, `FAULT` or `SAFE_STOP` (the SDK safety state of the last verdict of `POST /check`).

| Situation | Word |
| --- | --- |
| the last verdict is `READY`, is not older than `verdictMaxAgeS`, and no hold is in force | `READY` |
| the last verdict is older than `verdictMaxAgeS` (nobody judged the cell for too long) | `INHIBITED` |
| no verdict yet, or a verdict without a known state | `INHIBITED` |
| a hold is in force (`--hold`) | `INHIBITED` |
| the zones do not come from a file of this machine | `INHIBITED` |
| the verdict is `INHIBITED`, `FAULT` or `SAFE_STOP` (a breach, an expired calibration, a stale observer, the ARMOR alarm, a cut droid) | that word |
| the service is stopped cleanly | `SAFE_STOP` |
| the service is killed or hangs | the file stops being rewritten and the bridges read it as `SAFE_STOP` after `maxAgeS` |

## Rules

- A change of the word is written at once; the file is also rewritten every `refreshS` seconds when nothing changed, so that a silent service is told from a quiet cell.
- The file is replaced whole through a temporary file created exclusively and without following a link (`O_EXCL | O_NOFOLLOW`): a bridge reads all of a word or the earlier one, and a link left in the folder is never written through.
- When the file cannot be written (the folder is missing, read-only, or writable by anybody) it is **removed**, so the bridges read `SAFE_STOP` and never keep an older `READY`. The failure is announced once on standard error.
- The folder is not created by the service. The installation provides it: writable by the account of this service and readable by the accounts of the bridges (for example a shared group and mode `0750`), and the unit needs it in its writable paths (a drop-in with `ReadWritePaths=/run/hydra-umc`, because the unit protects the system read-only).
- It only observes: it never changes a verdict, never blocks `POST /check` and never raises into the service.
- Not tried with a real bridge on a real cell. The tests read the file the way the bridges read it.
