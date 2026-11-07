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

from .health_route import health_open_requested
from .hold import HoldRegistry
from .server_identity import identity_key_from
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from hydra_umc_safety_zones.api import SafetyZonesServer
from hydra_umc_safety_zones.config import (
    ConfigError,
    load_detections,
    load_observation_status,
    load_zone_set,
    parse_robot_body,
    parse_tool_velocity,
    strict_json_loads,
)
from hydra_umc_safety_zones.estop import NullEStopRequester, request_estop_for
from hydra_umc_safety_zones.can_estop import (
    CanConfigError,
    CanEStopError,
    CanEStopRequester,
    HeartbeatSender,
    load_config as load_can_config,
    open_bus,
    state_is_safe,
)
from hydra_umc_safety_zones.signing import (
    Ed25519StateSigner,
    Signer,
    SigningError,
    StateSigner,
    generate_keypair,
    load_key_file,
    load_private_key_file,
)
from hydra_umc_safety_zones.obstacles import DEFAULT_CELL_M, LEVEL_CHOICES, export_obstacles
from hydra_umc_safety_zones.fk_input import FkInputError, body_from_fk
from hydra_umc_safety_zones.safety_state import SafetyState, collect_breaches, evaluate_safety
from hydra_umc_safety_zones.stateful import StatefulSettings, parse_stateful_settings
from hydra_umc_safety_zones.armor_alarm import ArmorAlarmConfigError, ArmorAlarmGate, parse_settings as parse_armor_alarm_settings
from hydra_umc_safety_zones.armor_alarm_mqtt import ArmorAlarmSubscriber
from hydra_umc_safety_zones.record_trigger import RecordTriggerClient, RecordTriggerConfigError, build_client as build_record_trigger, parse_record_trigger_settings
from hydra_umc_safety_zones.zone_occupancy import ZoneOccupancyConfigError, ZoneOccupancyEmitter, build_publisher as build_occupancy_publisher, parse_zone_occupancy_settings
from hydra_umc_safety_zones.fault_events import FaultEventEmitter, FaultEventsConfigError, MqttFaultPublisher, parse_fault_event_settings

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
    body_path: str | None = None,
) -> int:
    try:
        zone_set = load_zone_set(zones_path)
        objects = load_detections(detections_path)
        # A body file with the feature switched off is refused, never ignored (parse_robot_body raises).
        robot_body = (
            parse_robot_body(strict_json_loads(Path(body_path).read_text(encoding="utf-8")), zone_set.robot_body) if body_path else None
        )
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
        evaluation = evaluate_safety(zone_set, objects, today, observation, tool_velocity_mps=tool_velocity_mps, body=robot_body)
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

    breaches = collect_breaches(zone_set, objects, tool_velocity_mps, robot_body)
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


