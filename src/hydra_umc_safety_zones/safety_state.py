# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/safety_state.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Combines zone-breach checking with calibration-freshness enforcement
into the single real fail-safe decision the README's E-STOP orchestration
depends on: a `ZoneSet` whose geometry cannot currently be trusted must
resolve to INHIBITED, never fall through and silently report READY as if
nothing were wrong. This module is the one place that decision gets made,
so no other service in the ecosystem has to invent its own criteria for
"what counts as INHIBITED" (see the module docstring in estop.py for the
matching detect-vs-enforce boundary on the E-STOP side).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from hydra_umc_safety_zones.breach import DetectedObject, check_breaches, worst_level_per_object
from hydra_umc_safety_zones.calibration import calibration_age_days, is_calibration_expired
from hydra_umc_safety_zones.zones import ZoneLevel, ZoneSet


class SafetyState(str, Enum):
    """Ordered by severity for anyone that needs to compare states:
    READY < WARNING < DANGER, and INHIBITED is its own category - not
    "worse than DANGER", but "the geometry itself cannot be trusted",
    which is a different kind of unsafe than a real breach."""

    READY = "ready"
    WARNING = "warning"
    DANGER = "danger"
    INHIBITED = "inhibited"


@dataclass(frozen=True)
class SafetyEvaluation:
    """One real decision plus the human-readable reason behind it - never
    just a bare enum value, so a caller (a log line, an E-STOP request, an
    operator dashboard) always has something concrete to show."""

    state: SafetyState
    reason: str


def evaluate_safety(
    zone_set: ZoneSet, objects: tuple[DetectedObject, ...], today: date
) -> SafetyEvaluation:
    """The one real entry point that decides READY/WARNING/DANGER/INHIBITED.

    Calibration is checked FIRST, before any breach logic runs - a missing
    or expired calibration always wins over what the (untrusted) geometry
    would otherwise report, by design.
    """
    if zone_set.calibration is None:
        return SafetyEvaluation(
            SafetyState.INHIBITED,
            "no calibration metadata present - zone geometry cannot be trusted",
        )
    if is_calibration_expired(zone_set.calibration, today):
        age = calibration_age_days(zone_set.calibration, today)
        return SafetyEvaluation(
            SafetyState.INHIBITED,
            f"calibration '{zone_set.calibration.version}' (source="
            f"{zone_set.calibration.source}) is {age} day(s) old, exceeds "
            f"max_age_days={zone_set.calibration.max_age_days}",
        )

    breaches = check_breaches(zone_set.zones, objects)
    worst = worst_level_per_object(breaches)

    danger_objects = sorted(oid for oid, level in worst.items() if level is ZoneLevel.DANGER)
    if danger_objects:
        return SafetyEvaluation(
            SafetyState.DANGER, f"object(s) {danger_objects} breached a danger zone"
        )

    warning_objects = sorted(oid for oid, level in worst.items() if level is ZoneLevel.WARNING)
    if warning_objects:
        return SafetyEvaluation(
            SafetyState.WARNING, f"object(s) {warning_objects} breached a warning zone"
        )

    return SafetyEvaluation(SafetyState.READY, "no breach, calibration valid")
