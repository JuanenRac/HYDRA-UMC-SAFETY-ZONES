# Changelog

All notable changes to HYDRA-UMC-SAFETY-ZONES are documented in this file.

Versioning follows the ecosystem-wide `MAJOR.MINOR.PATCH` "odometer" scheme,
applied automatically on every real build by `bump_version.py` (invoked
from build.sh/build.bat right before the compile-check): `PATCH` goes up by
1 per build; once `PATCH` would exceed 9 it resets to 0 and `MINOR` goes up
by 1 instead (e.g. `0.0.9` -> `0.1.0`), the same carry cascading into
`MAJOR` if `MINOR` also exceeds 9. `MAJOR` is otherwise only ever bumped by
hand.

## [Unreleased] - finite spatial-config fail-safe gate

- **`config.py` / `main.py`** - zone and detection coordinates now require
  finite numeric `x`, `y` and `z` values. A malformed `NaN`/infinite point
  causes the command to report `INHIBITED` (exit 3), rather than evaluating
  an untrustworthy boundary and risking a false ready state.
- Added configuration and CLI regression tests for this fail-safe path.

## [0.0.5] - Real v0: JSON/HTTP server mode, plus CM5 deployment

- **`config.py`** - `load_zones`/`load_zone_set`/`load_detections` split
  into a real file-reading wrapper plus a new `parse_zones`/
  `parse_zone_set`/`parse_detections` that accepts already-loaded JSON
  directly - the file-path form only ever made sense for the CLI,
  running on the same machine as the file. Behavior-preserving: the 3
  existing functions keep their exact signature and semantics.
- **`api.py`** (new) - `POST /check` reaches the exact same
  `evaluate_safety()`/`check_breaches()`/`request_estop_for()` functions
  the CLI's own `check` subcommand already runs, with `zones`/
  `detections` in the JSON body via the new `parse_*` functions above.
  Never asserts an E-STOP itself, only ever requests one
  (`NullEStopRequester`, same as the CLI) - a real transport still has to
  be wired to actually assert it. Real gap this closes: this project's
  own zone-breach/E-STOP-request logic was only ever reachable as a
  one-shot CLI.
- **`main.py`** - new `serve` subcommand (`--addr`/`--port`, default
  `127.0.0.1:8108`).
- **`systemd/hydra-umc-safety-zones.service`** (new) - loopback-only unit
  for `HYDRA-UMC-OS/provisioning/install_safety_zones.sh` (new, that
  repo), same stdlib "copy src/ + PYTHONPATH" shape as
  `install_datalake.sh`.
