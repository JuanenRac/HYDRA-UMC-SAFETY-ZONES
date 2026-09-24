from datetime import datetime, timedelta, timezone

import pytest

from hydra_umc_safety_zones.observation import (
    ObservationError,
    ObservationStatus,
    is_observation_stale,
    observation_age_seconds,
    parse_observation_status,
)

NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_observation_status_valid():
    obs = parse_observation_status(
        {"active": True, "observedAt": "2026-08-27T12:00:00+00:00", "maxAgeSeconds": 5, "error": None}
    )
    assert obs.active is True
    assert obs.observed_at == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    assert obs.max_age_seconds == 5
    assert obs.error is None


def test_parse_observation_status_accepts_a_real_z_suffixed_utc_timestamp():
    # The real shape a JSON serializer (JS Date#toISOString(), most Python
    # producers) actually emits - fromisoformat() alone doesn't accept a
    # bare "Z" before 3.11, so this must be handled explicitly.
    obs = parse_observation_status({"active": True, "observedAt": "2026-08-27T12:00:00Z", "maxAgeSeconds": 5})
    assert obs.observed_at == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_observation_status_observed_at_defaults_to_none_when_absent():
    # A real, honest "no observation has ever been received" state - never
    # guessed at or defaulted to "now".
    obs = parse_observation_status({"active": True, "observedAt": None, "maxAgeSeconds": 5})
    assert obs.observed_at is None
    obs2 = parse_observation_status({"active": True, "maxAgeSeconds": 5})
    assert obs2.observed_at is None


def test_parse_observation_status_error_defaults_to_none():
    obs = parse_observation_status({"active": True, "observedAt": None, "maxAgeSeconds": 5})
    assert obs.error is None


def test_parse_observation_status_missing_active_raises():
    with pytest.raises(ObservationError):
        parse_observation_status({"observedAt": None, "maxAgeSeconds": 5})


def test_parse_observation_status_non_boolean_active_raises():
    with pytest.raises(ObservationError):
        parse_observation_status({"active": "yes", "observedAt": None, "maxAgeSeconds": 5})


def test_parse_observation_status_missing_max_age_raises():
    with pytest.raises(ObservationError):
        parse_observation_status({"active": True, "observedAt": None})


def test_parse_observation_status_bad_observed_at_raises():
    with pytest.raises(ObservationError):
        parse_observation_status({"active": True, "observedAt": "not-a-datetime", "maxAgeSeconds": 5})


def test_parse_observation_status_non_string_error_raises():
    with pytest.raises(ObservationError):
        parse_observation_status({"active": True, "observedAt": None, "maxAgeSeconds": 5, "error": 42})


# Same real guard calibration.py's own max_age_days parsing already
# applies: bool is a subclass of int in Python.
@pytest.mark.parametrize("bad_max_age", [True, False, "soon"])
def test_parse_observation_status_rejects_bool_or_non_numeric_max_age(bad_max_age):
    with pytest.raises(ObservationError):
        parse_observation_status({"active": True, "observedAt": None, "maxAgeSeconds": bad_max_age})


def test_observation_status_rejects_non_positive_max_age():
    with pytest.raises(ObservationError):
        ObservationStatus(active=True, observed_at=None, max_age_seconds=0)
    with pytest.raises(ObservationError):
        ObservationStatus(active=True, observed_at=None, max_age_seconds=-1.0)


def test_observation_age_seconds_real_arithmetic():
    obs = ObservationStatus(active=True, observed_at=NOW - timedelta(seconds=10), max_age_seconds=30)
    assert observation_age_seconds(obs, NOW) == 10.0


def test_observation_age_seconds_none_when_never_observed():
    obs = ObservationStatus(active=True, observed_at=None, max_age_seconds=30)
    assert observation_age_seconds(obs, NOW) is None


def test_is_observation_stale_true_when_never_observed():
    obs = ObservationStatus(active=True, observed_at=None, max_age_seconds=30)
    assert is_observation_stale(obs, NOW)


def test_is_observation_stale_boundary_at_exactly_max_age_is_still_fresh():
    """Prueba de limites: max_age_seconds is inclusive - an observation
    exactly that old is still trusted, one second older is not."""
    obs = ObservationStatus(active=True, observed_at=NOW - timedelta(seconds=30), max_age_seconds=30)
    assert not is_observation_stale(obs, NOW)
    obs2 = ObservationStatus(active=True, observed_at=NOW - timedelta(seconds=31), max_age_seconds=30)
    assert is_observation_stale(obs2, NOW)


def test_is_observation_stale_future_observed_at_is_treated_as_invalid():
    """An observation timestamped in the future (clock skew) must fail
    safe just like a stale one - never treated as extra-fresh."""
    obs = ObservationStatus(active=True, observed_at=NOW + timedelta(seconds=5), max_age_seconds=30)
    assert is_observation_stale(obs, NOW)


def test_is_observation_stale_false_for_a_real_fresh_observation():
    obs = ObservationStatus(active=True, observed_at=NOW - timedelta(seconds=1), max_age_seconds=30)
    assert not is_observation_stale(obs, NOW)
