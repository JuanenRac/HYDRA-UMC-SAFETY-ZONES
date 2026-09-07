from datetime import date

from hydra_umc_safety_zones.breach import DetectedObject
from hydra_umc_safety_zones.calibration import ZoneCalibration
from hydra_umc_safety_zones.geometry import AABB, Point3D
from hydra_umc_safety_zones.safety_state import SafetyState, evaluate_safety
from hydra_umc_safety_zones.zones import Zone, ZoneLevel, ZoneSet

TODAY = date(2026, 8, 27)

WARNING_ZONE = Zone("warn1", ZoneLevel.WARNING, AABB(Point3D(0, 0, 0), Point3D(10, 10, 10)))
DANGER_ZONE = Zone("danger1", ZoneLevel.DANGER, AABB(Point3D(0, 0, 0), Point3D(2, 2, 2)))
FRESH_CAL = ZoneCalibration(version="cal-1", source="manual", calibrated_at=TODAY, max_age_days=30)
EXPIRED_CAL = ZoneCalibration(
    version="cal-0", source="manual", calibrated_at=date(2020, 1, 1), max_age_days=30
)


def test_evaluate_safety_ready_when_no_breach_and_calibration_valid():
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    objects = (DetectedObject("op1", Point3D(50, 50, 50)),)
    result = evaluate_safety(zone_set, objects, TODAY)
    assert result.state is SafetyState.READY


def test_evaluate_safety_warning_when_only_warning_zone_breached():
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    objects = (DetectedObject("op1", Point3D(5, 5, 5)),)
    result = evaluate_safety(zone_set, objects, TODAY)
    assert result.state is SafetyState.WARNING
    assert "op1" in result.reason


def test_evaluate_safety_danger_when_danger_zone_breached():
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    objects = (DetectedObject("op1", Point3D(1, 1, 1)),)
    result = evaluate_safety(zone_set, objects, TODAY)
    assert result.state is SafetyState.DANGER
    assert "op1" in result.reason


def test_evaluate_safety_inhibited_when_calibration_missing():
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=None)
    # Object nowhere near any zone - would be READY if calibration were
    # ignored, which is exactly the unsafe fallthrough this test guards
    # against.
    objects = (DetectedObject("op1", Point3D(500, 500, 500)),)
    result = evaluate_safety(zone_set, objects, TODAY)
    assert result.state is SafetyState.INHIBITED
    assert "no calibration" in result.reason


def test_evaluate_safety_inhibited_wins_over_a_real_looking_danger_breach():
    """The calibration check runs BEFORE the breach check - an expired
    calibration must win even when the geometry would otherwise report a
    real danger breach, since that geometry cannot be trusted either way."""
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=EXPIRED_CAL)
    objects = (DetectedObject("op1", Point3D(1, 1, 1)),)
    result = evaluate_safety(zone_set, objects, TODAY)
    assert result.state is SafetyState.INHIBITED
    assert "cal-0" in result.reason


def test_evaluate_safety_danger_outranks_warning_for_same_object():
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    # Point (1,1,1) is inside both the warning zone (0..10) and the danger
    # zone (0..2) at once.
    objects = (DetectedObject("op1", Point3D(1, 1, 1)),)
    result = evaluate_safety(zone_set, objects, TODAY)
    assert result.state is SafetyState.DANGER
