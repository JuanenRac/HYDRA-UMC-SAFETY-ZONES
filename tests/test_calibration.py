from datetime import date

import pytest

from hydra_umc_safety_zones.calibration import (
    CalibrationError,
    ZoneCalibration,
    calibration_age_days,
    is_calibration_expired,
    parse_calibration,
)


def test_parse_calibration_valid():
    cal = parse_calibration(
        {"version": "cal-1", "source": "manual", "calibrated_at": "2026-08-01", "max_age_days": 30}
    )
    assert cal.version == "cal-1"
    assert cal.source == "manual"
    assert cal.calibrated_at == date(2026, 8, 1)
    assert cal.max_age_days == 30


@pytest.mark.parametrize(
    "data",
    [
        {"source": "manual", "calibrated_at": "2026-08-01", "max_age_days": 30},
        {"version": "cal-1", "calibrated_at": "2026-08-01", "max_age_days": 30},
        {"version": "cal-1", "source": "manual", "max_age_days": 30},
        {"version": "cal-1", "source": "manual", "calibrated_at": "2026-08-01"},
        {"version": "", "source": "manual", "calibrated_at": "2026-08-01", "max_age_days": 30},
        {"version": "cal-1", "source": "", "calibrated_at": "2026-08-01", "max_age_days": 30},
    ],
)
def test_parse_calibration_missing_or_empty_field_raises(data):
    with pytest.raises(CalibrationError):
        parse_calibration(data)


def test_parse_calibration_bad_date_raises():
    with pytest.raises(CalibrationError):
        parse_calibration(
            {"version": "cal-1", "source": "manual", "calibrated_at": "not-a-date", "max_age_days": 30}
        )


def test_parse_calibration_bad_max_age_raises():
    with pytest.raises(CalibrationError):
        parse_calibration(
            {"version": "cal-1", "source": "manual", "calibrated_at": "2026-08-01", "max_age_days": "soon"}
        )


def test_zone_calibration_rejects_non_positive_max_age():
    with pytest.raises(CalibrationError):
        ZoneCalibration(version="v", source="s", calibrated_at=date(2026, 1, 1), max_age_days=0)
    with pytest.raises(CalibrationError):
        ZoneCalibration(version="v", source="s", calibrated_at=date(2026, 1, 1), max_age_days=-5)


def test_calibration_age_days_real_arithmetic():
    cal = ZoneCalibration(version="v", source="s", calibrated_at=date(2026, 1, 1), max_age_days=30)
    assert calibration_age_days(cal, date(2026, 1, 1)) == 0
    assert calibration_age_days(cal, date(2026, 1, 31)) == 30
    assert calibration_age_days(cal, date(2025, 12, 31)) == -1


def test_is_calibration_expired_boundary_at_exactly_max_age_is_still_valid():
    """Prueba de limites: max_age_days is inclusive - a calibration exactly
    that many days old is still trusted, one day older is not."""
    cal = ZoneCalibration(version="v", source="s", calibrated_at=date(2026, 1, 1), max_age_days=30)
    assert not is_calibration_expired(cal, date(2026, 1, 31))  # exactly 30 days old
    assert is_calibration_expired(cal, date(2026, 2, 1))  # 31 days old


def test_is_calibration_expired_future_calibrated_at_is_treated_as_invalid():
    """A calibration dated in the future (clock skew, bad data entry) must
    fail safe just like an expired one - never treated as extra-fresh."""
    cal = ZoneCalibration(version="v", source="s", calibrated_at=date(2026, 6, 1), max_age_days=30)
    assert is_calibration_expired(cal, date(2026, 5, 1))
