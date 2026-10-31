# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/robot_body.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The body of the robot as capsules, placed by whoever knows where the links are.

A request may carry the two ends of every segment of the robot (the poses that
forward kinematics gives, computed elsewhere) and the radius that whoever
configures the cell declares for each segment. Each segment becomes a capsule in
the frame of the zones and is compared with the zones, with the same velocity and
separation margins as a detected object.

The rule is the one of ``shapes.py``: the body may be over-estimated and never
under-estimated.

- A segment is cut into pieces no longer than ``resolution_m`` and each piece is
  replaced by the sphere around its middle that contains it: its radius is the
  declared radius, plus ``margin_m``, plus half the length of the piece, plus a
  rounding allowance. The sphere is larger than the piece, so this can only add
  contacts. When ``max_pieces_per_segment`` stops the cutting, the half piece (and
  with it the sphere) grows instead.
- The rounding allowance is a nanometre plus a millionth of a millimetre per metre
  of the largest coordinate involved, which is far above the error of the
  arithmetic of a rotation and a sum.
- Nothing here knows the robot. The radii are declared, with no default: a radius
  smaller than the real link makes the answer wrong in the unsafe direction and
  the model cannot see it.

This module only builds and compares geometry. It runs nothing and calls no
other program: the poses arrive already computed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from hydra_umc_safety_zones.breach import Breach
from hydra_umc_safety_zones.geometry import Point3D
from hydra_umc_safety_zones.shapes import rotation_matrix
from hydra_umc_safety_zones.zones import Zone

# The ids of the segments in a breach list start with this, so a stop request names the part of the robot.
BODY_OBJECT_PREFIX = "robot:"
# A coordinate beyond this is a mistake (units), not a cell.
MAX_COORDINATE_M = 1.0e6
_EPSILON_M = 1.0e-9
_EPSILON_PER_M = 1.0e-12


class RobotBodyError(ValueError):
    """The robot body of a request cannot be used: the whole request is refused, never read as a smaller body."""


class RobotBodyDisabled(RobotBodyError):
    """A request brought a robot body but the zones file did not switch the feature on."""


