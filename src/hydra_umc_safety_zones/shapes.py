# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/shapes.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Zone volumes that are not axis-aligned boxes: oriented boxes, vertical
cylinders and capsules.

Every shape answers the same questions as ``geometry.AABB`` (``contains``,
``distance_to``, ``intersects_sphere``, ``expanded``, ``bounding_box``) so the
breach check, the velocity and separation margins and the obstacles export treat
them all alike.

The rule for every shape in this module is that it may over-estimate a zone and
never under-estimate it:

- the distance to a shape is the exact Euclidean distance to the solid, reduced
  by ``SHAPE_EPSILON_M`` (one nanometre) so that the rounding of the floating
  point arithmetic can only make a point read as closer, never farther;
- ``expanded(margin)`` grows a shape by at least ``margin`` in every direction
  (an oriented box and a cylinder grow their faces outward, which also covers
  the rounded corners of the exact offset; a capsule grows its radius, which is
  the exact offset);
- ``bounding_box()`` is an axis-aligned box that contains the whole shape, used
  when a zones file asks for the ``bounding`` mode and by the obstacles export.

Nothing here looks at the robot: where a capsule sits on a link of the robot is
decided by whoever writes the zones file.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from hydra_umc_safety_zones.geometry import AABB, Point3D

# Distances to a shape are reduced by this much: floating point rounding in the rotation of a point into the frame of an oriented box is far below it.
SHAPE_EPSILON_M = 1e-9


def _finite_non_negative(value: float, name: str, *, strictly_positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if value < 0 or (strictly_positive and value == 0):
        raise ValueError(f"{name} must be {'above zero' if strictly_positive else 'not negative'}, got {value!r}")
    return float(value)


def _check_margin(margin: float) -> None:
    if not math.isfinite(margin):
        raise ValueError(f"margin must be a finite number, got {margin!r} - a NaN margin would build a shape that contains nothing")
    if margin < 0:
        raise ValueError("margin must be non-negative - a safety envelope can only grow, never shrink")


def _check_radius(radius: float) -> None:
    if not math.isfinite(radius) or radius < 0:
        raise ValueError(f"radius must be a finite, non-negative number, got {radius!r}")


def rotation_matrix(roll_deg: float, pitch_deg: float, yaw_deg: float) -> tuple[tuple[float, float, float], ...]:
    """Rotation matrix R = Rz(yaw) * Ry(pitch) * Rx(roll), angles in degrees; it maps local coordinates of a box to the cell frame."""
    cr, sr = math.cos(math.radians(roll_deg)), math.sin(math.radians(roll_deg))
    cp, sp = math.cos(math.radians(pitch_deg)), math.sin(math.radians(pitch_deg))
    cy, sy = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


@dataclass(frozen=True)
class OBB:
    """An oriented box: ``center``, half the side length along each local axis and the rotation of the local axes (roll, pitch, yaw in degrees, R = Rz*Ry*Rx)."""

    center: Point3D
    half_extents: tuple[float, float, float]
    rotation_deg: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        extents = tuple(_finite_non_negative(h, f"half extent {i}") for i, h in enumerate(self.half_extents))
        if len(extents) != 3:
            raise ValueError("an oriented box needs three half extents")
        angles = tuple(self.rotation_deg)
        if len(angles) != 3 or not all(isinstance(a, (int, float)) and not isinstance(a, bool) and math.isfinite(a) for a in angles):
            raise ValueError(f"rotation_deg must be three finite angles, got {self.rotation_deg!r}")
        object.__setattr__(self, "half_extents", extents)
        object.__setattr__(self, "rotation_deg", tuple(float(a) for a in angles))

    def _matrix(self) -> tuple[tuple[float, float, float], ...]:
        return rotation_matrix(*self.rotation_deg)

    def to_local(self, point: Point3D) -> tuple[float, float, float]:
        """The coordinates of ``point`` in the frame of the box: R^T * (point - center)."""
        r = self._matrix()
        d = (point.x - self.center.x, point.y - self.center.y, point.z - self.center.z)
        return tuple(r[0][i] * d[0] + r[1][i] * d[1] + r[2][i] * d[2] for i in range(3))  # type: ignore[return-value]

    def distance_to(self, point: Point3D) -> float:
        q = self.to_local(point)
        sq = 0.0
        for coordinate, half in zip(q, self.half_extents, strict=True):
            gap = max(abs(coordinate) - half, 0.0)
            sq += gap * gap
        return max(math.sqrt(sq) - SHAPE_EPSILON_M, 0.0)

    def contains(self, point: Point3D) -> bool:
        return self.distance_to(point) <= 0.0

    def intersects_sphere(self, center: Point3D, radius: float) -> bool:
        _check_radius(radius)
        return self.distance_to(center) <= radius

    def expanded(self, margin: float) -> "OBB":
        _check_margin(margin)
        hx, hy, hz = self.half_extents
        return OBB(self.center, (hx + margin, hy + margin, hz + margin), self.rotation_deg)

    def bounding_box(self) -> AABB:
        r = self._matrix()
        reach = [sum(abs(r[i][j]) * self.half_extents[j] for j in range(3)) + SHAPE_EPSILON_M for i in range(3)]
        c = self.center
        return AABB(Point3D(c.x - reach[0], c.y - reach[1], c.z - reach[2]), Point3D(c.x + reach[0], c.y + reach[1], c.z + reach[2]))

    def corners(self) -> list[Point3D]:
        """The eight corners of the box in the cell frame."""
        r = self._matrix()
        out = []
        for sx in (-1, 1):
            for sy in (-1, 1):
                for sz in (-1, 1):
                    local = (sx * self.half_extents[0], sy * self.half_extents[1], sz * self.half_extents[2])
                    out.append(
                        Point3D(
                            self.center.x + sum(r[0][j] * local[j] for j in range(3)),
                            self.center.y + sum(r[1][j] * local[j] for j in range(3)),
                            self.center.z + sum(r[2][j] * local[j] for j in range(3)),
                        )
                    )
        return out


@dataclass(frozen=True)
class Cylinder:
    """A vertical cylinder (its axis is the cell's Z axis): the centre of its base, its radius and its height upward from the base."""

    base_center: Point3D
    radius: float
    height: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "radius", _finite_non_negative(self.radius, "radius", strictly_positive=True))
        object.__setattr__(self, "height", _finite_non_negative(self.height, "height", strictly_positive=True))

    def distance_to(self, point: Point3D) -> float:
        radial = max(math.hypot(point.x - self.base_center.x, point.y - self.base_center.y) - self.radius, 0.0)
        z0 = self.base_center.z
        vertical = max(z0 - point.z, 0.0, point.z - (z0 + self.height))
        return max(math.hypot(radial, vertical) - SHAPE_EPSILON_M, 0.0)

    def contains(self, point: Point3D) -> bool:
        return self.distance_to(point) <= 0.0

    def intersects_sphere(self, center: Point3D, radius: float) -> bool:
        _check_radius(radius)
        return self.distance_to(center) <= radius

    def expanded(self, margin: float) -> "Cylinder":
        _check_margin(margin)
        c = self.base_center
        return Cylinder(Point3D(c.x, c.y, c.z - margin), self.radius + margin, self.height + 2 * margin)

    def bounding_box(self) -> AABB:
        c = self.base_center
        e = SHAPE_EPSILON_M
        return AABB(Point3D(c.x - self.radius - e, c.y - self.radius - e, c.z - e), Point3D(c.x + self.radius + e, c.y + self.radius + e, c.z + self.height + e))


