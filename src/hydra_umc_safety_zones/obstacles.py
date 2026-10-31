# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/obstacles.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Exports the zones as sphere obstacles in the scenario format of a path planner.

A path planner that only knows spheres can avoid a zone when the zone is covered by spheres. Every box is cut into cubes of side
``cell_m`` and each cube is covered by its circumscribed sphere, so the union of the spheres always contains the whole box: the
export can only over-cover a zone, never leave a corner of it free. A smaller cell hugs the box more tightly and makes more
spheres; the number of spheres is capped so that a mistyped cell cannot produce an unusable file.
"""
from __future__ import annotations

import math

from hydra_umc_safety_zones.zones import Zone, ZoneLevel, scale_zones_for_velocity, SeparationModel

DEFAULT_CELL_M = 0.5
MAX_OBSTACLES = 5000
LEVEL_CHOICES = ("all", "danger", "warning")


def _axis_cells(lo: float, hi: float, cell_m: float) -> list[tuple[float, float]]:
    """Splits [lo, hi] into equal intervals no longer than ``cell_m`` (one interval for a flat box)."""
    count = max(1, math.ceil((hi - lo) / cell_m - 1e-9))
    step = (hi - lo) / count
    return [(lo + i * step, lo + (i + 1) * step) for i in range(count)]


def zone_obstacles(zone: Zone, cell_m: float = DEFAULT_CELL_M) -> list[dict]:
    """The spheres that together cover the volume of one zone, as ``{"center": {...}, "radius": r}`` objects."""
    if isinstance(cell_m, bool) or not isinstance(cell_m, (int, float)) or not math.isfinite(cell_m) or cell_m <= 0:
        raise ValueError(f"cell_m must be a finite number above zero, got {cell_m!r}")
    # a shape that is not a box is covered through the axis-aligned box that contains it: larger, never smaller
    box = zone.volume.bounding_box()
    lo, hi = box.min_corner, box.max_corner
    xs, ys, zs = _axis_cells(lo.x, hi.x, cell_m), _axis_cells(lo.y, hi.y, cell_m), _axis_cells(lo.z, hi.z, cell_m)
    if len(xs) * len(ys) * len(zs) > MAX_OBSTACLES:
        raise ValueError(f"zone '{zone.zone_id}' would need more than {MAX_OBSTACLES} spheres at cell {cell_m} m: use a larger cell")
    out = []
    for x0, x1 in xs:
        for y0, y1 in ys:
            for z0, z1 in zs:
                half = (x1 - x0, y1 - y0, z1 - z0)
                radius = 0.5 * math.sqrt(sum(h * h for h in half))
                # the radius is rounded UP so that rounding for the file never uncovers a corner
                radius = math.ceil(radius * 1e6) / 1e6
                out.append(
                    {
                        "center": {"x": (x0 + x1) / 2, "y": (y0 + y1) / 2, "z": (z0 + z1) / 2},
                        "radius": radius,
                    }
                )
    return out


def export_obstacles(
    zones: tuple[Zone, ...],
    level: str = "all",
    cell_m: float = DEFAULT_CELL_M,
    tool_velocity_mps: float | None = None,
    separation: SeparationModel | None = None,
) -> dict:
    """The ``obstacles`` list for a planner scenario. ``level`` selects ``all`` zones, only ``danger`` or only ``warning``; with a
    tool velocity (or a separation model) the zones first grow by the same margin the check applies."""
    if level not in LEVEL_CHOICES:
        raise ValueError(f"level must be one of {LEVEL_CHOICES}, got {level!r}")
    grown = scale_zones_for_velocity(zones, tool_velocity_mps, separation)
    wanted = [z for z in grown if level == "all" or z.level is ZoneLevel(level)]
    obstacles: list[dict] = []
    for zone in wanted:
        obstacles.extend(zone_obstacles(zone, cell_m))
        if len(obstacles) > MAX_OBSTACLES:
            raise ValueError(f"the zones need more than {MAX_OBSTACLES} spheres at cell {cell_m} m: use a larger cell")
    return {"obstacles": obstacles}
