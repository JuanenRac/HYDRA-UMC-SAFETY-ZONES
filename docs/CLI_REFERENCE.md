# HYDRA-UMC-SAFETY-ZONES — CLI Reference

`hydra-umc-safety-zones` is a Python console script
(`src/hydra_umc_safety_zones/main.py`, installed as an entry point via
`pyproject.toml`). Real v0 is zone-breach checking and E-STOP
*requesting* (not asserting — see `estop.py`) against zones/detections
supplied as plain JSON files, deliberately independent of any specific
upstream detector. Real Hailo-8 occupancy mapping and real CAN transport
for actually asserting an E-STOP are out of scope for this CLI today.
Every example below was captured from a real run of the installed CLI
against real fixture JSON files — not written from memory.

## Usage

```
$ hydra-umc-safety-zones -h
usage: hydra-umc-safety-zones [-h] {check} ...

positional arguments:
  {check}
    check     Check detected objects against zones and request E-STOP for
              Danger breaches.

options:
  -h, --help  show this help message and exit
```

Bare invocation (no subcommand) prints identity/version/role and exits `0`:

```
$ hydra-umc-safety-zones
HYDRA-UMC-SAFETY-ZONES v0.0.4
Real-time 3D intrusion detection and E-STOP orchestration for robotic safe-working areas.
```

## Commands

### `check --zones PATH --detections PATH`

```
$ hydra-umc-safety-zones check -h
usage: hydra-umc-safety-zones check [-h] --zones ZONES --detections DETECTIONS

options:
  -h, --help            show this help message and exit
  --zones ZONES         Path to a zones JSON file.
  --detections DETECTIONS
                        Path to a detected-objects JSON file.
```

`--zones` is a JSON file shaped like
`{"zones": [{"id", "level": "warning"|"danger", "min": {"x","y","z"}, "max": {"x","y","z"}}, ...], "calibration": {...}}`.
`--detections` is shaped like `{"objects": [{"id", "position": {"x","y","z"}}, ...]}`.
Calibration is checked **first**, before any breach logic runs — a
missing or expired calibration always wins over what the (untrusted)
geometry would otherwise report. There are four real, distinct
`SAFETY STATE` outcomes, each with its own exit code, all reproduced
below against the same two nested zones (`warn1`: a 10×10×10 cube;
`danger1`: a 2×2×2 cube inside it).

**READY** — the detected object is far outside both zones, calibration
is fresh:

```
$ hydra-umc-safety-zones check --zones zones-valid.json --detections detections-clear.json
SAFETY STATE: READY - no breach, calibration valid
$ echo $?
0
```

**WARNING** — the object is inside the warning zone but not the inner
danger zone:

```
$ hydra-umc-safety-zones check --zones zones-valid.json --detections detections-warning.json
SAFETY STATE: WARNING - object(s) ['op1'] breached a warning zone
BREACH: object 'op1' inside warning zone 'warn1'
$ echo $?
1
```

**DANGER** — the object is inside the inner danger zone (and therefore
also the outer warning zone; both breaches are reported), and E-STOP is
requested — not asserted, per `estop.py`'s own detect-vs-enforce
boundary:

```
$ hydra-umc-safety-zones check --zones zones-valid.json --detections detections-danger.json
SAFETY STATE: DANGER - object(s) ['op1'] breached a danger zone
BREACH: object 'op1' inside warning zone 'warn1'
BREACH: object 'op1' inside danger zone 'danger1'
E-STOP REQUESTED: object 'op1' breached danger zone 'danger1' (not asserted - see estop.py)
$ echo $?
2
```

**INHIBITED — no calibration at all.** Fail-safe: this wins over
geometry even when the object is nowhere near any zone (this run reuses
`detections-danger.json`, deep inside the danger zone, to prove the
short-circuit — no `BREACH`/`E-STOP` line is printed):

```
$ hydra-umc-safety-zones check --zones zones-missing-cal.json --detections detections-danger.json
SAFETY STATE: INHIBITED - no calibration metadata present - zone geometry cannot be trusted
$ echo $?
3
```

**INHIBITED — expired calibration.** `max_age_days=30`, calibrated
`2020-01-01`; a real danger-zone breach still resolves to INHIBITED, not
DANGER:

```
$ hydra-umc-safety-zones check --zones zones-expired-cal.json --detections detections-danger.json
SAFETY STATE: INHIBITED - calibration 'cal-0' (source=manual) is 2431 day(s) old, exceeds max_age_days=30
$ echo $?
3
```

**A real error path** — a missing `--zones` file is not caught and
turned into a friendly message; it's a real, uncaught `FileNotFoundError`
with a Python traceback on stderr, exit code `1`:

```
$ hydra-umc-safety-zones check --zones does-not-exist.json --detections detections-clear.json
Traceback (most recent call last):
  ...
  File ".../src/hydra_umc_safety_zones/config.py", line 55, in load_zone_set
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
                     ...
FileNotFoundError: [Errno 2] No such file or directory: 'does-not-exist.json'
$ echo $?
1
```

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | `READY` — no breach, calibration valid |
| `1` | `WARNING` — a warning-zone breach, or an uncaught Python exception (e.g. a missing/malformed `--zones`/`--detections` file — not yet a handled, friendly error) |
| `2` | `DANGER` — a danger-zone breach; E-STOP was requested (not asserted) |
| `3` | `INHIBITED` — calibration missing or expired; the fail-safe path, checked before any breach logic runs |

## Not yet implemented

E-STOP is *requested* (`estop.py`'s `NullEStopRequester`, a real object
that records the request) but never actually *asserted* over real CAN
hardware — there is no live safety-rated transport wired up yet. Real
Hailo-8-based 3D occupancy mapping (producing the `--detections` input
this CLI consumes today from a plain JSON file) is also not built —
`check` is deliberately detector-agnostic so it can be tested and used
against any real or synthetic detection source.
