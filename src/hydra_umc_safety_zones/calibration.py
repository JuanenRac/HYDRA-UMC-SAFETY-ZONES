# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/calibration.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Real calibration-freshness tracking for zone geometry.

This is the fail-safe half of the README's "detect vs enforce" boundary:
a zone whose geometry hasn't been reconfirmed recently must not silently
keep reporting "no breach" as if it were still trustworthy. A camera that
shifted, a workspace that was rearranged, or a calibration file nobody
re-ran in months are all real ways a Warning/Danger perimeter can quietly
stop matching reality - this module makes "how old is this geometry, and
is that still acceptable" an explicit, checkable fact instead of an
assumption baked silently into `zones.json` forever.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


class CalibrationError(ValueError):
    """Raised when calibration metadata itself is malformed."""


@dataclass(frozen=True)
class ZoneCalibration:
    """Provenance and freshness policy for one zone set's geometry.

    `version` and `source` exist so a SAFE_STOP/INHIBITED reason can name
    exactly which calibration run produced the geometry currently in use -
    "which reason wins" should never be a guess another service has to
    reinvent (see safety_state.py).
    """

    version: str
    source: str
    calibrated_at: date
    max_age_days: int

    def __post_init__(self) -> None:
        if not self.version:
            raise CalibrationError("calibration version must be a non-empty string")
        if not self.source:
            raise CalibrationError("calibration source must be a non-empty string")
        if self.max_age_days <= 0:
            raise CalibrationError("max_age_days must be a positive integer")


def parse_calibration(data: dict) -> ZoneCalibration:
    """Parses the ``"calibration"`` object of a zones JSON file. Raises
    `CalibrationError` on any missing/malformed field - never silently
    fills in a default that could hide a real authoring mistake."""
    try:
        calibrated_at = datetime.strptime(str(data["calibrated_at"]), "%Y-%m-%d").date()
    except KeyError as exc:
        raise CalibrationError("calibration is missing required field: calibrated_at") from exc
    except ValueError as exc:
        raise CalibrationError(
            f"calibrated_at must be an ISO date (YYYY-MM-DD): {data.get('calibrated_at')!r}"
        ) from exc

    if "max_age_days" not in data:
        raise CalibrationError("calibration is missing required field: max_age_days")
    try:
        max_age_days = int(data["max_age_days"])
    except (TypeError, ValueError) as exc:
        raise CalibrationError(
            f"max_age_days must be an integer: {data['max_age_days']!r}"
        ) from exc

    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise CalibrationError("calibration is missing required non-empty field: version")
    source = data.get("source")
    if not isinstance(source, str) or not source:
        raise CalibrationError("calibration is missing required non-empty field: source")

    return ZoneCalibration(
        version=version,
        source=source,
        calibrated_at=calibrated_at,
        max_age_days=max_age_days,
    )


def calibration_age_days(calibration: ZoneCalibration, today: date) -> int:
    """Real elapsed days between `calibrated_at` and `today` - negative if
    `calibrated_at` is somehow in the future (clock skew, bad data entry),
    which `is_calibration_expired` below treats as invalid too."""
    return (today - calibration.calibrated_at).days


def is_calibration_expired(calibration: ZoneCalibration, today: date) -> bool:
    """True when the calibration is too old (age > max_age_days) OR
    dated in the future (age < 0) - either way, the geometry cannot be
    trusted as-is, so this is the single check `safety_state.py` relies
    on to decide whether to trust `zones.py` at all."""
    age = calibration_age_days(calibration, today)
    return age < 0 or age > calibration.max_age_days
