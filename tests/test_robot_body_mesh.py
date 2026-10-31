# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_robot_body_mesh.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Proof by harness that the robot body never under-estimates the robot.

For seeded random segments (random ends, radius, frame, margin and resolution) the
test builds a dense cloud of points that lie inside the capsule of the segment by
construction, with arithmetic that does not call the code under test, moves it to
the frame of the zones with its own rotation matrix, and puts a random zone (a
box, an oriented box, a cylinder or a capsule) around one of those points. The
body must then report a breach with that zone, every time.

A second property keeps the answer useful: a zone whose true distance from the
axis of the segment is more than the radius, the margin, half a piece and the
allowance must not be reported (the over-estimate is bounded, not unlimited).

A control test shows the harness fails a body whose radius is shrunk."""
from __future__ import annotations

import math
import random

from hydra_umc_safety_zones.geometry import AABB, Point3D
from hydra_umc_safety_zones.robot_body import (
    BodySegment,
    RobotBodySettings,
    RobotFrame,
    build_robot_body,
    check_body_breaches,
    segment_spheres,
)
from hydra_umc_safety_zones.shapes import OBB, Capsule, Cylinder
from hydra_umc_safety_zones.zones import Zone, ZoneLevel

SEED = 20261031
TRIALS = 160


def _rx(a: float) -> list[list[float]]:
    c, s = math.cos(a), math.sin(a)
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def _ry(a: float) -> list[list[float]]:
    c, s = math.cos(a), math.sin(a)
    return [[c, 0, s], [0, 1, 0], [-s, 0, c]]


def _rz(a: float) -> list[list[float]]:
    c, s = math.cos(a), math.sin(a)
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def _mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _move(r: list[list[float]], t: tuple[float, float, float], p: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(sum(r[i][j] * p[j] for j in range(3)) + t[i] for i in range(3))  # type: ignore[return-value]


def _unit(rng: random.Random) -> tuple[float, float, float]:
    while True:
        v = (rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1))
        n = math.sqrt(sum(c * c for c in v))
        if 0.1 < n <= 1.0:
            return (v[0] / n, v[1] / n, v[2] / n)


def _mesh(rng: random.Random, a: tuple[float, float, float], b: tuple[float, float, float], radius: float) -> list[tuple[float, float, float]]:
    """Points inside the capsule, built from its definition: a point of the axis plus a vector shorter than the radius.
    The ends of the axis and the full radius are always included, so the outer shell is covered."""
    points = []
    for i in range(41):
        t = i / 40
        axis = tuple(a[k] + t * (b[k] - a[k]) for k in range(3))
        points.append(axis)
        for _ in range(6):
            d = _unit(rng)
            reach = radius * (1.0 if rng.random() < 0.5 else rng.random() ** (1 / 3))
            points.append(tuple(axis[k] + reach * d[k] for k in range(3)))  # type: ignore[arg-type]
    return points


def _zone_around(rng: random.Random, p: tuple[float, float, float]) -> Zone:
    kind = rng.choice(["box", "obb", "cylinder", "capsule"])
    c = Point3D(*p)

    def grow(lo: float, hi: float) -> float:
        return rng.uniform(lo, hi)

    if kind == "box":
        e = [grow(0.001, 0.15) for _ in range(3)]
        vol = AABB(Point3D(c.x - e[0], c.y - e[1], c.z - e[2]), Point3D(c.x + grow(0.001, 0.15), c.y + grow(0.001, 0.15), c.z + grow(0.001, 0.15)))
    elif kind == "obb":
        vol = OBB(c, (grow(0.002, 0.15), grow(0.002, 0.15), grow(0.002, 0.15)), (rng.uniform(-180, 180), rng.uniform(-90, 90), rng.uniform(-180, 180)))
    elif kind == "cylinder":
        h = grow(0.01, 0.3)
        vol = Cylinder(Point3D(c.x + grow(-0.004, 0.004), c.y + grow(-0.004, 0.004), c.z - rng.uniform(0.001, 1.0) * h), grow(0.01, 0.2), h + 0.002)
    else:
        d = _unit(rng)
        s = grow(0.0, 0.3)
        vol = Capsule(c, Point3D(c.x + s * d[0], c.y + s * d[1], c.z + s * d[2]), grow(0.002, 0.1))
    return Zone("z", ZoneLevel.DANGER, vol)


def _trial(rng: random.Random):
    a = tuple(rng.uniform(-2, 2) for _ in range(3))
    b = tuple(rng.uniform(-2, 2) for _ in range(3))
    radius = rng.uniform(0.02, 0.4)
    rot = (rng.uniform(-math.pi, math.pi), rng.uniform(-1.5, 1.5), rng.uniform(-math.pi, math.pi))
    t = (rng.uniform(-3, 3), rng.uniform(-3, 3), rng.uniform(-3, 3))
    r = _mul(_rz(rot[2]), _mul(_ry(rot[1]), _rx(rot[0])))
    settings = RobotBodySettings(enabled=True, resolution_m=rng.uniform(0.02, 0.4), margin_m=rng.choice([0.0, 0.0, 0.03]), max_pieces_per_segment=rng.choice([200, 3]))
    frame = RobotFrame(t, tuple(math.degrees(x) for x in rot))
    body = build_robot_body((BodySegment("s", Point3D(*a), Point3D(*b), radius),), frame, settings)
    mesh = [_move(r, t, p) for p in _mesh(rng, a, b, radius)]
    return body, settings, mesh, radius, (_move(r, t, a), _move(r, t, b))


def _misses(shrink: float) -> tuple[int, int]:
    rng = random.Random(SEED)
    misses = 0
    checked = 0
    for _ in range(TRIALS):
        body, settings, mesh, radius, _ends = _trial(rng)
        if shrink != 1.0:
            seg = body.segments[0]
            body = type(body)((BodySegment(seg.segment_id, seg.start, seg.end, seg.radius_m * shrink),), body.allowance_m)
            settings = RobotBodySettings(enabled=True, resolution_m=0.005, margin_m=0.0, max_pieces_per_segment=1000)
        for _ in range(4):
            zone = _zone_around(rng, rng.choice(mesh))
            inside = [p for p in mesh if zone.volume.contains(Point3D(*p))]
            if not inside:
                continue
            checked += 1
            if not check_body_breaches((zone,), body, settings):
                misses += 1
    return misses, checked


def test_a_zone_around_any_point_of_the_robot_is_always_a_breach() -> None:
    misses, checked = _misses(1.0)
    assert checked > 400  # the property was really exercised
    assert misses == 0


def test_the_control_shows_the_harness_fails_a_body_shrunk_to_sixty_percent() -> None:
    misses, checked = _misses(0.6)
    assert checked > 100
    assert misses > 0


def _box_distance(p: tuple[float, float, float], lo: tuple[float, float, float], hi: tuple[float, float, float]) -> float:
    return math.sqrt(sum(max(lo[k] - p[k], 0.0, p[k] - hi[k]) ** 2 for k in range(3)))


def test_a_zone_clearly_beyond_the_body_is_not_reported() -> None:
    rng = random.Random(SEED + 1)
    far = 0
    for _ in range(TRIALS):
        body, settings, _mesh_points, _radius, (a, b) = _trial(rng)
        spheres = segment_spheres(body.segments[0], body, settings)
        reach = spheres[0][1]
        lo = tuple(rng.uniform(-6, 6) for _ in range(3))
        hi = tuple(lo[k] + rng.uniform(0.05, 1.5) for k in range(3))
        sampled = min(_box_distance(tuple(a[k] + (i / 2000) * (b[k] - a[k]) for k in range(3)), lo, hi) for i in range(2001))  # type: ignore[arg-type]
        if sampled - 0.01 <= reach:
            continue
        far += 1
        zone = Zone("z", ZoneLevel.DANGER, AABB(Point3D(*lo), Point3D(*hi)))
        assert check_body_breaches((zone,), body, settings) == ()
    assert far > 60
