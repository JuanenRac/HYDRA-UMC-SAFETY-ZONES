# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/zones.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Real Warning/Danger zone definitions, matching the two-level perimeter
model described in the README (slowdown vs stop)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from hydra_umc_safety_zones.calibration import ZoneCalibration
from hydra_umc_safety_zones.geometry import AABB


class ZoneLevel(str, Enum):
    """Ordered by severity: DANGER always outranks WARNING when a single
    object breaches zones of both levels at once (see `breach.py`)."""

    WARNING = "warning"
    DANGER = "danger"


@dataclass(frozen=True)
class Zone:
    """A single named Warning or Danger perimeter."""

    zone_id: str
    level: ZoneLevel
    volume: AABB


@dataclass(frozen=True)
class ZoneSet:
    """A zone list bound to the calibration that produced it. `calibration`
    is `None` only for a zones file that never declared one at all (see
    config.py) - `safety_state.py` treats that exactly like an expired
    calibration, never like "no calibration needed"."""

    zones: tuple[Zone, ...]
    calibration: ZoneCalibration | None


# First real, bounded implementation of a velocity-proportional safety
# envelope: a zone's own configured volume is the unconditional floor - it
# never gets smaller - and grows by an extra margin proportional to the
# real head/tool velocity the caller supplies (POST /check's own optional
# `toolVelocityMps`, see api.py/config.py). A moving tool can travel this
# extra margin before an operator/controller has a chance to react, so
# growing the envelope by (gain * speed), capped at a fixed maximum, is a
# conservative first cut - not a physically-derived stopping-distance
# model (that needs real deceleration/reaction-time data this project does
# not have yet), but real, bounded, and always fail-safe: unknown/omitted
# velocity leaves the static zone completely unchanged.
VELOCITY_MARGIN_GAIN_M_PER_MPS = 0.15
VELOCITY_MARGIN_MAX_M = 0.5


def velocity_margin_m(tool_velocity_mps: float) -> float:
    """The extra margin (in meters) a zone's own static volume grows by for
    a given non-negative tool velocity - linear in speed, capped at
    `VELOCITY_MARGIN_MAX_M` so a single bad/huge velocity reading can never
    grow a zone without bound.
    """
    if tool_velocity_mps < 0:
        raise ValueError("tool_velocity_mps must be non-negative")
    return min(tool_velocity_mps * VELOCITY_MARGIN_GAIN_M_PER_MPS, VELOCITY_MARGIN_MAX_M)


def scale_zones_for_velocity(
    zones: tuple[Zone, ...], tool_velocity_mps: float | None
) -> tuple[Zone, ...]:
    """Returns `zones` unchanged when `tool_velocity_mps` is `None` (the
    fail-safe default: a caller that never supplies a real velocity value
    gets exactly today's static behavior, byte-for-byte, not a smaller or
    differently-shaped envelope) or when the computed margin rounds down to
    0. Otherwise returns a new tuple of `Zone`s with the exact same
    `zone_id`/`level`, each one's `volume` grown by `velocity_margin_m()`
    via `AABB.expanded()` - which itself refuses to ever shrink a volume.
    """
    if tool_velocity_mps is None:
        return zones
    margin = velocity_margin_m(tool_velocity_mps)
    if margin <= 0:
        return zones
    return tuple(
        Zone(zone_id=zone.zone_id, level=zone.level, volume=zone.volume.expanded(margin))
        for zone in zones
    )