@dataclass(frozen=True)
class Capsule:
    """A capsule: every point within ``radius`` of the segment from ``start`` to ``end`` (equal ends give a sphere)."""

    start: Point3D
    end: Point3D
    radius: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "radius", _finite_non_negative(self.radius, "radius", strictly_positive=True))

    def _distance_to_segment(self, point: Point3D) -> float:
        a, b = self.start, self.end
        ab = (b.x - a.x, b.y - a.y, b.z - a.z)
        ap = (point.x - a.x, point.y - a.y, point.z - a.z)
        length_sq = ab[0] ** 2 + ab[1] ** 2 + ab[2] ** 2
        t = 0.0 if length_sq == 0.0 else max(0.0, min(1.0, (ap[0] * ab[0] + ap[1] * ab[1] + ap[2] * ab[2]) / length_sq))
        nearest = (a.x + t * ab[0], a.y + t * ab[1], a.z + t * ab[2])
        return math.dist(nearest, (point.x, point.y, point.z))

    def distance_to(self, point: Point3D) -> float:
        return max(self._distance_to_segment(point) - self.radius - SHAPE_EPSILON_M, 0.0)

    def contains(self, point: Point3D) -> bool:
        return self.distance_to(point) <= 0.0

    def intersects_sphere(self, center: Point3D, radius: float) -> bool:
        _check_radius(radius)
        return self.distance_to(center) <= radius

    def expanded(self, margin: float) -> "Capsule":
        _check_margin(margin)
        return Capsule(self.start, self.end, self.radius + margin)

    def bounding_box(self) -> AABB:
        r = self.radius + SHAPE_EPSILON_M
        a, b = self.start, self.end
        return AABB(
            Point3D(min(a.x, b.x) - r, min(a.y, b.y) - r, min(a.z, b.z) - r),
            Point3D(max(a.x, b.x) + r, max(a.y, b.y) + r, max(a.z, b.z) + r),
        )


@dataclass(frozen=True)
class GeometrySettings:
    """The switch of the zone shapes, read from the ``geometry`` object of a zones file.

    - ``shapes`` False (the default, and what a file without ``geometry`` means): only axis-aligned boxes are understood and a zone
      that declares another shape makes the whole file invalid, exactly as an unknown value would.
    - ``mode`` ``exact`` evaluates each shape as it is; ``bounding`` replaces every shape by the smallest axis-aligned box that
      contains it, which is larger and therefore the conservative choice when in doubt.
    """

    shapes: bool = False
    mode: str = "exact"


def parse_geometry_settings(data: object) -> GeometrySettings:
    if data is None:
        return GeometrySettings()
    if not isinstance(data, dict):
        raise ValueError("geometry must be a JSON object")
    unknown = sorted(set(data) - {"shapes", "mode"})
    if unknown:
        raise ValueError(f"geometry has unknown field(s): {unknown}")
    shapes = data.get("shapes", False)
    if not isinstance(shapes, bool):
        raise ValueError(f"geometry.shapes must be true or false, got {shapes!r}")
    mode = data.get("mode", "exact")
    if mode not in ("exact", "bounding"):
        raise ValueError(f"geometry.mode must be 'exact' or 'bounding', got {mode!r}")
    return GeometrySettings(shapes=shapes, mode=mode)


Volume = AABB | OBB | Cylinder | Capsule