- 9 new tests (`tests/test_api.py`, real end-to-end HTTP, reusing this
  repo's own `tests/test_cli.py` fixture shapes) - 57 total.

## [0.0.4] - established: real calibration-freshness enforcement, fail-safe by default
### Added
- `calibration.py` - real `ZoneCalibration` (version, source, `calibrated_at`, `max_age_days`) and `parse_calibration()`, raising `CalibrationError` on any missing/malformed field. `calibration_age_days()`/`is_calibration_expired()` treat a calibration older than its own `max_age_days`, or dated in the future (clock skew/bad data), as invalid - `max_age_days` itself is inclusive (a calibration exactly that many days old is still trusted, one day older is not).
- `zones.py` - new `ZoneSet` (zones + an optional `ZoneCalibration`); `calibration=None` represents a zones file that never declared one at all, treated identically to an expired one downstream.
- `config.py` - new `load_zone_set()`, parsing the same file `load_zones()` does plus an optional top-level `"calibration"` object. A missing `"calibration"` key loads successfully with `calibration=None` - deliberately not a load error, since the point of `ZoneSet` is to let the caller fail safe on missing calibration, not fail to load at all.
- `safety_state.py` - new `SafetyState` (READY/WARNING/DANGER/INHIBITED) and `evaluate_safety()`: the single real fail-safe decision this project's E-STOP orchestration depends on. Calibration is checked FIRST, before any breach logic runs - a missing or expired calibration always resolves to INHIBITED with a human-readable reason, even when the (untrusted) geometry would otherwise report a real-looking Danger breach.
- `main.py`'s `check` subcommand now loads a `ZoneSet` and reports the real `SafetyState`; new exit code 3 for INHIBITED, distinct from 0 (Ready)/1 (Warning)/2 (Danger).
- 23 new real tests: calibration parsing (valid, every missing/empty required field, bad date, bad `max_age_days`), the exact boundary of `max_age_days` (valid at exactly N days, expired at N+1), a future-dated calibration, `evaluate_safety()` for all four states including "expired calibration wins over a real danger breach", `load_zone_set()` with and without a `"calibration"` key, and 2 new CLI end-to-end cases proving a missing/expired calibration inhibits regardless of object position - 44/44 passing.
- Real verification beyond the test suite: ran `check` against a real zones file with no calibration key and an object nowhere near any zone - confirmed `SAFETY STATE: INHIBITED`, exit 3, not the `READY` a position-only check would have reported. Ran it again with a valid same-day calibration and an object inside the danger zone - confirmed `SAFETY STATE: DANGER`, real E-STOP request printed, exit 2, matching pre-existing behavior unchanged.
- Manifest maturity promoted `functional` -> `established` after the project's vital-improvement gate: versioned geometry, boundary validation, and fail-safe inhibition whenever calibration is missing, malformed, future-dated, or expired.

## [0.0.3] - Real v0 zone-breach checking and E-STOP requesting
### Added
- `geometry.py` - real `Point3D`/`AABB` primitives, inclusive-boundary containment check, hardware-independent by design.
- `zones.py` - real `ZoneLevel` (WARNING/DANGER) and `Zone` definitions.
- `breach.py` - `check_breaches()`: real breach detection between detected objects and zones; `worst_level_per_object()` collapses multi-zone breaches to the single worst outcome per object.
- `estop.py` - the code-level enforcement of the detect-vs-enforce boundary: `EStopRequest`/`EStopRequester` (a `Protocol`) plus `NullEStopRequester`, a real, honest requester that records requests without transmitting anything - there is no real CAN transport in this repository yet, and this module exists specifically to keep that boundary sharp rather than blur it. `request_estop_for()` requests a stop for every object whose worst breach is DANGER; WARNING-only breaches never reach the request path.
- `config.py` - real JSON loading for zones and detected-object positions.
- `main.py` - new `check --zones PATH --detections PATH` subcommand: exit 0 (no breach), 1 (Warning-only), 2 (Danger, E-STOP requested). Bare invocation is unchanged.
- 21 new real tests (`tests/`) - AABB containment (including boundary and corner-order normalization), breach detection across single/multiple zones and objects, E-STOP requesting for every level combination, JSON config round-trips, and a real end-to-end CLI round-trip for all three exit-code paths.
- Real verification beyond the test suite: ran `check` against real JSON fixtures for the no-breach, Warning-only and Danger cases, confirming the printed report and exit code for each.

### Fixed
- `build.sh` called `bump_manifest_version.py` (no `--sync`) as its very first line, before also calling `bump_version.py` later - the same double-bump pattern found in HYDRA-UMC-SYNTHETIC-DATA-GEN's build.sh. Rewritten to bump the native version first, then sync the manifest, matching the rest of the ecosystem's build scripts.

## [0.0.2]

Polish pass: copyright headers normalized across `main.py`, `__init__.py`,
`bump_version.py` and `build.sh`/`build.bat`/`run.sh`/`run.bat`; "why"
comments added, including the detect-vs-enforce boundary between this
service and the firmware's own E-STOP hardware; this `CHANGELOG.md`
added; README (5 languages) expanded with an Advanced Technical
Information section, a detailed Build & Run walkthrough with
troubleshooting, a dateless "Current Status & Next Steps" section
replacing the previous dated roadmap, and a full Related Projects
section. No behavior change - the bump is this verification build.

## [0.0.1]

Real build verification. `build.sh`/`build.bat` run end-to-end for real:
odometer bump, `.venv` creation, editable install, `python -m compileall`
clean across `src/`. `run.sh`/`run.bat` executed the entry point for real,
printing name + version + role. No business-logic change - the bump is the
recorded event.

## [0.0.0]

Initial skeleton: `pyproject.toml` (package metadata, no runtime
dependencies yet), `src/hydra_umc_safety_zones/` (`__init__.py` +
`main.py` entry point reading its version from installed package
metadata), `bump_version.py` (odometer-style version bump),
`build.sh`/`build.bat` (venv + editable install + compile-check) and
`run.sh`/`run.bat`.
