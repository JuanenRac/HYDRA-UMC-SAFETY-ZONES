# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_robot_body.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The robot body as capsules: the switch, the refusals, the verdicts, the API and the commands."""
from __future__ import annotations

import copy
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hydra_umc_safety_zones.api import SafetyZonesServer
from hydra_umc_safety_zones.config import ConfigError, parse_robot_body, parse_zone_set
from hydra_umc_safety_zones.fk_input import FkInputError, body_from_fk
from hydra_umc_safety_zones.main import main
from hydra_umc_safety_zones.observation import ObservationStatus
from hydra_umc_safety_zones.robot_body import RobotBodyDisabled, RobotBodyError
from hydra_umc_safety_zones.safety_state import SafetyState, evaluate_safety

NOW = datetime.now(timezone.utc)
TODAY = NOW.date()
OBS = ObservationStatus(active=True, observed_at=NOW, max_age_seconds=3600.0)


def _zones(robot_body: object = True, **extra: object) -> dict:
    data: dict = {
        "geometry": {"shapes": True, "robot_body": robot_body},
        "zones": [
            {"id": "fence", "level": "danger", "min": {"x": 2, "y": -1, "z": 0}, "max": {"x": 3, "y": 1, "z": 2}},
            {"id": "aisle", "level": "warning", "shape": "cylinder", "base_center": {"x": 0, "y": 4, "z": 0}, "radius": 0.5, "height": 2},
        ],
        "calibration": {"version": "c", "source": "manual", "calibrated_at": TODAY.isoformat(), "max_age_days": 30},
    }
    if robot_body is None:
        del data["geometry"]["robot_body"]  # the key is absent: the default
    data.update(extra)
    return data


def _body(end_x: float = 1.0, radius: float = 0.1) -> dict:
    return {"segments": [{"id": "forearm", "start": {"x": 0, "y": 0, "z": 1}, "end": {"x": end_x, "y": 0, "z": 1}, "radius": radius}]}


def _state(zones: dict, body: dict | None, **kwargs) -> SafetyState:
    zone_set = parse_zone_set(zones)
    parsed = parse_robot_body(body, zone_set.robot_body) if body is not None else None
    return evaluate_safety(zone_set, (), TODAY, OBS, body=parsed, **kwargs).state


def test_a_body_that_stays_clear_is_ready_and_one_that_reaches_a_danger_zone_is_danger() -> None:
    assert _state(_zones(), _body(end_x=1.0)) is SafetyState.READY
    assert _state(_zones(), _body(end_x=2.5)) is SafetyState.DANGER


def test_the_declared_radius_decides_the_edge_and_it_only_errs_on_the_safe_side() -> None:
    # the fence starts at x = 2: the capsule ends at x = 1.85 with radius 0.1 -> 0.05 m of air, a coarse cut adds at most half a piece
    assert _state(_zones(), _body(end_x=1.85, radius=0.1)) is SafetyState.READY
    assert _state(_zones(), _body(end_x=1.85, radius=0.16)) is SafetyState.DANGER
    # a thicker radius never turns a breach into READY
    assert _state(_zones(), _body(end_x=1.95, radius=0.1)) is SafetyState.DANGER


def test_the_breach_names_the_part_of_the_robot_and_the_stop_request_follows() -> None:
    from hydra_umc_safety_zones.estop import NullEStopRequester, request_estop_for
    from hydra_umc_safety_zones.safety_state import collect_breaches

    zone_set = parse_zone_set(_zones())
    body = parse_robot_body(_body(end_x=2.5), zone_set.robot_body)
    breaches = collect_breaches(zone_set, (), None, body)
    assert [(b.zone_id, b.object_id) for b in breaches] == [("fence", "robot:forearm")]
    requests = request_estop_for(breaches, NullEStopRequester())
    assert [r.object_id for r in requests] == ["robot:forearm"]


def test_the_frame_moves_the_poses_into_the_frame_of_the_zones() -> None:
    body = _body(end_x=1.0)
    body["frame"] = {"translation": {"x": 2.0, "y": 0, "z": 0}}
    assert _state(_zones(), body) is SafetyState.DANGER
    turned = _body(end_x=1.0)
    turned["frame"] = {"rotation_deg": {"yaw": 90}}  # the segment now points along +y, towards the aisle cylinder only at y = 4
    assert _state(_zones(), turned) is SafetyState.READY
    long_turned = _body(end_x=4.0)
    long_turned["frame"] = {"rotation_deg": {"yaw": 90}}
    long_turned["segments"][0]["start"] = {"x": 0, "y": 0, "z": 1}
    assert _state(_zones(), long_turned) is SafetyState.WARNING


