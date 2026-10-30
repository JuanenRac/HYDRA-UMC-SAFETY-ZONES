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
usage: hydra-umc-safety-zones [-h] {check,obstacles,serve} ...

positional arguments:
  {check,obstacles,serve}
    check       Check detected objects against zones and request E-STOP for
                Danger breaches.
    obstacles   Export the zones as sphere obstacles (the scenario format of
                a path planner) that cover every zone completely.
    serve       Run 'check' as a JSON/HTTP API (POST /check) - the exact
                same evaluate_safety()/check_breaches()/request_estop_for()
                functions the CLI already runs.

options:
  -h, --help  show this help message and exit
```

Bare invocation (no subcommand) prints identity/version/role and exits `0`:

```
$ hydra-umc-safety-zones
HYDRA-UMC-SAFETY-ZONES v0.1.3
Real-time 3D intrusion detection and E-STOP orchestration for robotic safe-working areas.
```

## Commands

### `check --zones PATH --detections PATH [--observation PATH]`

```
$ hydra-umc-safety-zones check -h
usage: hydra-umc-safety-zones check [-h] --zones ZONES --detections
                                     DETECTIONS [--observation OBSERVATION]

options:
  -h, --help            show this help message and exit
  --zones ZONES         Path to a zones JSON file.
  --detections DETECTIONS
                        Path to a detected-objects JSON file.
  --observation OBSERVATION
                        Path to an observation-status JSON file - real
                        evidence the supplied detections were actually
                        produced by an active, recently-updated observer.
                        Omitting this always resolves to INHIBITED, never a
                        silent READY - see evaluate_safety()'s own fail-safe
                        default.
