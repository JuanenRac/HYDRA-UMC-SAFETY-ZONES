from datetime import date, datetime, timedelta, timezone

from hydra_umc_safety_zones.breach import DetectedObject
from hydra_umc_safety_zones.calibration import ZoneCalibration
from hydra_umc_safety_zones.geometry import AABB, Point3D
from hydra_umc_safety_zones.observation import ObservationStatus

from hydra_umc_safety_zones.safety_state import (
    SDK_SAFETY_STATE_SCHEMA_VERSION,
    SafetyEvaluation,
    SafetyState,
    evaluate_safety,
    to_sdk_safety_state,
)
from hydra_umc_safety_zones.zones import Zone, ZoneLevel, ZoneSet

TODAY = date(2026, 8, 27)
NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

WARNING_ZONE = Zone("warn1", ZoneLevel.WARNING, AABB(Point3D(0, 0, 0), Point3D(10, 10, 10)))
DANGER_ZONE = Zone("danger1", ZoneLevel.DANGER, AABB(Point3D(0, 0, 0), Point3D(2, 2, 2)))
FRESH_CAL = ZoneCalibration(version="cal-1", source="manual", calibrated_at=TODAY, max_age_days=30)
EXPIRED_CAL = ZoneCalibration(
    version="cal-0", source="manual", calibrated_at=date(2020, 1, 1), max_age_days=30
)
# a real, active, fresh observer - the evidence evaluate_safety
# now requires before it will ever trust `objects` at all. Used by every
# pre-existing breach-logic test below so they keep testing exactly what
# they always tested (calibration/breach interaction), not this new gate.
FRESH_OBSERVATION = ObservationStatus(active=True, observed_at=NOW, max_age_seconds=5.0)


def test_evaluate_safety_ready_when_no_breach_and_calibration_valid():
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    objects = (DetectedObject("op1", Point3D(50, 50, 50)),)
    result = evaluate_safety(zone_set, objects, TODAY, FRESH_OBSERVATION, now=NOW)
    assert result.state is SafetyState.READY


def test_evaluate_safety_warning_when_only_warning_zone_breached():
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    objects = (DetectedObject("op1", Point3D(5, 5, 5)),)
    result = evaluate_safety(zone_set, objects, TODAY, FRESH_OBSERVATION, now=NOW)
    assert result.state is SafetyState.WARNING
    assert "op1" in result.reason


def test_evaluate_safety_danger_when_danger_zone_breached():
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    objects = (DetectedObject("op1", Point3D(1, 1, 1)),)
    result = evaluate_safety(zone_set, objects, TODAY, FRESH_OBSERVATION, now=NOW)
    assert result.state is SafetyState.DANGER
    assert "op1" in result.reason


def test_evaluate_safety_inhibited_when_calibration_missing():
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=None)
    # Object nowhere near any zone - would be READY if calibration were
    # ignored, which is exactly the unsafe fallthrough this test guards
    # against.
    objects = (DetectedObject("op1", Point3D(500, 500, 500)),)
    result = evaluate_safety(zone_set, objects, TODAY, FRESH_OBSERVATION, now=NOW)
    assert result.state is SafetyState.INHIBITED
    assert "no calibration" in result.reason


def test_evaluate_safety_inhibited_wins_over_a_real_looking_danger_breach():
    """The calibration check runs BEFORE the breach check - an expired
    calibration must win even when the geometry would otherwise report a
    real danger breach, since that geometry cannot be trusted either way."""
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=EXPIRED_CAL)
    objects = (DetectedObject("op1", Point3D(1, 1, 1)),)
    result = evaluate_safety(zone_set, objects, TODAY, FRESH_OBSERVATION, now=NOW)
    assert result.state is SafetyState.INHIBITED
    assert "cal-0" in result.reason


def test_evaluate_safety_danger_outranks_warning_for_same_object():
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    # Point (1,1,1) is inside both the warning zone (0..10) and the danger
    # zone (0..2) at once.
    objects = (DetectedObject("op1", Point3D(1, 1, 1)),)
    result = evaluate_safety(zone_set, objects, TODAY, FRESH_OBSERVATION, now=NOW)
    assert result.state is SafetyState.DANGER


# --- observer health must gate READY, exactly like calibration does ---


def test_evaluate_safety_inhibited_when_no_observation_status_provided():
    # The exact real anti-pattern this fix exists to close: an empty
    # `objects` tuple used to always mean READY, indistinguishable from a
    # real confirmed-clear observation. No observation evidence at all
    # (the fail-safe default) must never be silently treated as fine.
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    result = evaluate_safety(zone_set, (), TODAY)
    assert result.state is SafetyState.INHIBITED
    assert "no observation status" in result.reason


def test_evaluate_safety_inhibited_when_detections_removed_but_observer_reports_disabled():
    # this project's own literal acceptance test: "retirar detecciones... no
    # convierte la zona en libre" - removing detections must not, by
    # itself, ever look like a confirmed-clear zone.
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    disabled = ObservationStatus(active=False, observed_at=NOW, max_age_seconds=5.0)
    result = evaluate_safety(zone_set, (), TODAY, disabled, now=NOW)
    assert result.state is SafetyState.INHIBITED
    assert "disabled" in result.reason


