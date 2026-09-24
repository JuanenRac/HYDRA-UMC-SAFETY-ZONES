# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/observation.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""("estado desconocido explicito y observador obligatorio"): real
evidence that a detection/tracking observer actually produced the
`objects` evaluate_safety() is about to reason over - the missing half
of the picture that function used to lack entirely. An empty `objects`
tuple looked identical whether the zone was genuinely confirmed clear by
a live, healthy observer, or the observer had gone silent, been switched
off to save resources, or crashed outright - `evaluate_safety()` could
not tell "confirmed clear" from "no idea" apart, and silently treated
both as READY.

This is the fail-safe half of the same "detect vs enforce" boundary
calibration.py already documents for zone geometry, applied here to the
DETECTIONS themselves rather than the static geometry they're checked
against - see that module's own header comment for the matching
reasoning.

Caller-supplied (like ZoneCalibration itself), never tracked server-side:
this whole package stays a pure, stateless decision pipeline. Whoever is
actually running the detector (e.g. HYDRA-UMC-VISION-NODE) is the one
real source of truth for its own health - this module could never
honestly infer that on its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class ObservationError(ValueError):
    """Raised when observation-status metadata itself is malformed."""


@dataclass(frozen=True)
class ObservationStatus:
    """Real evidence backing (or not backing) a set of detections.

    `active`: False means tracking has been explicitly disabled (e.g. to
    save resources) - never inferred from `objects` merely being empty.

    `observed_at`: the real UTC instant the last successful observation
    was made, or None when no real observation has EVER been received
    this session/process - a fresh boot with zero evidence, never
    treated as "confirmed clear" just because nothing has gone wrong yet
    either.

    `max_age_seconds`: deliberately real sub-second precision, unlike
    ZoneCalibration's own day-granularity (calibration.py) - calibration
    drifts over weeks; a live detection feed going stale for even a few
    seconds is a materially different, much faster-moving kind of risk.

    `error`: a real, named internal failure (e.g. "camera driver
    disconnected") - always wins over any observed emptiness, since an
    observer that is actively failing is the opposite of trustworthy
    evidence that a zone is clear.
    """

    active: bool
    observed_at: datetime | None
    max_age_seconds: float
    error: str | None = None

    def __post_init__(self) -> None:
        if self.max_age_seconds <= 0:
            raise ObservationError("max_age_seconds must be a positive number")


def observation_age_seconds(observation: ObservationStatus, now: datetime) -> float | None:
    """Real elapsed seconds since the last observation, or None if there
    has never been one at all this session/process."""
    if observation.observed_at is None:
        return None
    return (now - observation.observed_at).total_seconds()


def is_observation_stale(observation: ObservationStatus, now: datetime) -> bool:
    """True when there has never been a real observation yet, the last
    one is too old, or it's somehow timestamped in the future (clock
    skew, bad data entry) - any of these means `objects` cannot be
    trusted as confirmed evidence right now. Mirrors
    is_calibration_expired()'s own real semantics for the exact same
    reason - see that function's own doc comment."""
    age = observation_age_seconds(observation, now)
    if age is None:
        return True
    return age < 0 or age > observation.max_age_seconds


def parse_observation_status(data: dict) -> ObservationStatus:
    """Parses an already-loaded ``"observation"`` object shaped like:
    ``{"active": bool, "observedAt": "<ISO 8601 datetime>"|null,
    "maxAgeSeconds": N, "error": "<string>"|null}``.

    Raises `ObservationError` on any missing/malformed field - never
    silently fills in a default that could hide a real authoring
    mistake (same discipline `parse_calibration` already applies).
    """
    if "active" not in data:
        raise ObservationError("observation is missing required field: active")
    active = data["active"]
    if not isinstance(active, bool):
        raise ObservationError(f"observation.active must be a boolean: {active!r}")

    observed_at_raw = data.get("observedAt")
    observed_at: datetime | None = None
    if observed_at_raw is not None:
        try:
            observed_at = datetime.fromisoformat(str(observed_at_raw).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ObservationError(
                f"observation.observedAt must be an ISO 8601 datetime: {observed_at_raw!r}"
            ) from exc

    if "maxAgeSeconds" not in data:
        raise ObservationError("observation is missing required field: maxAgeSeconds")
    raw_max_age = data["maxAgeSeconds"]
    # Same real guard calibration.py's own max_age_days parsing already
    # applies: `bool` is a subclass of `int` in Python, so a
    # plain numeric cast would silently accept one instead of raising.
    if isinstance(raw_max_age, bool) or not isinstance(raw_max_age, (int, float)):
        raise ObservationError(
            f"observation.maxAgeSeconds must be a real number, not a boolean: {raw_max_age!r}"
        )
    max_age_seconds = float(raw_max_age)

    error = data.get("error")
    if error is not None and not isinstance(error, str):
        raise ObservationError(f"observation.error must be a string or null: {error!r}")

    return ObservationStatus(active=active, observed_at=observed_at, max_age_seconds=max_age_seconds, error=error)