```

`--zones` is a JSON file shaped like
`{"zones": [{"id", "level": "warning"|"danger", "min": {"x","y","z"}, "max": {"x","y","z"}}, ...], "calibration": {...}}`.
`--detections` is shaped like `{"objects": [{"id", "position": {"x","y","z"}}, ...]}`.
`--observation` is shaped like
`{"active": bool, "observedAt": ISO-8601 string or null, "maxAgeSeconds": number, "error": string or null}` —
real evidence that `--detections` was actually produced by an active,
recently-updated observer, never guessed or defaulted. `observedAt`
accepts a bare `Z` suffix as well as an explicit UTC offset.

There are three checks, run in this fixed order, each able to
short-circuit the rest: **calibration** first (a missing or expired
calibration always wins over what the untrusted geometry would
otherwise report), then **observation** (no observer evidence at
all, a disabled observer, an observer-reported error, or a stale
observation all win over what the geometry would otherwise report), and
only then the real breach check. All examples below run against the
same two nested zones (`warn1`: a 10×10×10 cube; `danger1`: a 2×2×2 cube
inside it) with a fresh calibration and a fresh, active observation
unless the example is specifically about calibration or observation
failing.

**READY** — the detected object is far outside both zones, calibration
is fresh, and the observer is real, active and fresh:

```
$ hydra-umc-safety-zones check --zones zones-valid.json --detections detections-clear.json --observation observation-active.json
SAFETY STATE: READY - no breach, calibration valid
$ echo $?
0
```

**WARNING** — the object is inside the warning zone but not the inner
danger zone:

```
$ hydra-umc-safety-zones check --zones zones-valid.json --detections detections-warning.json --observation observation-active.json
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
$ hydra-umc-safety-zones check --zones zones-valid.json --detections detections-danger.json --observation observation-active.json
SAFETY STATE: DANGER - object(s) ['op1'] breached a danger zone
BREACH: object 'op1' inside warning zone 'warn1'
BREACH: object 'op1' inside danger zone 'danger1'
E-STOP REQUESTED: object 'op1' breached danger zone 'danger1' (not asserted - see estop.py)
$ echo $?
2
```

**INHIBITED — no calibration at all.** Fail-safe: this wins over
geometry (and over the observer check below it) even when the object is
deep inside the danger zone (this run reuses `detections-danger.json`
to prove the short-circuit — no `BREACH`/`E-STOP` line is printed):

```
$ hydra-umc-safety-zones check --zones zones-missing-cal.json --detections detections-danger.json --observation observation-active.json
SAFETY STATE: INHIBITED - no calibration metadata present - zone geometry cannot be trusted
$ echo $?
3
```

**INHIBITED — expired calibration.** `max_age_days=30`, calibrated long
ago; a real danger-zone breach still resolves to INHIBITED, not DANGER:

```
$ hydra-umc-safety-zones check --zones zones-expired-cal.json --detections detections-danger.json --observation observation-active.json
SAFETY STATE: INHIBITED - calibration 'cal-0' (source=manual) is 2447 day(s) old, exceeds max_age_days=30
$ echo $?
3
```

**INHIBITED — `--observation` omitted entirely.** The exact real
anti-pattern this fix closes: a caller not yet updated to pass
`--observation` at all must never silently get `READY` back just
because the flag wasn't given — calibration is fresh and the object is
nowhere near a zone, and the result is still INHIBITED:

```
$ hydra-umc-safety-zones check --zones zones-valid.json --detections detections-clear.json
SAFETY STATE: INHIBITED - no observation status provided - cannot confirm the supplied detections reflect a real, active observer
$ echo $?
3
```

**INHIBITED — observer disabled.** The literal acceptance
case: removing detections (`{"objects": []}`) while the observer itself
reports `active: false` must not read as a confirmed-clear zone:

```
$ hydra-umc-safety-zones check --zones zones-valid.json --detections detections-empty.json --observation observation-disabled.json
SAFETY STATE: INHIBITED - tracking observer is disabled - dependent functions are inhibited
$ echo $?
3
```

**INHIBITED — stale observation.** The observer's own
`observedAt` is older than its own `maxAgeSeconds` — the real reason
names the actual age and limit, never a vague "stale" label:

```
$ hydra-umc-safety-zones check --zones zones-valid.json --detections detections-empty.json --observation observation-stale.json
SAFETY STATE: INHIBITED - last real observation is 211442476.5s old, exceeds max_age_seconds=3600.0
$ echo $?
3
```

**INHIBITED — observer-reported internal error.** The literal
acceptance case: a real error reported by the observer itself
blocks logical enablement, naming the real error text:

```
$ hydra-umc-safety-zones check --zones zones-valid.json --detections detections-empty.json --observation observation-error.json
SAFETY STATE: INHIBITED - observer reported an internal error: camera driver disconnected
$ echo $?
3
```

**INHIBITED — non-finite coordinate.** A `NaN`/`Infinity`/`-Infinity`
`x`/`y`/`z` in either JSON file is caught by `config.py` *before*
`evaluate_safety()` ever runs, and treated exactly as untrustworthy as a
missing calibration — never silently coerced or skipped:

```
$ hydra-umc-safety-zones check --zones zones-valid.json --detections detections-nan.json --observation observation-active.json
SAFETY STATE: INHIBITED - invalid safety configuration: point.x must be finite
$ echo $?
3
```

**INHIBITED — malformed `--observation` file.** A structurally invalid
observation document (here, `active` is not a real boolean) is caught
before `evaluate_safety()` runs, same as a malformed `--zones`/
`--detections` file:

```
$ hydra-umc-safety-zones check --zones zones-valid.json --detections detections-clear.json --observation observation-malformed.json
SAFETY STATE: INHIBITED - invalid safety configuration: observation.active must be a boolean: 'not-a-boolean'
$ echo $?
3
```

**A file that is missing, is not JSON or lacks a field** is as untrustworthy as a stale
calibration: it is reported as `INHIBITED` and exits `3`, never as a traceback with status `1`
(which means `WARNING`, "slow down"):

```
$ hydra-umc-safety-zones check --zones does-not-exist.json --detections detections-clear.json --observation observation-active.json
SAFETY STATE: INHIBITED - invalid safety configuration: FileNotFoundError: [Errno 2] No such file or directory: 'does-not-exist.json'
$ echo $?
3
```

The same holds for `NaN`/`Infinity` anywhere in a file (Python's `json` accepts them by default;
here they are refused), an `observedAt` without a time zone, a `level` that is not lower case
(`"danger"`, not `"DANGER"`), an empty `zones` list, a zone or object `id` that is empty or used
twice, and a coordinate that is a boolean or a string.

`--tool-velocity N` (meters/second, finite and not negative) grows every zone by the same
velocity-proportional margin the API's `toolVelocityMps` does (`0.15 m` per m/s, capped at `0.5 m`).

Two clock tolerances keep a healthy cell from being inhibited by rounding: an observation
timestamped up to 0.25 s ahead of this machine's clock is not "in the future", and a calibration
dated one day ahead (a local date written east of UTC) is not expired. Anything further ahead is.

### `serve [--addr ADDR] [--port PORT] [--zones-file PATH] [--token T] [--max-observation-age-seconds S] [--max-calibration-age-days D] [--decision-log FILE]`

```
$ hydra-umc-safety-zones serve -h
usage: hydra-umc-safety-zones serve [-h] [--addr ADDR] [--port PORT]

