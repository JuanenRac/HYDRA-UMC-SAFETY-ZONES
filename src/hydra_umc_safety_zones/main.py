# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - entry point: src/hydra_umc_safety_zones/main.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Entry point for HYDRA-UMC-SAFETY-ZONES.

Bare invocation (no arguments) is unchanged from the skeleton stage:
prints identity, version and role, exits 0.

The real `check` subcommand runs this project's actual v0 safety logic -
zone-breach checking and E-STOP *requesting* - against zones/detections
supplied as JSON files, deliberately independent of any specific upstream
detector. See geometry.py/zones.py/breach.py/estop.py for what "real"
means here, and their module docstrings for what is still out of scope
(real Hailo-8 occupancy mapping, real CAN transport).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from hydra_umc_safety_zones.api import SafetyZonesServer
from hydra_umc_safety_zones.breach import check_breaches
from hydra_umc_safety_zones.calibration import CalibrationError
from hydra_umc_safety_zones.config import (
    ConfigError,
    load_detections,
    load_observation_status,
    load_zone_set,
    parse_tool_velocity,
)
from hydra_umc_safety_zones.estop import NullEStopRequester, request_estop_for
from hydra_umc_safety_zones.observation import ObservationError
from hydra_umc_safety_zones.obstacles import DEFAULT_CELL_M, LEVEL_CHOICES, export_obstacles
from hydra_umc_safety_zones.safety_state import SafetyState, evaluate_safety
from hydra_umc_safety_zones.zones import scale_zones_for_velocity

PROJECT_NAME = "HYDRA-UMC-SAFETY-ZONES"
DIST_NAME = "hydra-umc-safety-zones"
ROLE = (
    "Real-time 3D intrusion detection and E-STOP orchestration for robotic "
    "safe-working areas."
)


def get_version() -> str:
    """Read the running version from installed package metadata, which is
    sourced from pyproject.toml - the single place bump_version.py edits.

    Why not a hardcoded __version__ string here instead? That would give
    this project two places to keep in sync on every build. Reading it
    back from installed metadata means this function can never drift out
    of sync with the number bump_version.py actually wrote."""
    try:
        return version(DIST_NAME)
    except PackageNotFoundError:
        return "0.0.0-dev (package not installed - run build.sh/build.bat first)"


# Exit status when the input could not be read or understood at all. It is
# never 1 (WARNING, "slow down") or 0 (READY): a configuration that could not
# be read must not be mistaken for a milder safety state.
EXIT_INVALID_INPUT = 3


def _run_check(
    zones_path: str,
    detections_path: str,
    observation_path: str | None,
    tool_velocity_mps: float | None = None,
) -> int:
    try:
        zone_set = load_zone_set(zones_path)
        objects = load_detections(detections_path)
        # real, required-in-spirit observer evidence - see
        # evaluate_safety()'s own fail-safe default (None -> INHIBITED)
        # for why this stays an optional CLI flag rather than a hard
        # argparse requirement: a caller that omits it gets an honest,
        # visible INHIBITED result, not a crash and not a silent READY.
        observation = load_observation_status(observation_path) if observation_path else None
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the cell is not verified
        # Invalid spatial/calibration/observation data (or a file that is
        # missing, is not JSON, lacks a field...) is as untrustworthy as
        # stale calibration: never fall through to READY, never leave as a
        # traceback with status 1 (which means WARNING).
        print(f"SAFETY STATE: INHIBITED - invalid safety configuration: {type(exc).__name__}: {exc}")
        return EXIT_INVALID_INPUT
    today = datetime.now(timezone.utc).date()

    try:
        evaluation = evaluate_safety(zone_set, objects, today, observation, tool_velocity_mps=tool_velocity_mps)
    except Exception as exc:  # noqa: BLE001
        print(f"SAFETY STATE: INHIBITED - the evaluation itself failed: {type(exc).__name__}: {exc}")
        return EXIT_INVALID_INPUT
    print(f"SAFETY STATE: {evaluation.state.value.upper()} - {evaluation.reason}")

    if evaluation.state is SafetyState.INHIBITED:
        # Fail-safe: geometry cannot be trusted, so no breach logic below
        # runs at all - a stale/missing calibration must never be silently
        # treated as "no breach".
        return 3

    if evaluation.state is SafetyState.READY:
        return 0

    breaches = check_breaches(scale_zones_for_velocity(zone_set.zones, tool_velocity_mps, zone_set.separation), objects)
    for b in breaches:
        print(f"BREACH: object '{b.object_id}' inside {b.level.value} zone '{b.zone_id}'")

    if evaluation.state is SafetyState.WARNING:
        return 1

    requester = NullEStopRequester()
    requests = request_estop_for(breaches, requester)
    for r in requests:
        print(f"E-STOP REQUESTED: {r.reason} (not asserted - see estop.py)")
    return 2