def _run_body_from_fk(args: argparse.Namespace) -> int:
    """Turns the output of 'fk --json' into a robot-body file for 'check --body'. Nothing is run; two files are read."""
    try:
        fk = strict_json_loads(Path(args.fk).read_text(encoding="utf-8"))
        segments = strict_json_loads(Path(args.segments).read_text(encoding="utf-8"))
        body = body_from_fk(fk, segments)
    except (FkInputError, OSError, ValueError) as exc:
        print(f"body-from-fk: could not build the robot body: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INVALID_INPUT
    text = json.dumps(body, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(f"body-from-fk: {len(body['segments'])} segments written to {args.output}")
    else:
        print(text)
    return 0


def _stateful_enabled_in(zones_file: str) -> bool:
    """True when the zones file switches the stateful mode on; a file that cannot be read says nothing here (loading it fails on its own)."""
    try:
        data = json.loads(Path(zones_file).read_text(encoding="utf-8"))
        return isinstance(data, dict) and isinstance(data.get("stateful"), dict) and data["stateful"].get("enabled") is True
    except (OSError, ValueError):
        return False


def _stateful_settings(zones_file: str) -> StatefulSettings:
    """The tunable values of the stateful mode: from the ``stateful`` object of the zones file, with the mode switched on."""
    data = None
    if zones_file:
        raw = json.loads(Path(zones_file).read_text(encoding="utf-8"))
        data = raw.get("stateful") if isinstance(raw, dict) else None
    settings = parse_stateful_settings(data)
    return StatefulSettings(
        enabled=True,
        heartbeat_ttl_s=settings.heartbeat_ttl_s,
        state_ttl_s=settings.state_ttl_s,
        max_subscribers=settings.max_subscribers,
        keepalive_s=settings.keepalive_s,
    )


def _armor_audit_writer(destination: str):
    """The audit sink of the reading of the ARMOR alarm: one JSON line per record to a file, or to standard error with '-'; None keeps them in memory only."""
    if not destination:
        return None

    def write(record: dict) -> None:
        line = json.dumps(record, sort_keys=True)
        if destination == "-":
            print(line, file=sys.stderr, flush=True)
        else:
            with open(destination, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    return write


def _start_armor_alarm(zones_file: str) -> tuple[ArmorAlarmGate, ArmorAlarmSubscriber] | None:
    """The gate and the (not yet started) subscriber when the zones file switches ``armorAlarm.enabled`` on; None when it is off or absent."""
    if not zones_file:
        return None
    raw = json.loads(Path(zones_file).read_text(encoding="utf-8"))
    settings = parse_armor_alarm_settings(raw.get("armorAlarm") if isinstance(raw, dict) else None)
    if not settings.enabled:
        return None
    gate = ArmorAlarmGate(settings, _armor_audit_writer(settings.audit_log))
    return gate, ArmorAlarmSubscriber(gate)


def _start_fault_events(zones_file: str) -> tuple[FaultEventEmitter, MqttFaultPublisher] | None:
    """The emitter and the (not yet started) publisher when the zones file switches ``faultEvents.enabled`` on; None when it is off or absent."""
    if not zones_file:
        return None
    raw = json.loads(Path(zones_file).read_text(encoding="utf-8"))
    settings = parse_fault_event_settings(raw.get("faultEvents") if isinstance(raw, dict) else None)
    if not settings.enabled:
        return None
    publisher = MqttFaultPublisher(settings)
    return FaultEventEmitter(settings, publisher.publish, log=lambda message: print(message, file=sys.stderr, flush=True)), publisher


def _start_zone_occupancy(zones_file: str) -> tuple[ZoneOccupancyEmitter, MqttFaultPublisher] | None:
    """The emitter and the (not yet started) publisher when the zones file switches ``zoneOccupancy.enabled`` on; None when it is off or absent."""
    if not zones_file:
        return None
    raw = json.loads(Path(zones_file).read_text(encoding="utf-8"))
    settings = parse_zone_occupancy_settings(raw.get("zoneOccupancy") if isinstance(raw, dict) else None)
    if not settings.enabled:
        return None
    publisher = build_occupancy_publisher(settings)
    return ZoneOccupancyEmitter(settings, publisher.publish, log=lambda message: print(message, file=sys.stderr, flush=True)), publisher


def _start_record_trigger(zones_file: str) -> RecordTriggerClient | None:
    """The client when the zones file switches ``recordTrigger.enabled`` on; None when it is off or absent."""
    if not zones_file:
        return None
    raw = json.loads(Path(zones_file).read_text(encoding="utf-8"))
    settings = parse_record_trigger_settings(raw.get("recordTrigger") if isinstance(raw, dict) else None)
    if not settings.enabled:
        return None
    return build_record_trigger(settings, log=lambda message: print(message, file=sys.stderr, flush=True))


def _start_can(path: str):
    """Reads the CAN configuration and opens the bus: the requester, the heartbeat sender (not started) and the configuration. None-like when it is not enabled is an error: a file given is a file meant."""
    config = load_can_config(path)
    if not config.enabled:
        raise CanConfigError('the file says "enabled": false')
    bus = open_bus(config)
    requester = CanEStopRequester(bus, config)
    heartbeat = HeartbeatSender(bus, config, lambda: False)
    return requester, heartbeat, config


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
    stateful: StatefulSettings | None = None
    if args.stateful or (args.zones_file and _stateful_enabled_in(args.zones_file)):
        try:
            stateful = _stateful_settings(args.zones_file)
        except Exception as exc:  # noqa: BLE001
            print(f"[safety-zones] fatal: invalid stateful settings: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    signer: Signer | None = None
    if args.state_key_file:
        try:
            if args.state_signing_alg == "ed25519":
                signer = Ed25519StateSigner(load_private_key_file(args.state_key_file), args.state_key_id)
            else:
                signer = StateSigner(load_key_file(args.state_key_file), args.state_key_id)
        except SigningError as exc:
            print(f"[safety-zones] fatal: --state-key-file: {exc}", file=sys.stderr)
            return 2
    try:
        identity_key = identity_key_from(args.identity_key_file)
    except ValueError as exc:
        print(f"[safety-zones] fatal: --identity-key-file: {exc}", file=sys.stderr)
        return 2
    armor_runtime = None
    try:
        armor_runtime = _start_armor_alarm(args.zones_file)
    except (ArmorAlarmConfigError, OSError, ValueError) as exc:
        print(f"[safety-zones] fatal: invalid armorAlarm settings: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    fault_runtime = None
    try:
        fault_runtime = _start_fault_events(args.zones_file)
    except (FaultEventsConfigError, OSError, ValueError) as exc:
        print(f"[safety-zones] fatal: invalid faultEvents settings: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    occupancy_runtime = None
    try:
        occupancy_runtime = _start_zone_occupancy(args.zones_file)
    except (ZoneOccupancyConfigError, OSError, ValueError) as exc:
        print(f"[safety-zones] fatal: invalid zoneOccupancy settings: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    trigger_client = None
    try:
        trigger_client = _start_record_trigger(args.zones_file)
    except (RecordTriggerConfigError, OSError, ValueError) as exc:
        print(f"[safety-zones] fatal: invalid recordTrigger settings: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    can_runtime = None
    if args.can_estop_config:
        try:
            can_runtime = _start_can(args.can_estop_config)
        except (CanConfigError, CanEStopError) as exc:
            print(f"[safety-zones] fatal: --can-estop-config: {exc}", file=sys.stderr)
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
            stateful=stateful,
            signer=signer,
            estop_requester=can_runtime[0] if can_runtime else None,
            armor_gate=armor_runtime[0] if armor_runtime else None,
        )
    except OSError as exc:
        print(f"[safety-zones] fatal: could not start the HTTP API on {addr}:{port}: {exc}", file=sys.stderr)
        return 2
    if args.hold:
        if args.hold_actuate and can_runtime is None:
            print("[safety-zones] fatal: --hold-actuate needs --can-estop-config (a hold is handed to the same requester as a breach)", file=sys.stderr)
            server.server_close()
            return 2
        server.holds = HoldRegistry()
        server.hold_actuate = bool(args.hold_actuate)
        print(f"[safety-zones] holds on: POST /hold records a hold{' and requests a stop' if server.hold_actuate else ' (recorded only; --hold-actuate would also request a stop)'}")
    if can_runtime is not None:
        _, heartbeat, can_config = can_runtime
        heartbeat.is_safe = lambda: state_is_safe(server.last_verdict(), can_config.state_max_age_ms)
        heartbeat.start()
        print(
            f"[safety-zones] CAN: stop requests on id {can_config.estop_id:#x}, safe heartbeat on id {can_config.heartbeat_id:#x} every "
            f"{can_config.heartbeat_period_ms} ms only while the last verdict is READY (not tried on a real bus)"
        )
    if armor_runtime is not None:
        gate, subscriber = armor_runtime
        try:
            subscriber.start()
        except (ArmorAlarmConfigError, OSError) as exc:
            print(f"[safety-zones] fatal: the reading of the ARMOR alarm could not start: {exc}", file=sys.stderr)
            server.server_close()
            return 2
        broker = gate.settings.broker
        print(
            f"[safety-zones] ARMOR alarm: reading {gate.settings.topic} from {broker.host}:{broker.port}; a sounding alarm INHIBITS new starts and never stops a machine"
            + (" except by the safe stop that the installation switched on" if gate.settings.safe_stop else "")
            + " (not tried with a real alarm)"
        )
    if fault_runtime is not None:
        emitter, fault_publisher = fault_runtime
        try:
            fault_publisher.start()
        except (FaultEventsConfigError, OSError) as exc:
            print(f"[safety-zones] fatal: the fault events could not start: {exc}", file=sys.stderr)
            server.server_close()
            return 2
        server.fault_emitter = emitter
        print(f"[safety-zones] fault events: a change of the safety state is published as fault-event/1 on {emitter.settings.topic} (not tried with a real broker)")
    if occupancy_runtime is not None:
        occupancy_emitter, occupancy_publisher = occupancy_runtime
        try:
            occupancy_publisher.start()
        except (FaultEventsConfigError, OSError) as exc:
            print(f"[safety-zones] fatal: the zone occupancy could not start: {exc}", file=sys.stderr)
            server.server_close()
            return 2
        server.occupancy_emitter = occupancy_emitter
        print(f"[safety-zones] zone occupancy: the zones with something inside are published on {occupancy_emitter.settings.topic} (not tried with a real broker)")
    if trigger_client is not None:
        server.record_trigger = trigger_client
        print(f"[safety-zones] record trigger: a safe stop asks {len(trigger_client.settings.targets)} camera(s) to keep their frames (not tried with a real camera)")
    if signer is not None:
        print(f"[safety-zones] every verdict is signed ({signer.alg}, key id {signer.key_id})")
    print(f"[safety-zones] HTTP API listening on {addr}:{port}")
    print("[safety-zones] POST /check, GET /stats" + (", POST /heartbeat, GET /events" if stateful is not None else ""))
    if stateful is not None:
        print(
            f"[safety-zones] STATEFUL: the observer proves it is alive with POST /heartbeat (valid {stateful.heartbeat_ttl_s}s); "
            f"a verdict is valid {stateful.state_ttl_s}s and is pushed on GET /events"
        )
    if zone_set is not None:
        print(f"[safety-zones] zones and calibration fixed by {args.zones_file} (sha256 {config_sha256})")
    else:
        print("[safety-zones] WARNING: zones and calibration come from each request: whoever reaches this port decides them (use --zones-file)")
    if args.token:
        print("[safety-zones] POST /check requires a bearer token")
    if args.decision_log:
        print(f"[safety-zones] every verdict is logged as a JSON line to {'standard error' if args.decision_log == '-' else args.decision_log}")
    server.identity_key = identity_key
    if identity_key is not None:
        print("[safety-zones] tokens of HYDRA-UMC-SERVER accepted: every route needs an identity, and a viewer may read but not change")
    server.start_watcher()
    try:
        server.health_open = health_open_requested(args.health_open)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if armor_runtime is not None:
            armor_runtime[1].stop()
        if fault_runtime is not None:
            fault_runtime[1].stop()
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

    check_parser.add_argument(
        "--body",
        required=False,
        help="Path to a robot-body JSON file (the segments of the robot with their declared radii, in the frame of the zones). "
             "Refused with INHIBITED unless the zones file switches geometry.robot_body on.",
    )

    fk_parser = subparsers.add_parser(
        "body-from-fk",
        help="Build a robot-body file from the JSON of 'hydra-umc-physics-replica fk --json' and a file that names the segments and declares their radii.",
    )
    fk_parser.add_argument("--fk", required=True, help="Path to the JSON that 'fk --json' printed (an array of joint poses).")
    fk_parser.add_argument("--segments", required=True, help="Path to the segments file: segments between named joint frames, with their radii.")
    fk_parser.add_argument("--output", default="", help="write the JSON to this file instead of standard output")

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
    serve_parser.add_argument("--identity-key-file", default=None, help="file with the signing key of HYDRA-UMC-SERVER (its JWT_SECRET): a valid token of the SERVER is then accepted, every route (GET /health and GET /stats too) demands an identity and a viewer may read but not change (off by default; or set HYDRA_UMC_SAFETY_ZONES_IDENTITY_KEY_FILE)")
    serve_parser.add_argument("--health-open", action="store_true", help="answer GET /health (open to loopback, to other callers only with the bearer token); closed by default, or set HYDRA_UMC_SAFETY_ZONES_HEALTH_OPEN=1")
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

    serve_parser.add_argument(
        "--state-key-file",
        default=os.environ.get("SAFETY_ZONES_STATE_KEY_FILE", ""),
        help="a file with the shared key (at least 32 characters) that signs every verdict (sdkSafetyStateSignature); off by default, "
             "because a consumer must verify before the service starts signing",
    )
    serve_parser.add_argument(
        "--state-signing-alg",
        choices=("hmac", "ed25519"),
        default=os.environ.get("SAFETY_ZONES_STATE_SIGNING_ALG", "hmac"),
        help="how verdicts are signed when --state-key-file is given: hmac (a shared key, the default) or ed25519 (--state-key-file is then a PEM private key "
             "kept outside the repository, and a consumer needs only the public key; needs the cryptography package)",
    )
    serve_parser.add_argument(
        "--state-key-id",
        default=os.environ.get("SAFETY_ZONES_STATE_KEY_ID", "default"),
        help="the id of the signing key, carried in the signature (default: default)",
    )
    serve_parser.add_argument(
        "--can-estop-config",
        default=os.environ.get("SAFETY_ZONES_CAN_ESTOP_CONFIG", ""),
        help="a JSON file (identifiers, channel, heartbeat period) that turns on the E-STOP request and the safe heartbeat over CAN with python-can; "
             "off by default, a stop is then only recorded",
    )
    serve_parser.add_argument(
        "--hold",
        action="store_true",
        default=os.environ.get("SAFETY_ZONES_HOLD", "") in ("1", "true", "yes"),
        help="POST /hold lets a caller with the identity of the server (or the token) hold one robot and GET /holds lists them; the hold is only recorded "
             "(off by default, the routes answer 404)",
    )
    serve_parser.add_argument(
        "--hold-actuate",
        action="store_true",
        default=os.environ.get("SAFETY_ZONES_HOLD_ACTUATE", "") in ("1", "true", "yes"),
        help="with --hold, a hold also hands a stop request to the CAN requester (needs --can-estop-config; off by default, a hold is then only recorded)",
    )
    serve_parser.add_argument(
        "--stateful",
        action="store_true",
        default=os.environ.get("SAFETY_ZONES_STATEFUL", "") in ("1", "true", "yes"),
        help="stateful mode: the observer sends POST /heartbeat, POST /check takes its observation from it and GET /events pushes the state "
             "(the tuning values are in the 'stateful' object of the zones file; off by default)",
    )

    keygen_parser = subparsers.add_parser(
        "state-keygen",
        help="Create an Ed25519 key pair for signing verdicts: the private key (owner-only, never printed) and the public key to give to the consumers. Never overwrites a file.",
    )
    keygen_parser.add_argument("--private-out", required=True, help="where the private key (PEM) is written; keep it outside every repository")
    keygen_parser.add_argument("--public-out", required=True, help="where the public key (PEM) is written")

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
        return _run_check(args.zones, args.detections, args.observation, velocity, args.body)
    if args.command == "serve":
        return _run_serve(args)
    if args.command == "state-keygen":
        try:
            generate_keypair(args.private_out, args.public_out)
        except SigningError as exc:
            print(f"[safety-zones] fatal: {exc}", file=sys.stderr)
            return 2
        print(f"[safety-zones] Ed25519 key pair written: private key {args.private_out} (owner only), public key {args.public_out}")
        return 0
    if args.command == "body-from-fk":
        return _run_body_from_fk(args)
    if args.command == "obstacles":
        return _run_obstacles(args)

    # Bare invocation: unchanged identity/version/role report.
    print(f"{PROJECT_NAME} v{get_version()}")
    print(ROLE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
