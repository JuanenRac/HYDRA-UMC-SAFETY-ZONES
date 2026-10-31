# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_characterization_verdicts.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Characterization of the verdicts, the reasons and the margins.

These tests pin what the service answers today for a request that carries only
detections: the state, the reason text, the breach list, the growth of the zones
for the velocity and the separation model, the verdict with shape zones and the
exact keys of a /check answer. Anything added later (a robot body, for example)
must leave every one of them unchanged when it is not used."""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from hydra_umc_safety_zones.breach import DetectedObject, check_breaches
from hydra_umc_safety_zones.api import SafetyZonesServer
from hydra_umc_safety_zones.config import parse_zone_set
from hydra_umc_safety_zones.geometry import Point3D
from hydra_umc_safety_zones.main import main
from hydra_umc_safety_zones.observation import ObservationStatus
from hydra_umc_safety_zones.safety_state import SafetyState, evaluate_safety
from hydra_umc_safety_zones.zones import scale_zones_for_velocity

TODAY = date(2030, 1, 10)
NOW = datetime(2030, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
CAL = {"version": "c1", "source": "manual", "calibrated_at": "2030-01-01", "max_age_days": 30}
OBS = ObservationStatus(active=True, observed_at=NOW, max_age_seconds=60.0)

ZONES = {
    "geometry": {"shapes": True},
    "zones": [
        {"id": "box-danger", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 2, "y": 2, "z": 2}},
        {"id": "cap-warn", "level": "warning", "shape": "capsule", "start": {"x": 5, "y": 0, "z": 1}, "end": {"x": 7, "y": 0, "z": 1}, "radius": 0.5},
        {"id": "cyl-warn", "level": "warning", "shape": "cylinder", "base_center": {"x": 0, "y": 6, "z": 0}, "radius": 1.0, "height": 2.0},
        {
            "id": "obb-warn",
            "level": "warning",
            "shape": "obb",
            "center": {"x": 10, "y": 0, "z": 1},
            "half_extents": {"x": 1, "y": 0.5, "z": 1},
            "rotation_deg": {"yaw": 90},
        },
    ],
    "calibration": CAL,
}


def _verdict(x: float, y: float, z: float, radius: float = 0.0, velocity: float | None = None, zones: dict | None = None) -> SafetyState:
    zone_set = parse_zone_set(zones or ZONES)
    obj = (DetectedObject("p", Point3D(x, y, z), radius),)
    return evaluate_safety(zone_set, obj, TODAY, OBS, now=NOW, tool_velocity_mps=velocity).state


def test_ready_reason_and_state_are_pinned() -> None:
    evaluation = evaluate_safety(parse_zone_set(ZONES), (), TODAY, OBS, now=NOW)
    assert evaluation.state is SafetyState.READY
    assert evaluation.reason == "no breach, calibration valid"
    assert evaluation.fault is False


def test_danger_and_warning_reasons_are_pinned() -> None:
    zone_set = parse_zone_set(ZONES)
    danger = evaluate_safety(zone_set, (DetectedObject("p", Point3D(1, 1, 1)),), TODAY, OBS, now=NOW)
    assert (danger.state, danger.reason) == (SafetyState.DANGER, "object(s) ['p'] breached a danger zone")
    warning = evaluate_safety(zone_set, (DetectedObject("q", Point3D(6, 0, 1)),), TODAY, OBS, now=NOW)
    assert (warning.state, warning.reason) == (SafetyState.WARNING, "object(s) ['q'] breached a warning zone")


def test_inhibited_reasons_are_pinned() -> None:
    zone_set = parse_zone_set(ZONES)
    no_observation = evaluate_safety(zone_set, (), TODAY, None, now=NOW)
    assert no_observation.state is SafetyState.INHIBITED
    assert no_observation.reason.startswith("no observation status provided")
    no_calibration = evaluate_safety(parse_zone_set({"zones": ZONES["zones"], "geometry": {"shapes": True}}), (), TODAY, OBS, now=NOW)
    assert no_calibration.state is SafetyState.INHIBITED
    assert no_calibration.reason == "no calibration metadata present - zone geometry cannot be trusted"


def test_the_capsule_cylinder_and_oriented_box_verdicts_at_and_around_their_edges() -> None:
    # capsule: radius 0.5 around the segment (5,0,1)-(7,0,1)
    assert _verdict(6, 0.5, 1) is SafetyState.WARNING
    assert _verdict(6, 0.51, 1) is SafetyState.READY
    assert _verdict(4.5, 0, 1) is SafetyState.WARNING
    assert _verdict(4.49, 0, 1) is SafetyState.READY
    # cylinder: base (0,6,0), radius 1, height 2
    assert _verdict(1, 6, 1) is SafetyState.WARNING
    assert _verdict(1.01, 6, 1) is SafetyState.READY
    assert _verdict(0, 6, 2) is SafetyState.WARNING
    assert _verdict(0, 6, 2.01) is SafetyState.READY
    # oriented box turned 90 degrees about Z: its 1 m half extent along x now points along y
    assert _verdict(10, 1.1, 1) is SafetyState.READY
    assert _verdict(10, 0.9, 1) is SafetyState.WARNING
    assert _verdict(10.6, 0, 1) is SafetyState.READY
    assert _verdict(10.4, 0, 1) is SafetyState.WARNING


def test_the_radius_of_a_detection_reaches_into_shapes() -> None:
    assert _verdict(6, 0.9, 1, radius=0.0) is SafetyState.READY
    assert _verdict(6, 0.9, 1, radius=0.4) is SafetyState.WARNING


def test_the_velocity_margin_is_linear_and_capped() -> None:
    # the box ends at x = 2: 0.15 m per m/s, at most 0.5 m
    assert _verdict(2.14, 1, 1, velocity=1.0) is SafetyState.DANGER
    assert _verdict(2.16, 1, 1, velocity=1.0) is SafetyState.READY
    assert _verdict(2.49, 1, 1, velocity=100.0) is SafetyState.DANGER
    assert _verdict(2.51, 1, 1, velocity=100.0) is SafetyState.READY
    # no velocity: the static zone
    assert _verdict(2.01, 1, 1) is SafetyState.READY


def test_the_separation_model_grows_the_zones_even_without_a_velocity() -> None:
    zones = dict(ZONES, separation={"approach_speed_mps": 1.6, "total_stop_time_s": 0.5, "intrusion_distance_m": 0.2})
    # K*T + C = 1.0 m
    assert _verdict(2.99, 1, 1, zones=zones) is SafetyState.DANGER
    assert _verdict(3.01, 1, 1, zones=zones) is SafetyState.READY


def test_the_breach_list_follows_the_scaled_zones() -> None:
    zone_set = parse_zone_set(ZONES)
    objects = (DetectedObject("p", Point3D(2.1, 1, 1)),)
    assert check_breaches(zone_set.zones, objects) == ()
    scaled = scale_zones_for_velocity(zone_set.zones, 1.0, zone_set.separation)
    breaches = check_breaches(scaled, objects)
    assert [(b.zone_id, b.level.value, b.object_id) for b in breaches] == [("box-danger", "danger", "p")]


def _serve():
    server = SafetyZonesServer(("127.0.0.1", 0), "test role")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _post(url: str, body: dict) -> tuple[int, dict]:
    import urllib.error

    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_the_keys_of_a_check_answer_are_pinned() -> None:
    server, thread = _serve()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        zones = dict(ZONES, calibration=dict(CAL, calibrated_at=datetime.now(timezone.utc).date().isoformat()))
        observation = {"active": True, "observedAt": datetime.now(timezone.utc).isoformat(), "maxAgeSeconds": 3600, "error": None}
        objects = {"objects": [{"id": "p", "position": {"x": 1, "y": 1, "z": 1}}]}
        status, body = _post(f"{base}/check", {"zones": zones, "detections": objects, "observation": observation})
        assert status == 200
        assert sorted(body) == ["breaches", "estopRequests", "reason", "sdkSafetyState", "state"]
        assert body["state"] == "danger"
        assert [b["zone_id"] for b in body["breaches"]] == ["box-danger"]
        assert sorted(body["breaches"][0]) == ["level", "object_id", "position", "zone_id"]
        assert [r["object_id"] for r in body["estopRequests"]] == ["p"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_the_check_command_prints_and_exits_as_before(tmp_path: Path, capsys) -> None:
    zones = tmp_path / "zones.json"
    zones.write_text(json.dumps(dict(ZONES, calibration=dict(CAL, calibrated_at=datetime.now(timezone.utc).date().isoformat()))), encoding="utf-8")
    detections = tmp_path / "detections.json"
    detections.write_text(json.dumps({"objects": [{"id": "p", "position": {"x": 1, "y": 1, "z": 1}}]}), encoding="utf-8")
    observation = tmp_path / "observation.json"
    observation.write_text(json.dumps({"active": True, "observedAt": datetime.now(timezone.utc).isoformat(), "maxAgeSeconds": 3600, "error": None}), encoding="utf-8")
    code = main(["check", "--zones", str(zones), "--detections", str(detections), "--observation", str(observation)])
    out = capsys.readouterr().out.splitlines()
    assert code == 2
    assert out[0] == "SAFETY STATE: DANGER - object(s) ['p'] breached a danger zone"
    assert out[1] == "BREACH: object 'p' inside danger zone 'box-danger'"
    assert out[2].startswith("E-STOP REQUESTED: object 'p' breached danger zone 'box-danger'")
