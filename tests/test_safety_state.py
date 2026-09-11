from datetime import date

from hydra_umc_safety_zones.breach import DetectedObject
from hydra_umc_safety_zones.calibration import ZoneCalibration
from hydra_umc_safety_zones.geometry import AABB, Point3D
from datetime import datetime

from hydra_umc_safety_zones.safety_state import (
    SDK_SAFETY_STATE_SCHEMA_VERSION,
    SafetyEvaluation,
    SafetyState,
    evaluate_safety,
    to_sdk_safety_state,
)
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


# --- to_sdk_safety_state(): real HYDRA-UMC-SDK contract conformance ---
# (F05 - see this function's own header comment for the real bug this
# closes).

REAL_SDK_STATES = {"READY", "INHIBITED", "FAULT", "SAFE_STOP"}


def _assert_real_sdk_shape(payload: dict, expected_state: str, expected_source: str = "hydra-umc-safety-zones") -> None:
    assert payload.keys() == {"schema_version", "state", "source", "timestamp_utc"}
    assert payload["schema_version"] == SDK_SAFETY_STATE_SCHEMA_VERSION
    assert payload["state"] == expected_state
    assert payload["state"] in REAL_SDK_STATES, "must always be one of the SDK's own real 4 enum values"
    assert payload["source"] == expected_source
    # Must be a real, parseable ISO-8601 timestamp - the schema's own
    # format: date-time requirement, not just any non-empty string.
    datetime.fromisoformat(payload["timestamp_utc"])


def test_to_sdk_safety_state_ready_maps_directly():
    _assert_real_sdk_shape(to_sdk_safety_state(SafetyEvaluation(SafetyState.READY, "no breach")), "READY")


def test_to_sdk_safety_state_inhibited_maps_directly():
    _assert_real_sdk_shape(
        to_sdk_safety_state(SafetyEvaluation(SafetyState.INHIBITED, "calibration expired")), "INHIBITED"
    )


def test_to_sdk_safety_state_warning_maps_conservatively_to_inhibited():
    # Real judgment call, not a guess (see to_sdk_safety_state's own
    # header comment): this module's own WARNING never e-stops the cell,
    # but a vision-driven correction is a more sensitive consumer, so it
    # must NOT be silently authorized (mapped to READY) just because the
    # physical cell itself keeps running.
    _assert_real_sdk_shape(
        to_sdk_safety_state(SafetyEvaluation(SafetyState.WARNING, "warning zone breached")), "INHIBITED"
    )


def test_to_sdk_safety_state_danger_maps_to_safe_stop():
    # DANGER is this module's own live E-STOP-request condition - SAFE_STOP
    # is the SDK's own closest real match, not a plain access-denial.
    _assert_real_sdk_shape(
        to_sdk_safety_state(SafetyEvaluation(SafetyState.DANGER, "danger zone breached")), "SAFE_STOP"
    )


def test_to_sdk_safety_state_never_reuses_this_modules_own_lowercase_vocabulary():
    # The real bug this whole function exists to prevent: this module's
    # own internal SafetyState.value strings ("ready", "warning", ...)
    # must never leak into the emitted SDK shape - a consumer comparing
    # against the SDK's real uppercase enum would silently and permanently
    # fail every comparison otherwise.
    for state in SafetyState:
        emitted = to_sdk_safety_state(SafetyEvaluation(state, "x"))
        assert emitted["state"] != state.value or state.value.isupper()
        assert emitted["state"].isupper()


def test_to_sdk_safety_state_source_is_overridable():
    payload = to_sdk_safety_state(SafetyEvaluation(SafetyState.READY, "no breach"), source="a-real-caller")
    _assert_real_sdk_shape(payload, "READY", expected_source="a-real-caller")