options:
  -h, --help   show this help message and exit
  --addr ADDR  address to bind the HTTP API to (default: 127.0.0.1)
  --port PORT  port for the HTTP API (default: 8108)
```

Runs the exact same `evaluate_safety()`/`check_breaches()`/
`request_estop_for()` functions as `check`, over a plain stdlib
`http.server` JSON API instead of files on disk — `zones`/`detections`/
`observation` travel in the request body. Binds to loopback
(`127.0.0.1:8108`) by default, matching the
`systemd/hydra-umc-safety-zones.service` unit.

* **`GET /stats`** — `{"role": "..."}`, a liveness/identity check.
* **`GET /state`** — the latest verdict of `POST /check`: `{"sdkSafetyState": {schema_version, state, source, timestamp_utc}, "reason": ..., "configSha256": ...}`, `200`;
  `404` before the first verdict. Needs the bearer token when one is configured. A consumer judges the age of
  `timestamp_utc` itself.
* **`POST /check`** — body `{"zones": {...}, "detections": {...}, "observation": {...}}`,
  using the exact same shapes as the `--zones`/`--detections`/
  `--observation` files above. `"observation"` is an optional key (a
  caller not yet updated to send it at all is not rejected with a `400`
  for a field that didn't exist before this check) — but omitting it always
  resolves to `"inhibited"`, never a silent `"ready"`, via
  `evaluate_safety()`'s own fail-safe default:

  ```json
  {"zones": {...}, "detections": {...}}
  ```
  ```json
  {"state": "inhibited", "reason": "no observation status provided - cannot confirm the supplied detections reflect a real, active observer", "breaches": [], "estopRequests": [], "sdkSafetyState": {"schema_version": "1.0", "state": "INHIBITED", "source": "hydra-umc-safety-zones", "timestamp_utc": "..."}}
  ```

  A disabled observer resolves the same way even with `"detections":
  {"objects": []}` (the literal acceptance case over real HTTP —
  removing detections must not, by itself, ever look like a confirmed-clear
  zone):

  ```json
  {"zones": {...}, "detections": {"objects": []}, "observation": {"active": false, "observedAt": null, "maxAgeSeconds": 5}}
  ```
  ```json
  {"state": "inhibited", "reason": "tracking observer is disabled - dependent functions are inhibited", "breaches": [], "estopRequests": [], "sdkSafetyState": {"schema_version": "1.0", "state": "INHIBITED", "source": "hydra-umc-safety-zones", "timestamp_utc": "..."}}
  ```

  An object may carry `"radiusM"` (meters, default `0`): it is then a sphere and breaches every
  zone it touches. `"toolVelocityMps"` (optional, finite and not negative) grows the zones as above.
  An observer that reports an `error` is published as SDK state `FAULT` (a broken detector), a stale
  or missing one as `INHIBITED` (not verified); both keep the internal `state` `"inhibited"`.

  **Who decides the zones.** By default the request brings its own `zones` and calibration, so
  whoever can reach the port decides them: keep it on loopback, or use the options below.
  `--zones-file PATH` (or `SAFETY_ZONES_FILE`) loads the zones and calibration once at start and a
  request that sends `zones` is refused with `400`; every verdict then carries `configSha256`, the
  hash of that file. `--max-observation-age-seconds` and `--max-calibration-age-days` refuse a request
  whose evidence claims to stay valid longer. `--token` (or `SAFETY_ZONES_TOKEN`) makes `POST /check`
  demand `Authorization: Bearer <token>` (`401` otherwise); `GET /stats` stays open.
  `--decision-log FILE` (or `SAFETY_ZONES_DECISION_LOG`; `-` is standard error) appends one JSON line per
  verdict: `at`, `state`, `sdkSafetyState`, `reason`, `configSha256`, `breaches`, `estopRequests`. It is off
  unless given, and a log that cannot be written never changes the verdict.

  Requests are limited: a body over 1 MiB is refused, a client silent for 10 s is dropped, and at most
  32 connections are served at once (`503` beyond). Responds `200` with `{"state", "reason", "breaches", "estopRequests",
  "sdkSafetyState"}` for a well-formed request (including `"inhibited"`,
  which still reports a normal `200` — inhibition is an expected safety
  outcome, not a request error); responds `400` with `{"error": "..."}`
  for a malformed JSON body, a missing `zones`/`detections` key, an
  invalid safety configuration (a non-finite coordinate, same as the
  CLI, `NaN`/`Infinity` anywhere, a time without a time zone), or a structurally malformed `observation` value:

  ```json
  {"zones": {...}, "detections": {...}, "observation": {"active": "not-a-boolean"}}
  ```
  ```json
  {"error": "invalid safety configuration: observation.active must be a boolean: 'not-a-boolean'"}
  ```

  Any other failure inside the check answers `500` with `"state": "inhibited"` and SDK state `FAULT`,
  never a closed connection and never a state that could be read as clear.

  Never asserts an E-STOP — only ever requests one, same
  `NullEStopRequester` as the CLI. An object inside several danger zones produces one request that
  names all of them (`zone_ids`).

### `obstacles --zones PATH [--level all|danger|warning] [--cell M] [--tool-velocity V] [--output FILE]`

Exports the zones as sphere obstacles, in the `obstacles` format of a path-planner scenario (`{"obstacles": [{"center": {"x", "y", "z"}, "radius": r}]}`), so a planner that only knows spheres can route around the zones. Each box is cut into cubes of side `--cell` (default `0.5` m) and every cube is covered by its circumscribed sphere, so the spheres always contain the whole zone: the export can over-cover a zone but never leaves a corner free. A smaller cell hugs the box more tightly with more spheres; more than 5000 spheres is refused. With `--tool-velocity` (or a `separation` model in the zones file) the zones first grow by the same margin `check` applies. The JSON goes to standard output, or to `--output`. A file that cannot be read exits `3` and writes nothing.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | `READY` — no breach, calibration valid, observer active and fresh |
| `1` | `WARNING` — a warning-zone breach. Never an error: an input that cannot be read is `3` |
| `2` | `DANGER` — a danger-zone breach; E-STOP was requested (not asserted) |
| `3` | `INHIBITED` — calibration missing/expired, or no observer evidence, a disabled/stale/errored observer, or any input file that is missing, malformed or invalid (including `NaN`/`Infinity` and times without a time zone); the fail-safe path, checked before any breach logic runs |

## Not yet implemented

E-STOP is *requested* (`estop.py`'s `NullEStopRequester`, a real object
that records the request) but never actually *asserted* over real CAN
hardware — there is no live safety-rated transport wired up yet. Real
Hailo-8-based 3D occupancy mapping (producing the `--detections` input
this CLI consumes today from a plain JSON file) is also not built —
`check` is deliberately detector-agnostic so it can be tested and used
against any real or synthetic detection source. Real observer-health
reporting (populating `--observation`/`"observation"` from an actual
tracking process's own liveness, rather than a hand-authored JSON file)
is likewise out of scope for this repo — this repo defines and enforces the
contract, not the producer.