def test_evaluate_safety_inhibited_when_the_observer_reports_an_internal_error():
    # this project's own literal acceptance test: "un error interno bloquea la
    # habilitacion logica."
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    errored = ObservationStatus(active=True, observed_at=NOW, max_age_seconds=5.0, error="camera driver disconnected")
    result = evaluate_safety(zone_set, (), TODAY, errored, now=NOW)
    assert result.state is SafetyState.INHIBITED
    assert "camera driver disconnected" in result.reason


def test_evaluate_safety_inhibited_when_observation_is_stale():
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    stale = ObservationStatus(active=True, observed_at=NOW - timedelta(seconds=30), max_age_seconds=5.0)
    result = evaluate_safety(zone_set, (), TODAY, stale, now=NOW)
    assert result.state is SafetyState.INHIBITED
    assert "stale" not in result.reason  # the real reason names the age/limit, not a vague label
    assert "30.0s old" in result.reason


def test_evaluate_safety_inhibited_after_a_fresh_boot_never_reuses_a_prior_safe_state():
    # this project's own literal acceptance test: "tras reinicio no se reutiliza
    # el ultimo estado seguro sin nueva observacion."
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    never_observed = ObservationStatus(active=True, observed_at=None, max_age_seconds=5.0)
    result = evaluate_safety(zone_set, (), TODAY, never_observed, now=NOW)
    assert result.state is SafetyState.INHIBITED
    assert "never" in result.reason.lower() or "no real observation" in result.reason


def test_evaluate_safety_ready_when_a_real_active_fresh_observer_confirms_clear():
    # The other half of this project's own real point: this fix must NOT turn
    # "genuinely confirmed clear" into a permanent false alarm - a real,
    # active, fresh observer honestly reporting zero objects is legitimate
    # evidence, not something to second-guess.
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    result = evaluate_safety(zone_set, (), TODAY, FRESH_OBSERVATION, now=NOW)
    assert result.state is SafetyState.READY


# --- tool_velocity_mps: real, bounded, fail-safe envelope scaling ---


def test_evaluate_safety_omitted_velocity_is_byte_for_byte_the_same_as_before():
    # An object just outside the static danger zone (max corner (2,2,2)):
    # with no velocity supplied, this must stay exactly the pre-existing
    # static behavior - no breach, no matter what a velocity-aware caller
    # might otherwise expect.
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    objects = (DetectedObject("op1", Point3D(2.1, 1, 1)),)
    result = evaluate_safety(zone_set, objects, TODAY, FRESH_OBSERVATION, now=NOW)
    assert result.state is SafetyState.WARNING  # still inside the wider warning zone, not danger


def test_evaluate_safety_zero_velocity_matches_omitted_velocity():
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    objects = (DetectedObject("op1", Point3D(2.1, 1, 1)),)
    omitted = evaluate_safety(zone_set, objects, TODAY, FRESH_OBSERVATION, now=NOW)
    zero = evaluate_safety(zone_set, objects, TODAY, FRESH_OBSERVATION, now=NOW, tool_velocity_mps=0.0)
    assert omitted.state == zero.state


def test_evaluate_safety_real_velocity_grows_the_danger_zone_and_triggers_earlier():
    # Same object position as the "omitted velocity" case above - only
    # difference is a real, non-zero tool velocity - now the grown danger
    # envelope actually reaches it.
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    objects = (DetectedObject("op1", Point3D(2.1, 1, 1)),)
    result = evaluate_safety(zone_set, objects, TODAY, FRESH_OBSERVATION, now=NOW, tool_velocity_mps=2.0)
    assert result.state is SafetyState.DANGER


def test_evaluate_safety_velocity_never_shrinks_the_static_danger_zone():
    # An object well inside the static danger zone must still be DANGER at
    # every velocity, including 0 - the static extent is the unconditional
    # floor, never reduced by this parameter.
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=FRESH_CAL)
    objects = (DetectedObject("op1", Point3D(1, 1, 1)),)
    for velocity in (None, 0.0, 1.0, 50.0):
        result = evaluate_safety(zone_set, objects, TODAY, FRESH_OBSERVATION, now=NOW, tool_velocity_mps=velocity)
        assert result.state is SafetyState.DANGER


def test_evaluate_safety_observer_check_runs_before_breach_logic_but_after_calibration():
    # Ordering matters for a consistent, predictable reason string: missing
    # calibration still wins over a missing observation, but a missing
    # observation wins over what would otherwise be a real breach.
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=None)
    objects = (DetectedObject("op1", Point3D(1, 1, 1)),)  # would be DANGER if evaluated
    result = evaluate_safety(zone_set, objects, TODAY, None, now=NOW)
    assert result.state is SafetyState.INHIBITED
    assert "calibration" in result.reason  # calibration's own reason, not observation's


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


def test_an_expired_calibration_names_who_ran_it():
    stale = ZoneCalibration(
        version="cal-9", source="manual", calibrated_at=date(2026, 1, 1), max_age_days=30, calibrated_by="A. Operator"
    )
    zone_set = ZoneSet(zones=(WARNING_ZONE, DANGER_ZONE), calibration=stale)
    result = evaluate_safety(zone_set, (), TODAY, FRESH_OBSERVATION, now=NOW)
    assert result.state is SafetyState.INHIBITED
    assert "by A. Operator" in result.reason