def test_the_velocity_and_separation_margins_grow_the_zones_for_the_body_too() -> None:
    assert _state(_zones(), _body(end_x=1.8, radius=0.1)) is SafetyState.READY
    assert _state(_zones(), _body(end_x=1.8, radius=0.1), tool_velocity_mps=2.0) is SafetyState.DANGER
    with_model = _zones(separation={"approach_speed_mps": 1.6, "total_stop_time_s": 0.5, "intrusion_distance_m": 0.2})
    assert _state(with_model, _body(end_x=0.95, radius=0.1)) is SafetyState.DANGER


def test_the_margin_of_the_settings_grows_every_segment() -> None:
    plain = _zones({"enabled": True})
    wide = _zones({"enabled": True, "margin_m": 0.3})
    assert _state(plain, _body(end_x=1.7, radius=0.1)) is SafetyState.READY
    assert _state(wide, _body(end_x=1.7, radius=0.1)) is SafetyState.DANGER


def test_a_cell_without_the_switch_refuses_a_body_and_the_default_is_off() -> None:
    for zones in (_zones(False), _zones(None), _zones({"enabled": False})):
        zone_set = parse_zone_set(zones)
        with pytest.raises(RobotBodyDisabled):
            parse_robot_body(_body(), zone_set.robot_body)
    no_geometry = _zones()
    del no_geometry["geometry"]
    no_geometry["zones"] = no_geometry["zones"][:1]  # shapes need the geometry object; a box file has none
    with pytest.raises(RobotBodyDisabled):
        parse_robot_body(_body(), parse_zone_set(no_geometry).robot_body)


def test_a_library_caller_that_builds_the_body_without_the_switch_is_inhibited() -> None:
    enabled = parse_zone_set(_zones())
    body = parse_robot_body(_body(), enabled.robot_body)
    off = parse_zone_set(_zones(False))
    evaluation = evaluate_safety(off, (), TODAY, OBS, body=body)
    assert evaluation.state is SafetyState.INHIBITED
    assert "robot body refused" in evaluation.reason


def test_required_makes_a_request_without_a_body_inhibited() -> None:
    zones = _zones({"enabled": True, "required": True})
    assert _state(zones, None) is SafetyState.INHIBITED
    assert _state(zones, _body()) is SafetyState.READY
    # not required: the previous behaviour, no body, no robot checks
    assert _state(_zones(), None) is SafetyState.READY


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(segments=[]),
        lambda b: b.update(segments="x"),
        lambda b: b.update(unknown=1),
        lambda b: b["segments"][0].pop("radius"),
        lambda b: b["segments"][0].update(radius=0),
        lambda b: b["segments"][0].update(radius=-0.1),
        lambda b: b["segments"][0].update(radius=True),
        lambda b: b["segments"][0].update(radius="0.1"),
        lambda b: b["segments"][0].update(radius=float("nan")),
        lambda b: b["segments"][0].update(radius=float("inf")),
        lambda b: b["segments"][0].update(extra=1),
        lambda b: b["segments"][0].update(id=""),
        lambda b: b["segments"][0].update(id=3),
        lambda b: b["segments"][0].update(start={"x": 0, "y": 0}),
        lambda b: b["segments"][0].update(start={"x": float("nan"), "y": 0, "z": 0}),
        lambda b: b["segments"][0].update(end={"x": 1e9, "y": 0, "z": 0}),
        lambda b: b["segments"][0].update(end="x"),
        lambda b: b.update(segments=b["segments"] * 2),
        lambda b: b.update(frame="x"),
        lambda b: b.update(frame={"scale": 2}),
        lambda b: b.update(frame={"translation": {"x": 1e9}}),
        lambda b: b.update(frame={"rotation_deg": {"yaw": float("inf")}}),
        lambda b: b.update(segments=[b["segments"][0], "x"]),
    ],
)
def test_a_malformed_body_is_refused_whole(mutate) -> None:
    body = copy.deepcopy(_body())
    mutate(body)
    zone_set = parse_zone_set(_zones())
    with pytest.raises(RobotBodyError):
        parse_robot_body(body, zone_set.robot_body)


