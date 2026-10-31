# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/zones.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Real Warning/Danger zone definitions, matching the two-level perimeter
model described in the README (slowdown vs stop)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from hydra_umc_safety_zones.calibration import ZoneCalibration
from hydra_umc_safety_zones.shapes import Volume

if TYPE_CHECKING:
    from hydra_umc_safety_zones.robot_body import RobotBodySettings


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
    volume: Volume


@dataclass(frozen=True)
class ZoneSet:
    """A zone list bound to the calibration that produced it. `calibration`
    is `None` only for a zones file that never declared one at all (see
    config.py) - `safety_state.py` treats that exactly like an expired
    calibration, never like "no calibration needed"."""

    zones: tuple[Zone, ...]
    calibration: ZoneCalibration | None
    # Optional ISO 13855 / ISO 13857 separation-distance model (see SeparationModel); None keeps only the linear margin below.
    separation: "SeparationModel | None" = None
    # The switch and tuning of the robot body (robot_body.py); None is the same as off: a request that carries a body is refused.
    robot_body: "RobotBodySettings | None" = None


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


# Largest margin any model may ask for: a sanity bound so that a mistyped parameter cannot grow a zone without limit.
SEPARATION_MARGIN_MAX_M = 10.0


@dataclass(frozen=True)
class SeparationModel:
    """Separation distance in the form of ISO 13855: ``S = K * T + C``, plus the distance the tool itself still travels while the
    protective function reacts and the machine stops.

    Nothing here is assumed: every figure comes from the installation's own risk assessment (the standard gives K = 2.0 m/s for
    walking and 1.6 m/s for the reach of a hand, but which applies, the reaction and stopping times measured on the machine, and the
    intrusion distance C of ISO 13857 depend on the cell). The model is opt-in; the linear margin always stays as the conservative
    floor, so enabling the model can only grow a zone, never shrink it.

    - ``approach_speed_mps``  K: speed at which a person approaches.
    - ``total_stop_time_s``   T: reaction time of the detection plus the stopping time of the machine.
    - ``intrusion_distance_m`` C: distance a body part can reach towards the hazard before it is detected (ISO 13857).
    - ``tool_deceleration_mps2`` optional: when given, the tool also travels v^2 / (2 a) while it brakes.
    """

    approach_speed_mps: float
    total_stop_time_s: float
    intrusion_distance_m: float
    tool_deceleration_mps2: float | None = None

    def __post_init__(self) -> None:
        for name in ("approach_speed_mps", "total_stop_time_s", "intrusion_distance_m"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"separation.{name} must be a finite number that is not negative, got {value!r}")
        if self.approach_speed_mps == 0 and self.total_stop_time_s == 0 and self.intrusion_distance_m == 0:
            raise ValueError("a separation model with every figure at zero would protect nothing")
        deceleration = self.tool_deceleration_mps2
        if deceleration is not None and (
            isinstance(deceleration, bool) or not isinstance(deceleration, (int, float)) or not math.isfinite(deceleration) or deceleration <= 0
        ):
            raise ValueError(f"separation.tool_deceleration_mps2 must be a finite number above zero, got {deceleration!r}")

    def margin_m(self, tool_velocity_mps: float) -> float:
        """The distance, in meters, the zones must grow by for a tool moving at ``tool_velocity_mps`` (finite, not negative)."""
        if not math.isfinite(tool_velocity_mps) or tool_velocity_mps < 0:
            raise ValueError(f"tool_velocity_mps must be a finite number that is not negative, got {tool_velocity_mps!r}")
        distance = self.approach_speed_mps * self.total_stop_time_s + self.intrusion_distance_m
        distance += tool_velocity_mps * self.total_stop_time_s
        if self.tool_deceleration_mps2 is not None:
            distance += tool_velocity_mps**2 / (2.0 * self.tool_deceleration_mps2)
        return min(distance, SEPARATION_MARGIN_MAX_M)


def parse_separation_model(data: object) -> SeparationModel:
    """Reads the optional ``"separation"`` object of a zones file: ``{"approach_speed_mps": K, "total_stop_time_s": T,
    "intrusion_distance_m": C}`` plus an optional ``"tool_deceleration_mps2"``. Anything else in it is refused."""
    if not isinstance(data, dict):
        raise ValueError("separation must be a JSON object")
    allowed = {"approach_speed_mps", "total_stop_time_s", "intrusion_distance_m", "tool_deceleration_mps2"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"separation has unknown field(s): {unknown}")
    missing = sorted({"approach_speed_mps", "total_stop_time_s", "intrusion_distance_m"} - set(data))
    if missing:
        raise ValueError(f"separation is missing field(s): {missing}")
    return SeparationModel(
        approach_speed_mps=data["approach_speed_mps"],
        total_stop_time_s=data["total_stop_time_s"],
        intrusion_distance_m=data["intrusion_distance_m"],
        tool_deceleration_mps2=data.get("tool_deceleration_mps2"),
    )


def velocity_margin_m(tool_velocity_mps: float) -> float:
    """The extra margin (in meters) a zone's own static volume grows by for
    a given non-negative tool velocity - linear in speed, capped at
    `VELOCITY_MARGIN_MAX_M` so a single bad/huge velocity reading can never
    grow a zone without bound.
    """
    # NaN fails every comparison: min(nan * gain, MAX) is NaN, which builds a box of NaN that contains nothing - every zone would be silently switched off.
    if not math.isfinite(tool_velocity_mps):
        raise ValueError(f"tool_velocity_mps must be a finite number, got {tool_velocity_mps!r}")
    if tool_velocity_mps < 0:
        raise ValueError("tool_velocity_mps must be non-negative")
    return min(tool_velocity_mps * VELOCITY_MARGIN_GAIN_M_PER_MPS, VELOCITY_MARGIN_MAX_M)


def scale_zones_for_velocity(
    zones: tuple[Zone, ...], tool_velocity_mps: float | None, separation: SeparationModel | None = None
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
        if separation is None:
            return zones
        # the separation distance does not depend on the tool: with a model configured the zones grow even when no velocity is given
        tool_velocity_mps = 0.0
    margin = velocity_margin_m(tool_velocity_mps)
    if separation is not None:
        # the model can only grow the envelope: the linear margin stays as the floor
        margin = max(margin, separation.margin_m(tool_velocity_mps))
    if margin <= 0:
        return zones
    return tuple(
        Zone(zone_id=zone.zone_id, level=zone.level, volume=zone.volume.expanded(margin))
        for zone in zones
    )
