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
from datetime import date, datetime, timezone
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


# HYDRA-UMC-SDK's own formal contract (contracts/json-schema/v1/
# safety-state.schema.json) requires `state` to be exactly one of
# READY/INHIBITED/FAULT/SAFE_STOP - a real, DIFFERENT vocabulary (and
# different member set) than this module's own internal SafetyState
# above (lowercase ready/warning/danger/inhibited, no FAULT/SAFE_STOP at
# all). Real gap found 2026-09-08 (private plan's own F05): nothing in
# this repo ever emitted the SDK-conformant shape, so a consumer that
# actually wired "the real SafetyState feed" HYDRA-UMC-VISUAL-SERVOING-
# API's own authorization.py already anticipated in its own docstring
# would have compared lowercase "ready" against that module's uppercase
# "READY" and NEVER authorized a single correction, even in a genuinely
# safe cell - a real, silent, would-have-shipped integration bug, not a
# hypothetical one.
SDK_SAFETY_STATE_SCHEMA_VERSION = "1.0"

_SDK_STATE_BY_INTERNAL_STATE: dict[SafetyState, str] = {
    SafetyState.READY: "READY",
    # WARNING does not stop the physical cell in this module's own model
    # (evaluate_safety() above never raises an E-STOP for it - see
    # estop.py's own detect-vs-enforce boundary) but a vision-driven
    # correction is a more sensitive consumer than a plain motion
    # controller: authorizing a fine PBVS correction while an object has
    # already breached a warning zone is a real, separate judgment call,
    # made deliberately conservative (fail-closed) here rather than
    # silently reusing READY.
    SafetyState.WARNING: "INHIBITED",
    # DANGER is this module's own live E-STOP-request condition (see
    # api.py's own request_estop_for() call) - SAFE_STOP is the SDK's own
    # closest real semantic match for "an e-stop is actively in effect",
    # not a plain access-denial.
    SafetyState.DANGER: "SAFE_STOP",
    # Untrustworthy geometry (missing/expired calibration) is exactly the
    # SDK's own INHIBITED semantics - a direct match, not a judgment call.
    SafetyState.INHIBITED: "INHIBITED",
}


def to_sdk_safety_state(evaluation: SafetyEvaluation, source: str = "hydra-umc-safety-zones") -> dict:
    """The one real place this module's own internal SafetyEvaluation is
    translated into HYDRA-UMC-SDK's own formal SafetyState contract shape
    (schema_version/state/source/timestamp_utc) - see
    contracts/json-schema/v1/safety-state.schema.json in HYDRA-UMC-SDK for
    the schema this must always validate against. `source` defaults to
    this repo's own name (the schema's own required, non-empty
    `source` field - "which real service reported this state").
    """
    return {
        "schema_version": SDK_SAFETY_STATE_SCHEMA_VERSION,
        "state": _SDK_STATE_BY_INTERNAL_STATE[evaluation.state],
        "source": source,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