def test_a_body_that_is_not_an_object_or_has_too_many_segments_is_refused() -> None:
    zone_set = parse_zone_set(_zones({"enabled": True, "max_segments": 2}))
    for raw in (None, [], "x", 3):
        with pytest.raises(RobotBodyError):
            parse_robot_body(raw, zone_set.robot_body)
    many = {"segments": [{"id": f"s{i}", "start": {"x": 0, "y": 0, "z": 0}, "end": {"x": 1, "y": 0, "z": 0}, "radius": 0.1} for i in range(3)]}
    with pytest.raises(RobotBodyError):
        parse_robot_body(many, zone_set.robot_body)


@pytest.mark.parametrize(
    "setting",
    [
        "yes",
        3,
        {"enabled": "yes"},
        {"enabled": True, "resolution_m": 0},
        {"enabled": True, "resolution_m": -1},
        {"enabled": True, "resolution_m": "0.1"},
        {"enabled": True, "margin_m": -0.1},
        {"enabled": True, "margin_m": True},
        {"enabled": True, "max_segments": 0},
        {"enabled": True, "max_segments": 1.5},
        {"enabled": True, "max_pieces_per_segment": 0},
        {"enabled": True, "size": 1},
        {"required": "no"},
    ],
)
def test_malformed_settings_make_the_zones_file_invalid(setting) -> None:
    with pytest.raises(ConfigError):
        parse_zone_set(_zones(setting))


def test_a_detection_that_uses_the_reserved_prefix_is_refused_when_a_body_is_given() -> None:
    from hydra_umc_safety_zones.breach import DetectedObject
    from hydra_umc_safety_zones.geometry import Point3D

    zone_set = parse_zone_set(_zones())
    body = parse_robot_body(_body(), zone_set.robot_body)
    evaluation = evaluate_safety(zone_set, (DetectedObject("robot:forearm", Point3D(50, 50, 50)),), TODAY, OBS, body=body)
    assert evaluation.state is SafetyState.INHIBITED


def test_a_long_segment_is_still_over_estimated_when_the_cut_is_capped() -> None:
    # one piece for 100 m: the sphere is 50 m wide, far more than the robot, never less
    zones = _zones({"enabled": True, "max_pieces_per_segment": 1})
    far = {"segments": [{"id": "s", "start": {"x": 0, "y": 0, "z": 1}, "end": {"x": 100, "y": 0, "z": 1}, "radius": 0.1}]}
    assert _state(zones, far) is SafetyState.DANGER


# --- over HTTP ---