def _run_obstacles(args: argparse.Namespace) -> int:
    """Writes the zones as sphere obstacles for a path planner (JSON on standard output, or to --output)."""
    try:
        zone_set = load_zone_set(args.zones)
        velocity = parse_tool_velocity(args.tool_velocity) if args.tool_velocity is not None else None
        exported = export_obstacles(zone_set.zones, args.level, args.cell, velocity, zone_set.separation)
    except Exception as exc:  # noqa: BLE001 - nothing is exported from a file that could not be read
        print(f"obstacles: could not export the zones: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INVALID_INPUT
    text = json.dumps(exported, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(f"obstacles: {len(exported['obstacles'])} spheres written to {args.output}")
    else:
        print(text)
    return 0


def _run_serve(args: argparse.Namespace) -> int:
    addr, port = args.addr, args.port
    zone_set = None
    config_sha256 = None
    if args.zones_file:
        # Authoritative mode: the zones and the calibration come from a file
        # on this machine, read once at start, and a request cannot bring
        # its own. Without it, whoever can reach the port decides the zones.
        try:
            zone_set = load_zone_set(args.zones_file)
            config_sha256 = hashlib.sha256(Path(args.zones_file).read_bytes()).hexdigest()
        except Exception as exc:  # noqa: BLE001
            print(f"[safety-zones] fatal: could not load --zones-file {args.zones_file}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    try:
        server = SafetyZonesServer(
            (addr, port),
            ROLE,
            token=args.token or None,
            zone_set=zone_set,
            config_sha256=config_sha256,
            max_observation_age_seconds=args.max_observation_age_seconds,
            max_calibration_age_days=args.max_calibration_age_days,
            decision_log=args.decision_log or None,
        )
    except OSError as exc:
        print(f"[safety-zones] fatal: could not start the HTTP API on {addr}:{port}: {exc}", file=sys.stderr)
        return 2
    print(f"[safety-zones] HTTP API listening on {addr}:{port}")
    print("[safety-zones] POST /check, GET /stats")
    if zone_set is not None:
        print(f"[safety-zones] zones and calibration fixed by {args.zones_file} (sha256 {config_sha256})")
    else:
        print("[safety-zones] WARNING: zones and calibration come from each request: whoever reaches this port decides them (use --zones-file)")
    if args.token:
        print("[safety-zones] POST /check requires a bearer token")
    if args.decision_log:
        print(f"[safety-zones] every verdict is logged as a JSON line to {'standard error' if args.decision_log == '-' else args.decision_log}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[safety-zones] shutting down")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hydra-umc-safety-zones")
    subparsers = parser.add_subparsers(dest="command")

    check_parser = subparsers.add_parser(
        "check", help="Check detected objects against zones and request E-STOP for Danger breaches."
    )
    check_parser.add_argument(
        "--tool-velocity",
        type=float,
        default=None,
        help="Head/tool speed in meters per second: grows the zones, as toolVelocityMps does in the API.",
    )
    check_parser.add_argument("--zones", required=True, help="Path to a zones JSON file.")
    check_parser.add_argument(
        "--detections", required=True, help="Path to a detected-objects JSON file."
    )
    check_parser.add_argument(
        "--observation",
        required=False,
        help="Path to an observation-status JSON file - real evidence the "
             "supplied detections were actually produced by an active, recently-"
             "updated observer. Omitting this always resolves to INHIBITED, never "
             "a silent READY - see evaluate_safety()'s own fail-safe default.",
    )

    obstacles_parser = subparsers.add_parser(
        "obstacles",
        help="Export the zones as sphere obstacles (the scenario format of a path planner) that cover every zone completely.",
    )
    obstacles_parser.add_argument("--zones", required=True, help="Path to a zones JSON file.")
    obstacles_parser.add_argument("--level", choices=LEVEL_CHOICES, default="all", help="which zones to export (default: all)")
    obstacles_parser.add_argument("--cell", type=float, default=DEFAULT_CELL_M, help="side in meters of the cubes each zone is cut into (default: 0.5)")
    obstacles_parser.add_argument("--tool-velocity", type=float, default=None, help="grow the zones for this tool speed in m/s, as check does")
    obstacles_parser.add_argument("--output", default="", help="write the JSON to this file instead of standard output")

    serve_parser = subparsers.add_parser(
        "serve",
        help="Run 'check' as a JSON/HTTP API (POST /check) - the exact same "
             "evaluate_safety()/check_breaches()/request_estop_for() functions the "
             "CLI already runs, with zones/detections in the JSON body instead of a "
             "server-side file path.",
    )
    serve_parser.add_argument("--addr", default="127.0.0.1", help="address to bind the HTTP API to")
    serve_parser.add_argument("--port", type=int, default=8108, help="port for the HTTP API")
    serve_parser.add_argument(
        "--zones-file",
        default=os.environ.get("SAFETY_ZONES_FILE", ""),
        help="zones JSON (with its calibration) that the server holds fixed; a request cannot bring its own zones",
    )
    serve_parser.add_argument(
        "--token",
        default=os.environ.get("SAFETY_ZONES_TOKEN", ""),
        help="bearer token POST /check requires (default: the SAFETY_ZONES_TOKEN environment variable)",
    )
    serve_parser.add_argument(
        "--max-observation-age-seconds",
        type=float,
        default=None,
        help="server ceiling for a request's observation.maxAgeSeconds: a larger one is refused",
    )
    serve_parser.add_argument(
        "--max-calibration-age-days",
        type=int,
        default=None,
        help="server ceiling for the calibration's max_age_days: a larger one is refused",
    )

    serve_parser.add_argument(
        "--decision-log",
        default=os.environ.get("SAFETY_ZONES_DECISION_LOG", ""),
        help="log every verdict of POST /check as one JSON line to this file, or to standard error with '-' (default: off)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        velocity = None
        if args.tool_velocity is not None:
            try:
                velocity = parse_tool_velocity(args.tool_velocity)
            except ConfigError as exc:
                print(f"SAFETY STATE: INHIBITED - invalid safety configuration: {exc}")
                return EXIT_INVALID_INPUT
        return _run_check(args.zones, args.detections, args.observation, velocity)
    if args.command == "serve":
        return _run_serve(args)
    if args.command == "obstacles":
        return _run_obstacles(args)

    # Bare invocation: unchanged identity/version/role report.
    print(f"{PROJECT_NAME} v{get_version()}")
    print(ROLE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
