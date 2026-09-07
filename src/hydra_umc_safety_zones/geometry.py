# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/geometry.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Real 3D geometry primitives: points and axis-aligned bounding volumes.

Deliberately hardware-independent: everything here operates on plain
(x, y, z) floats, regardless of whether they came from a real Hailo-8
spatial segmentation pipeline or a test fixture. The Hailo-8 dependency
this project's README describes lives entirely upstream, in how those
coordinates get produced - not in whether a point is inside a box.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Point3D:
    """A point in the robot cell's shared 3D workspace frame, in meters."""

    x: float
    y: float
    z: float


@dataclass(frozen=True)
class AABB:
    """An axis-aligned bounding box: the simplest volume that can represent
    a real Warning/Danger perimeter without needing a full mesh or convex
    hull. `min_corner` and `max_corner` are opposite corners; which corner
    is "min" per axis is resolved in `__post_init__` so callers never have
    to get the ordering right themselves.
    """

    min_corner: Point3D
    max_corner: Point3D

    def __post_init__(self) -> None:
        lo = Point3D(
            min(self.min_corner.x, self.max_corner.x),
            min(self.min_corner.y, self.max_corner.y),
            min(self.min_corner.z, self.max_corner.z),
        )
        hi = Point3D(
            max(self.min_corner.x, self.max_corner.x),
            max(self.min_corner.y, self.max_corner.y),
            max(self.min_corner.z, self.max_corner.z),
        )
        object.__setattr__(self, "min_corner", lo)
        object.__setattr__(self, "max_corner", hi)

    def contains(self, point: Point3D) -> bool:
        """Inclusive on every axis: a point exactly on the boundary counts
        as inside. For a safety perimeter, treating the edge as "in" is the
        conservative choice - it can only cause an earlier breach report,
        never a missed one.
        """
        return (
            self.min_corner.x <= point.x <= self.max_corner.x
            and self.min_corner.y <= point.y <= self.max_corner.y
            and self.min_corner.z <= point.z <= self.max_corner.z
        )
