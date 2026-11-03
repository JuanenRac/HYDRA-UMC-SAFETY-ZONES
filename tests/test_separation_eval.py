# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_separation_eval.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""K, T and C have no default: a zone without them is NOT evaluated, and not evaluated never releases."""
from datetime import datetime, timezone

import pytest

from hydra_umc_safety_zones.breach import DetectedObject
from hydra_umc_safety_zones.config import ConfigError, parse_zone_set
from hydra_umc_safety_zones.geometry import Point3D
from hydra_umc_safety_zones.observation import ObservationStatus
from hydra_umc_safety_zones.safety_state import SafetyState, evaluate_safety
from hydra_umc_safety_zones.separation_eval import separation_report
from hydra_umc_safety_zones.zones import parse_separation_params, scale_zones_for_velocity

NOW = datetime.now(timezone.utc)
CALIBRATION = {"version": "c", "source": "manual", "calibrated_at": NOW.date().isoformat(), "max_age_days": 30}
OBSERVATION = ObservationStatus(active=True, observed_at=NOW, max_age_seconds=60)
FIGURES = {"approach_speed_mps": 1.6, "total_stop_time_s": 0.5, "intrusion_distance_m": 0.1}  # S = 0.9 m


def _zone(zone_id="d1", **extra):
    return {"id": zone_id, "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 2, "y": 2, "z": 2}, **extra}


def _set(zones, **top):
    return parse_zone_set({"calibration": CALIBRATION, "zones": zones, **top})


def _eval(zone_set, x=1.0):
    return evaluate_safety(zone_set, (DetectedObject("o", Point3D(x, 1, 1)),), NOW.date(), OBSERVATION)


def _verdicts(zone_set, objects=()):
    from hydra_umc_safety_zones.safety_state import collect_breaches

    return {z["zone"]: z["verdict"] for z in separation_report(zone_set, collect_breaches(zone_set, objects))["zones"]}


def test_without_any_figure_the_zone_is_not_evaluated_with_the_reason():
    zone_set = _set([_zone()])
    report = separation_report(zone_set, ())
    assert report["status"] == "not_evaluated"
    assert report["zones"][0]["verdict"] == "not_evaluated" and "distanceM" not in report["zones"][0]
    assert "approach_speed_mps" in report["zones"][0]["reason"]


def test_nothing_is_computed_with_an_invented_value_a_far_object_does_not_make_it_evaluated():
    zone_set = _set([_zone()])
    assert _verdicts(zone_set, (DetectedObject("o", Point3D(50, 1, 1)),)) == {"d1": "not_evaluated"}


@pytest.mark.parametrize("empty", ["approach_speed_mps", "total_stop_time_s", "intrusion_distance_m"])
def test_one_empty_figure_is_enough_for_not_evaluated(empty):
    figures = {**FIGURES, empty: None}
    zone_set = _set([_zone(separation=figures)])
    report = separation_report(zone_set, ())
    assert report["zones"][0]["verdict"] == "not_evaluated" and empty in report["zones"][0]["reason"]


def test_a_zone_with_its_own_incomplete_figures_does_not_borrow_the_machine_ones():
    zone_set = _set([_zone(separation={"approach_speed_mps": 1.6})], separation=FIGURES)
    assert _verdicts(zone_set) == {"d1": "not_evaluated"}


def test_a_zone_without_figures_uses_the_machine_figures():
    zone_set = _set([_zone()], separation=FIGURES)
    assert _verdicts(zone_set) == {"d1": "evaluated_safe"}


def test_evaluated_safe_and_evaluated_unsafe_use_s_equals_k_t_plus_c():
    zone_set = _set([_zone()], separation=FIGURES)
    # the zone ends at x=2; S = 0.9 m, so x=2.85 is inside the grown zone and x=2.95 is outside
    assert _verdicts(zone_set, (DetectedObject("o", Point3D(2.85, 1, 1)),)) == {"d1": "evaluated_unsafe"}
    assert _verdicts(zone_set, (DetectedObject("o", Point3D(2.95, 1, 1)),)) == {"d1": "evaluated_safe"}
    assert separation_report(zone_set, ())["zones"][0]["distanceM"] == pytest.approx(0.9)


def test_each_zone_grows_by_its_own_s():
    zone_set = _set([_zone("a", separation=FIGURES), _zone("b", separation={**FIGURES, "total_stop_time_s": 2.0})])
    grown = scale_zones_for_velocity(zone_set.zones, None, zone_set.separation)
    assert grown[0].volume.max_corner.x == pytest.approx(2.9)
    assert grown[1].volume.max_corner.x == pytest.approx(2 + 1.6 * 2.0 + 0.1)


def test_overall_status_is_the_worst_zone():
    zone_set = _set([_zone("a", separation=FIGURES), _zone("b")])
    assert separation_report(zone_set, ())["status"] == "not_evaluated"
    both = _set([_zone("a", separation=FIGURES), _zone("b", separation=FIGURES)])
    assert separation_report(both, ())["status"] == "evaluated_safe"


def test_required_makes_a_not_evaluated_zone_inhibit_a_verdict_that_would_be_ready():
    zone_set = _set([_zone()], separation_required=True)
    evaluation = _eval(zone_set, x=50.0)
    assert evaluation.state is SafetyState.INHIBITED and "not evaluated" in evaluation.reason
    assert evaluation.separation["status"] == "not_evaluated"


def test_not_required_keeps_the_old_verdicts_but_still_reports_not_evaluated():
    zone_set = _set([_zone()])
    evaluation = _eval(zone_set, x=50.0)
    assert evaluation.state is SafetyState.READY and evaluation.separation["status"] == "not_evaluated"
    assert evaluation.separation["required"] is False


def test_required_with_figures_releases_and_a_breach_still_wins():
    zone_set = _set([_zone()], separation=FIGURES, separation_required=True)
    assert _eval(zone_set, x=50.0).state is SafetyState.READY
    assert _eval(zone_set, x=50.0).separation["status"] == "evaluated_safe"
    unsafe = _eval(zone_set, x=1.0)
    assert unsafe.state is SafetyState.DANGER and unsafe.separation["status"] == "evaluated_unsafe"


def test_a_danger_is_not_hidden_by_a_not_evaluated_zone():
    zone_set = _set([_zone()], separation_required=True)
    assert _eval(zone_set, x=1.0).state is SafetyState.DANGER


def test_empty_figures_in_the_file_are_not_an_error_but_wrong_ones_are():
    assert parse_separation_params(None)[0] is None
    assert parse_separation_params({"approach_speed_mps": None, "total_stop_time_s": None, "intrusion_distance_m": None})[1] == (
        "approach_speed_mps", "total_stop_time_s", "intrusion_distance_m")
    for bad in (-1, float("nan"), float("inf"), True, "1"):
        with pytest.raises(ValueError):
            parse_separation_params({**FIGURES, "approach_speed_mps": bad})
        with pytest.raises(ValueError):  # a wrong figure is refused even when another one is still empty
            parse_separation_params({"approach_speed_mps": bad})
    with pytest.raises(ValueError):
        parse_separation_params({**FIGURES, "k": 1})
    with pytest.raises(ValueError):
        parse_separation_params([1])
    with pytest.raises(ValueError):
        parse_separation_params({"approach_speed_mps": 0, "total_stop_time_s": 0, "intrusion_distance_m": 0})


def test_the_file_refuses_bad_zone_figures_and_a_bad_required_flag():
    with pytest.raises(ConfigError, match="d1"):
        _set([_zone(separation={**FIGURES, "total_stop_time_s": -1})])
    with pytest.raises(ConfigError):
        _set([_zone()], separation_required="yes")


def test_boundary_the_object_exactly_at_s_counts_as_inside():
    zone_set = _set([_zone()], separation=FIGURES)
    assert _verdicts(zone_set, (DetectedObject("o", Point3D(2.9, 1, 1)),)) == {"d1": "evaluated_unsafe"}


def test_state_route_exposes_the_separation_verdict():
    import json
    import threading
    import urllib.request

    from hydra_umc_safety_zones.api import SafetyZonesServer

    zone_set = {"calibration": CALIBRATION, "zones": [_zone()]}
    server = SafetyZonesServer(("127.0.0.1", 0), "test role")
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = {
            "zones": zone_set,
            "detections": {"objects": []},
            "observation": {"active": True, "observedAt": NOW.isoformat(), "maxAgeSeconds": 60},
        }
        request = urllib.request.Request(f"http://127.0.0.1:{port}/check", json.dumps(body).encode(), {"Content-Type": "application/json"})
        answer = json.loads(urllib.request.urlopen(request, timeout=10).read())
        assert answer["separation"]["status"] == "not_evaluated"
        held = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=10).read())
        assert held["separation"]["status"] == "not_evaluated" and held["sdkSafetyState"]["state"] == "READY"
    finally:
        server.shutdown()
        server.server_close()


def test_the_example_file_parses_and_leaves_every_zone_not_evaluated():
    import json
    from pathlib import Path

    raw = json.loads((Path(__file__).resolve().parent.parent / "docs" / "zones.separation.example.json").read_text(encoding="utf-8"))
    zone_set = parse_zone_set(raw)
    assert zone_set.separation is None and zone_set.separation_required is False
    assert {z.zone_id for z in zone_set.zones} == {"warn1", "danger1"}
    assert set(_verdicts(zone_set).values()) == {"not_evaluated"}
