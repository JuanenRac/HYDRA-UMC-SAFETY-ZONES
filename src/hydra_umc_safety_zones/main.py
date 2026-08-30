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
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version

from hydra_umc_safety_zones.breach import check_breaches
from hydra_umc_safety_zones.config import ConfigError, load_detections, load_zone_set
from hydra_umc_safety_zones.estop import NullEStopRequester, request_estop_for
from hydra_umc_safety_zones.safety_state import SafetyState, evaluate_safety

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


def _run_check(zones_path: str, detections_path: str) -> int:
    try:
        zone_set = load_zone_set(zones_path)
        objects = load_detections(detections_path)
    except ConfigError as exc:
        # Invalid spatial data is as untrustworthy as stale calibration: never
        # fall through to READY or evaluate a boundary with NaN coordinates.
        print(f"SAFETY STATE: INHIBITED - invalid safety configuration: {exc}")
        return 3
    today = datetime.now(timezone.utc).date()

    evaluation = evaluate_safety(zone_set, objects, today)
    print(f"SAFETY STATE: {evaluation.state.value.upper()} - {evaluation.reason}")

    if evaluation.state is SafetyState.INHIBITED:
        # Fail-safe: geometry cannot be trusted, so no breach logic below
        # runs at all - a stale/missing calibration must never be silently
        # treated as "no breach".
        return 3

    if evaluation.state is SafetyState.READY:
        return 0

    breaches = check_breaches(zone_set.zones, objects)
    for b in breaches:
        print(f"BREACH: object '{b.object_id}' inside {b.level.value} zone '{b.zone_id}'")

    if evaluation.state is SafetyState.WARNING:
        return 1

    requester = NullEStopRequester()
    requests = request_estop_for(breaches, requester)
    for r in requests:
        print(f"E-STOP REQUESTED: {r.reason} (not asserted - see estop.py)")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hydra-umc-safety-zones")
    subparsers = parser.add_subparsers(dest="command")

    check_parser = subparsers.add_parser(
        "check", help="Check detected objects against zones and request E-STOP for Danger breaches."
    )
    check_parser.add_argument("--zones", required=True, help="Path to a zones JSON file.")
    check_parser.add_argument(
        "--detections", required=True, help="Path to a detected-objects JSON file."
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        return _run_check(args.zones, args.detections)

    # Bare invocation: unchanged identity/version/role report.
    print(f"{PROJECT_NAME} v{get_version()}")
    print(ROLE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