def _serve(**kwargs):
    server = SafetyZonesServer(("127.0.0.1", 0), "test role", **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def _post(url: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _request(zones: dict, robot_body: object) -> dict:
    return {
        "zones": zones,
        "detections": {"objects": []},
        "observation": {"active": True, "observedAt": datetime.now(timezone.utc).isoformat(), "maxAgeSeconds": 3600, "error": None},
        "robotBody": robot_body,
    }


def test_check_over_http_with_a_body_gives_danger_and_names_the_segment() -> None:
    server, thread, base = _serve()
    try:
        status, answer = _post(f"{base}/check", _request(_zones(), _body(end_x=2.5)))
        assert status == 200
        assert answer["state"] == "danger"
        assert answer["breaches"][0]["object_id"] == "robot:forearm"
        assert answer["estopRequests"][0]["object_id"] == "robot:forearm"
        assert answer["robotBody"] == {"segments": 1}
        status, answer = _post(f"{base}/check", _request(_zones(), _body(end_x=1.0)))
        assert (status, answer["state"]) == (200, "ready")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_check_over_http_refuses_a_body_when_the_switch_is_off_with_inhibited() -> None:
    server, thread, base = _serve()
    try:
        for zones in (_zones(False), _zones(None)):
            status, answer = _post(f"{base}/check", _request(zones, _body(end_x=2.5)))
            assert status == 400
            assert answer["state"] == "inhibited"
            assert answer["sdkSafetyState"]["state"] == "INHIBITED"
            assert answer["breaches"] == [] and answer["estopRequests"] == []
        status, answer = _post(f"{base}/check", _request(_zones(), {"segments": []}))
        assert (status, answer["state"]) == (400, "inhibited")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_check_over_http_without_a_body_is_unchanged_even_when_the_switch_is_on() -> None:
    server, thread, base = _serve()
    try:
        request = _request(_zones(), None)
        del request["robotBody"]
        status, answer = _post(f"{base}/check", request)
        assert (status, answer["state"]) == (200, "ready")
        assert "robotBody" not in answer
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_a_server_that_holds_its_zones_uses_its_own_switch(tmp_path: Path) -> None:
    from hydra_umc_safety_zones.config import load_zone_set

    path = tmp_path / "zones.json"
    path.write_text(json.dumps(_zones()), encoding="utf-8")
    server, thread, base = _serve(zone_set=load_zone_set(path))
    try:
        request = _request(_zones(), _body(end_x=2.5))
        del request["zones"]
        status, answer = _post(f"{base}/check", request)
        assert (status, answer["state"]) == (200, "danger")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# --- the commands ---


def _files(tmp_path: Path, zones: dict, body: dict) -> list[str]:
    (tmp_path / "zones.json").write_text(json.dumps(zones), encoding="utf-8")
    (tmp_path / "detections.json").write_text(json.dumps({"objects": []}), encoding="utf-8")
    (tmp_path / "observation.json").write_text(
        json.dumps({"active": True, "observedAt": datetime.now(timezone.utc).isoformat(), "maxAgeSeconds": 3600, "error": None}), encoding="utf-8"
    )
    (tmp_path / "body.json").write_text(json.dumps(body), encoding="utf-8")
    return [
        "check",
        "--zones",
        str(tmp_path / "zones.json"),
        "--detections",
        str(tmp_path / "detections.json"),
        "--observation",
        str(tmp_path / "observation.json"),
        "--body",
        str(tmp_path / "body.json"),
    ]


def test_check_command_with_a_body(tmp_path: Path, capsys) -> None:
    assert main(_files(tmp_path, _zones(), _body(end_x=2.5))) == 2
    out = capsys.readouterr().out
    assert "BREACH: object 'robot:forearm' inside danger zone 'fence'" in out
    assert main(_files(tmp_path, _zones(), _body(end_x=1.0))) == 0


def test_check_command_refuses_a_body_when_the_switch_is_off(tmp_path: Path, capsys) -> None:
    assert main(_files(tmp_path, _zones(False), _body(end_x=1.0))) == 3
    assert capsys.readouterr().out.startswith("SAFETY STATE: INHIBITED")


def test_check_command_refuses_a_body_file_that_is_not_json(tmp_path: Path, capsys) -> None:
    args = _files(tmp_path, _zones(), _body())
    (tmp_path / "body.json").write_text("{not json", encoding="utf-8")
    assert main(args) == 3
    (tmp_path / "body.json").write_text('{"segments": [{"id": "a", "start": {"x": NaN, "y": 0, "z": 0}}]}', encoding="utf-8")
    assert main(args) == 3
    assert "INHIBITED" in capsys.readouterr().out


# --- from the output of fk ---

FK = [
    {"joint": "shoulder", "position": {"x": 0.0, "y": 0.0, "z": 0.3}, "quaternion": {"w": 1, "x": 0, "y": 0, "z": 0}},
    {"joint": "elbow", "position": {"x": 0.5, "y": 0.0, "z": 0.3}, "quaternion": {"w": 1, "x": 0, "y": 0, "z": 0}},
    {"joint": "wrist", "position": {"x": 1.5, "y": 0.0, "z": 0.3}, "quaternion": {"w": 1, "x": 0, "y": 0, "z": 0}},
]
SPEC = {
    "base": {"x": 0, "y": 0, "z": 0},
    "segments": [
        {"id": "column", "from": "base", "to": "shoulder", "radius": 0.1},
        {"id": "upper", "from": "shoulder", "to": "elbow", "radius": 0.08},
        {"id": "tool", "from": "wrist", "to": {"x": 1.5, "y": 0, "z": 0.1}, "radius": 0.05},
    ],
}


def test_body_from_fk_resolves_names_the_base_and_explicit_points() -> None:
    body = body_from_fk(FK, SPEC)
    assert [s["id"] for s in body["segments"]] == ["column", "upper", "tool"]
    assert body["segments"][0]["start"] == {"x": 0.0, "y": 0.0, "z": 0.0}
    assert body["segments"][1]["end"] == {"x": 0.5, "y": 0.0, "z": 0.3}
    assert body["segments"][2]["end"] == {"x": 1.5, "y": 0.0, "z": 0.1}
    parse_robot_body(body, parse_zone_set(_zones()).robot_body)  # and it is a valid body


@pytest.mark.parametrize(
    "fk, spec",
    [
        ([], SPEC),
        ({"joint": "a"}, SPEC),
        ([{"joint": "a"}], SPEC),
        ([{"joint": "a", "position": {"x": float("nan"), "y": 0, "z": 0}}], {"segments": [{"id": "s", "from": "a", "to": "a", "radius": 0.1}]}),
        (FK + [FK[0]], SPEC),
        ([{"joint": "base", "position": {"x": 0, "y": 0, "z": 0}}], SPEC),
        (FK, {**SPEC, "segments": [{"id": "s", "from": "ghost", "to": "elbow", "radius": 0.1}]}),
        (FK, {**SPEC, "segments": [{"id": "s", "from": "shoulder", "to": "elbow"}]}),
        (FK, {**SPEC, "segments": [{"id": "s", "from": "shoulder", "to": "elbow", "radius": 0}]}),
        (FK, {**SPEC, "segments": []}),
        (FK, {"segments": [{"id": "s", "from": "base", "to": "elbow", "radius": 0.1}]}),
        (FK, {**SPEC, "color": 1}),
        (FK, "x"),
    ],
)
def test_body_from_fk_refuses_everything_it_cannot_resolve(fk, spec) -> None:
    with pytest.raises(FkInputError):
        body_from_fk(fk, spec)


def test_body_from_fk_command_and_the_whole_chain(tmp_path: Path, capsys) -> None:
    (tmp_path / "fk.json").write_text(json.dumps(FK), encoding="utf-8")
    (tmp_path / "spec.json").write_text(json.dumps(SPEC), encoding="utf-8")
    out = tmp_path / "body.json"
    assert main(["body-from-fk", "--fk", str(tmp_path / "fk.json"), "--segments", str(tmp_path / "spec.json"), "--output", str(out)]) == 0
    assert "3 segments written" in capsys.readouterr().out
    zones = _zones()
    zones["zones"][0]["min"]["x"] = 1.0  # the fence now stands in the way of the upper arm and the wrist
    zones["zones"][0]["max"]["x"] = 1.2
    (tmp_path / "zones.json").write_text(json.dumps(zones), encoding="utf-8")
    (tmp_path / "detections.json").write_text(json.dumps({"objects": []}), encoding="utf-8")
    (tmp_path / "observation.json").write_text(
        json.dumps({"active": True, "observedAt": datetime.now(timezone.utc).isoformat(), "maxAgeSeconds": 3600, "error": None}), encoding="utf-8"
    )
    code = main(
        [
            "check",
            "--zones", str(tmp_path / "zones.json"),
            "--detections", str(tmp_path / "detections.json"),
            "--observation", str(tmp_path / "observation.json"),
            "--body", str(out),
        ]
    )
    assert code == 0  # the arm ends at x = 0.5 and the tool hangs at x = 1.5: neither touches 1.0-1.2
    zones["zones"][0]["min"]["x"] = 1.4
    zones["zones"][0]["max"]["x"] = 1.6
    (tmp_path / "zones.json").write_text(json.dumps(zones), encoding="utf-8")
    capsys.readouterr()
    code = main(
        [
            "check",
            "--zones", str(tmp_path / "zones.json"),
            "--detections", str(tmp_path / "detections.json"),
            "--observation", str(tmp_path / "observation.json"),
            "--body", str(out),
        ]
    )
    assert code == 2
    assert "robot:tool" in capsys.readouterr().out


def test_body_from_fk_command_prints_nothing_on_a_bad_input(tmp_path: Path, capsys) -> None:
    (tmp_path / "fk.json").write_text("[]", encoding="utf-8")
    (tmp_path / "spec.json").write_text(json.dumps(SPEC), encoding="utf-8")
    assert main(["body-from-fk", "--fk", str(tmp_path / "fk.json"), "--segments", str(tmp_path / "spec.json")]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not build the robot body" in captured.err
    assert main(["body-from-fk", "--fk", str(tmp_path / "missing.json"), "--segments", str(tmp_path / "spec.json")]) == 3