@dataclass(frozen=True)
class RobotBodySettings:
    """The switch and the tuning of the robot body, read from ``geometry.robot_body`` of the zones file.

    - ``enabled`` False (the default, and what a file without the key means): a request that carries a robot body is refused.
    - ``required`` True: a request without a robot body is INHIBITED, so a client that stops sending it is not read as all clear.
    - ``resolution_m``: the longest piece a segment is cut into (smaller: tighter, slower).
    - ``margin_m``: extra radius added to every segment, on top of the declared one.
    - ``max_segments`` and ``max_pieces_per_segment``: ceilings so that a request cannot make the check unbounded.
    """

    enabled: bool = False
    required: bool = False
    resolution_m: float = 0.05
    margin_m: float = 0.0
    max_segments: int = 64
    max_pieces_per_segment: int = 200

    def __post_init__(self) -> None:
        for name in ("enabled", "required"):
            if not isinstance(getattr(self, name), bool):
                raise RobotBodyError(f"robot_body.{name} must be true or false, got {getattr(self, name)!r}")
        for name in ("resolution_m", "margin_m"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise RobotBodyError(f"robot_body.{name} must be a finite number, got {value!r}")
        if self.resolution_m <= 0:
            raise RobotBodyError(f"robot_body.resolution_m must be above zero, got {self.resolution_m!r}")
        if self.margin_m < 0:
            raise RobotBodyError(f"robot_body.margin_m must not be negative, got {self.margin_m!r}")
        for name, ceiling in (("max_segments", 1024), ("max_pieces_per_segment", 10000)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
                raise RobotBodyError(f"robot_body.{name} must be a whole number from 1 to {ceiling}, got {value!r}")


@dataclass(frozen=True)
class BodySegment:
    """One segment of the robot in the frame of the zones: the two ends and the declared radius (all in metres)."""

    segment_id: str
    start: Point3D
    end: Point3D
    radius_m: float

    def __post_init__(self) -> None:
        if not isinstance(self.segment_id, str) or not self.segment_id.strip():
            raise RobotBodyError(f"a segment id must be a non-empty string, got {self.segment_id!r}")
        if isinstance(self.radius_m, bool) or not isinstance(self.radius_m, (int, float)) or not math.isfinite(self.radius_m) or self.radius_m <= 0:
            raise RobotBodyError(f"segment {self.segment_id!r}: radius must be a finite number above zero, got {self.radius_m!r}")
        for point in (self.start, self.end):
            if max(abs(point.x), abs(point.y), abs(point.z)) > MAX_COORDINATE_M:
                raise RobotBodyError(f"segment {self.segment_id!r}: a coordinate is beyond {MAX_COORDINATE_M:g} m - check the units")


@dataclass(frozen=True)
class RobotFrame:
    """Where the frame of the poses is in the frame of the zones: the origin and the rotation (roll, pitch, yaw in degrees, R = Rz*Ry*Rx)."""

    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_deg: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def apply(self, point: Point3D) -> Point3D:
        r = rotation_matrix(*self.rotation_deg)
        t = self.translation
        return Point3D(
            r[0][0] * point.x + r[0][1] * point.y + r[0][2] * point.z + t[0],
            r[1][0] * point.x + r[1][1] * point.y + r[1][2] * point.z + t[1],
            r[2][0] * point.x + r[2][1] * point.y + r[2][2] * point.z + t[2],
        )


@dataclass(frozen=True)
class RobotBody:
    """The segments of a request, already in the frame of the zones, and the rounding allowance that goes with their size."""

    segments: tuple[BodySegment, ...]
    allowance_m: float = _EPSILON_M


def build_robot_body(segments: tuple[BodySegment, ...], frame: RobotFrame | None, settings: RobotBodySettings) -> RobotBody:
    """Puts the segments in the frame of the zones and checks the ceilings. Raises RobotBodyError, never returns a partial body."""
    if not segments:
        raise RobotBodyError("a robot body needs at least one segment: an empty body would read as a robot that is nowhere")
    if len(segments) > settings.max_segments:
        raise RobotBodyError(f"a robot body may have {settings.max_segments} segments at most, got {len(segments)}")
    seen: set[str] = set()
    placed: list[BodySegment] = []
    largest = 0.0
    for segment in segments:
        if segment.segment_id in seen:
            raise RobotBodyError(f"segment id {segment.segment_id!r} is used more than once: reports and stop requests would be ambiguous")
        seen.add(segment.segment_id)
        start, end = segment.start, segment.end
        if frame is not None:
            start, end = frame.apply(start), frame.apply(end)
        extent = max(abs(v) for point in (segment.start, segment.end, start, end) for v in (point.x, point.y, point.z))
        if not math.isfinite(extent) or extent > MAX_COORDINATE_M:
            raise RobotBodyError(f"segment {segment.segment_id!r}: a coordinate is beyond {MAX_COORDINATE_M:g} m after the frame is applied")
        largest = max(largest, extent)
        placed.append(BodySegment(segment.segment_id, start, end, segment.radius_m))
    return RobotBody(tuple(placed), _EPSILON_M + _EPSILON_PER_M * largest)


def segment_spheres(segment: BodySegment, body: RobotBody, settings: RobotBodySettings) -> list[tuple[Point3D, float]]:
    """The spheres (middle, radius) whose union contains the capsule of the segment, with the margin of the settings."""
    a, b = segment.start, segment.end
    length = math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))
    pieces = max(1, min(settings.max_pieces_per_segment, math.ceil(length / settings.resolution_m)))
    radius = segment.radius_m + settings.margin_m + length / (2 * pieces) + body.allowance_m
    spheres = []
    for i in range(pieces):
        t = (i + 0.5) / pieces
        spheres.append((Point3D(a.x + t * (b.x - a.x), a.y + t * (b.y - a.y), a.z + t * (b.z - a.z)), radius))
    return spheres


def check_body_breaches(zones: tuple[Zone, ...], body: RobotBody, settings: RobotBodySettings) -> tuple[Breach, ...]:
    """Every (segment, zone) pair where the capsule of the segment may touch the zone: one breach each, placed at the first sphere that touches."""
    breaches: list[Breach] = []
    for segment in body.segments:
        spheres = segment_spheres(segment, body, settings)
        for zone in zones:
            for centre, radius in spheres:
                if zone.volume.intersects_sphere(centre, radius):
                    breaches.append(Breach(zone_id=zone.zone_id, level=zone.level, object_id=BODY_OBJECT_PREFIX + segment.segment_id, position=centre))
                    break
    return tuple(breaches)
