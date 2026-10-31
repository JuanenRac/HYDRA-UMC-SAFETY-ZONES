# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_separation.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""The optional ISO 13855 / ISO 13857 separation model: S = K * T + C plus what the tool still travels. It can only grow a zone."""
import pytest

from hydra_umc_safety_zones.config import parse_zone_set
from hydra_umc_safety_zones.geometry import AABB, Point3D
from hydra_umc_safety_zones.zones import (
    SEPARATION_MARGIN_MAX_M,
    SeparationModel,
    Zone,
    ZoneLevel,
    parse_separation_model,
    scale_zones_for_velocity,
    velocity_margin_m,
)

DANGER = Zone("danger1", ZoneLevel.DANGER, AABB(Point3D(0, 0, 0), Point3D(2, 2, 2)))
MODEL = SeparationModel(approach_speed_mps=1.6, total_stop_time_s=0.5, intrusion_distance_m=0.1)


def test_the_margin_is_k_times_t_plus_c_for_a_still_tool():
    assert MODEL.margin_m(0.0) == pytest.approx(1.6 * 0.5 + 0.1)


def test_a_moving_tool_adds_the_distance_it_travels_while_the_machine_stops():
    assert MODEL.margin_m(1.0) == pytest.approx(1.6 * 0.5 + 0.1 + 1.0 * 0.5)


def test_a_known_deceleration_adds_the_braking_distance():
    braking = SeparationModel(1.6, 0.5, 0.1, tool_deceleration_mps2=2.0)
    assert braking.margin_m(2.0) == pytest.approx(1.6 * 0.5 + 0.1 + 2.0 * 0.5 + 4.0 / 4.0)


def test_the_margin_is_bounded():
    assert MODEL.margin_m(10_000.0) == SEPARATION_MARGIN_MAX_M


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), True, "1"])
def test_every_figure_must_be_a_finite_number_that_is_not_negative(bad):
    for field in ("approach_speed_mps", "total_stop_time_s", "intrusion_distance_m"):
        figures = {"approach_speed_mps": 1.6, "total_stop_time_s": 0.5, "intrusion_distance_m": 0.1, field: bad}
        with pytest.raises(ValueError):
            SeparationModel(**figures)


def test_a_model_of_zeros_and_a_bad_deceleration_are_refused():
    with pytest.raises(ValueError):
        SeparationModel(0, 0, 0)
    for bad in (0.0, -1.0, float("nan")):
        with pytest.raises(ValueError):
            SeparationModel(1.6, 0.5, 0.1, tool_deceleration_mps2=bad)


def test_the_margin_rejects_a_bad_velocity():
    for bad in (-0.1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            MODEL.margin_m(bad)


def test_the_model_never_shrinks_below_the_linear_margin():
    tiny = SeparationModel(0.0, 0.0, 0.001)
    grown = scale_zones_for_velocity((DANGER,), 2.0, tiny)[0].volume
    linear = scale_zones_for_velocity((DANGER,), 2.0)[0].volume
    assert grown == linear  # the linear margin (0.30 m) is the floor


def test_the_model_grows_the_zones_further_than_the_linear_margin():
    with_model = scale_zones_for_velocity((DANGER,), 1.0, MODEL)[0].volume
    linear = scale_zones_for_velocity((DANGER,), 1.0)[0].volume
    assert with_model.min_corner.x < linear.min_corner.x
    assert with_model.min_corner.x == pytest.approx(-MODEL.margin_m(1.0))
    assert velocity_margin_m(1.0) < MODEL.margin_m(1.0)


def test_with_a_model_the_zones_grow_even_when_no_velocity_is_given():
    grown = scale_zones_for_velocity((DANGER,), None, MODEL)[0].volume
    assert grown.min_corner.x == pytest.approx(-MODEL.margin_m(0.0))
    # and without a model an omitted velocity is still the very same zones
    zones = (DANGER,)
    assert scale_zones_for_velocity(zones, None, None) is zones


def test_the_zones_file_may_declare_the_model():
    raw = {
        "zones": [{"id": "d", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 1, "y": 1, "z": 1}}],
        "separation": {"approach_speed_mps": 1.6, "total_stop_time_s": 0.4, "intrusion_distance_m": 0.2, "tool_deceleration_mps2": 3.0},
    }
    zone_set = parse_zone_set(raw)
    assert zone_set.separation == SeparationModel(1.6, 0.4, 0.2, 3.0)
    del raw["separation"]
    assert parse_zone_set(raw).separation is None


def test_a_malformed_model_in_the_file_is_refused():
    for bad in ("1.6", [1], {"approach_speed_mps": 1.6}, {"approach_speed_mps": 1.6, "total_stop_time_s": 0.4, "intrusion_distance_m": 0.2, "k": 1}):
        with pytest.raises(ValueError):
            parse_separation_model(bad)


def test_the_verdict_itself_uses_the_separation_model_not_only_the_list_of_breaches():
    """The state returned by evaluate_safety must agree with the breaches the API and the CLI list: a zone grown by the model."""
    from datetime import datetime, timezone

    from hydra_umc_safety_zones.breach import DetectedObject
    from hydra_umc_safety_zones.calibration import ZoneCalibration
    from hydra_umc_safety_zones.observation import ObservationStatus
    from hydra_umc_safety_zones.safety_state import SafetyState, evaluate_safety
    from hydra_umc_safety_zones.zones import ZoneSet

    now = datetime.now(timezone.utc)
    calibration = ZoneCalibration(version="c", source="manual", calibrated_at=now.date(), max_age_days=30)
    observation = ObservationStatus(active=True, observed_at=now, max_age_seconds=60)
    near = (DetectedObject("o", Point3D(2.5, 1, 1)),)  # 0.5 m outside the zone, inside the 0.9 m the model asks for
    without = ZoneSet(zones=(DANGER,), calibration=calibration)
    with_model = ZoneSet(zones=(DANGER,), calibration=calibration, separation=MODEL)
    assert evaluate_safety(without, near, now.date(), observation).state is SafetyState.READY
    assert evaluate_safety(with_model, near, now.date(), observation).state is SafetyState.DANGER
