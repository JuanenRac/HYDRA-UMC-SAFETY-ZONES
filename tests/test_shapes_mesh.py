# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_shapes_mesh.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Proof by harness that the shapes never under-estimate a zone.

For every shape the test builds a dense mesh of points that are, by
construction, inside the solid (the mesh is generated from the definition of the
shape with its own, independent arithmetic: it never calls the code under
test). The shape must then:

- contain every mesh point and every corner;
- report a breach for any sphere that reaches a mesh point;
- after growing by a margin, contain every point within that margin of a mesh point;
- have a bounding box that contains the whole mesh.

The shapes are drawn from a seeded random generator (the same ones every run),
because the repository has no runtime dependencies to add a property-testing
library for."""
from __future__ import annotations

import math
import random

import pytest

from hydra_umc_safety_zones.geometry import Point3D
from hydra_umc_safety_zones.shapes import OBB, Capsule, Cylinder

SEED = 20260101
SHAPES_PER_KIND = 25


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


def _apply(m: list[list[float]], v: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


def _fibonacci_sphere(n: int) -> list[tuple[float, float, float]]:
    golden = math.pi * (3.0 - math.sqrt(5.0))
    out = []
    for i in range(n):
        y = 1 - 2 * (i + 0.5) / n
        r = math.sqrt(max(0.0, 1 - y * y))
        out.append((math.cos(golden * i) * r, y, math.sin(golden * i) * r))
    return out


def _linspace(lo: float, hi: float, n: int) -> list[float]:
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


def _obb_mesh(shape: OBB) -> list[tuple[float, float, float]]:
    roll, pitch, yaw = (math.radians(a) for a in shape.rotation_deg)
    matrix = _mul(_rz(yaw), _mul(_ry(pitch), _rx(roll)))
    hx, hy, hz = shape.half_extents
    points = []
    for x in _linspace(-hx, hx, 6):
        for y in _linspace(-hy, hy, 6):
            for z in _linspace(-hz, hz, 6):
                wx, wy, wz = _apply(matrix, (x, y, z))
                points.append((shape.center.x + wx, shape.center.y + wy, shape.center.z + wz))
    return points


def _cylinder_mesh(shape: Cylinder) -> list[tuple[float, float, float]]:
    points = []
    c = shape.base_center
    for z in _linspace(c.z, c.z + shape.height, 6):
        points.append((c.x, c.y, z))
        for r in _linspace(shape.radius / 3, shape.radius, 3):
            for k in range(24):
                a = 2 * math.pi * k / 24
                points.append((c.x + r * math.cos(a), c.y + r * math.sin(a), z))
    return points


def _capsule_mesh(shape: Capsule) -> list[tuple[float, float, float]]:
    directions = _fibonacci_sphere(60)
    points = []
    for t in _linspace(0.0, 1.0, 7):
        px = shape.start.x + t * (shape.end.x - shape.start.x)
        py = shape.start.y + t * (shape.end.y - shape.start.y)
        pz = shape.start.z + t * (shape.end.z - shape.start.z)
        points.append((px, py, pz))
        for r in (shape.radius * 0.5, shape.radius):
            points.extend((px + r * d[0], py + r * d[1], pz + r * d[2]) for d in directions)
    return points


def _random_point(rng: random.Random, span: float) -> Point3D:
    return Point3D(rng.uniform(-span, span), rng.uniform(-span, span), rng.uniform(-span, span))


def _probe(rng: random.Random, mesh: list[tuple[float, float, float]]) -> Point3D:
    """Half of the probes are anywhere in the cell, half are within 1.5 m of a point of the solid, so that many of them really reach it."""
    if rng.random() < 0.5:
        return _random_point(rng, 6)
    x, y, z = rng.choice(mesh)
    return Point3D(x + rng.uniform(-1.5, 1.5), y + rng.uniform(-1.5, 1.5), z + rng.uniform(-1.5, 1.5))


def _random_shapes() -> list[tuple[str, object, list[tuple[float, float, float]]]]:
    rng = random.Random(SEED)
    out: list[tuple[str, object, list[tuple[float, float, float]]]] = []
    for _ in range(SHAPES_PER_KIND):
        obb = OBB(
            _random_point(rng, 3),
            (rng.uniform(0.05, 2), rng.uniform(0.05, 2), rng.uniform(0.05, 2)),
            (rng.uniform(-180, 180), rng.uniform(-90, 90), rng.uniform(-180, 180)),
        )
        out.append(("obb", obb, _obb_mesh(obb)))
        cyl = Cylinder(_random_point(rng, 3), rng.uniform(0.05, 2), rng.uniform(0.05, 3))
        out.append(("cylinder", cyl, _cylinder_mesh(cyl)))
        cap = Capsule(_random_point(rng, 3), _random_point(rng, 3), rng.uniform(0.05, 1.5))
        out.append(("capsule", cap, _capsule_mesh(cap)))
    return out


SHAPES = _random_shapes()
IDS = [f"{kind}-{i // 3}" for i, (kind, _, _) in enumerate(SHAPES)]


@pytest.mark.parametrize(("kind", "shape", "mesh"), SHAPES, ids=IDS)
def test_every_point_of_the_solid_is_inside_the_shape(kind: str, shape, mesh) -> None:
    missed = [p for p in mesh if not shape.contains(Point3D(*p))]
    assert missed == []


@pytest.mark.parametrize(("kind", "shape", "mesh"), SHAPES, ids=IDS)
def test_the_bounding_box_contains_the_whole_solid(kind: str, shape, mesh) -> None:
    box = shape.bounding_box()
    assert all(box.contains(Point3D(*p)) for p in mesh)


@pytest.mark.parametrize(("kind", "shape", "mesh"), SHAPES, ids=IDS)
def test_a_sphere_that_reaches_the_solid_is_a_breach(kind: str, shape, mesh) -> None:
    rng = random.Random(SEED + 1)
    for _ in range(400):
        probe = _probe(rng, mesh)
        radius = rng.choice([0.0, 0.1, 0.4, 1.0])
        nearest = min(math.dist((probe.x, probe.y, probe.z), m) for m in mesh)
        if nearest <= radius:  # the probes that do not reach the mesh say nothing; the ones that do must all be breaches
            assert shape.intersects_sphere(probe, radius), (probe, radius, nearest)


@pytest.mark.parametrize(("kind", "shape", "mesh"), SHAPES, ids=IDS)
def test_growing_a_shape_covers_everything_within_the_margin_of_the_solid(kind: str, shape, mesh) -> None:
    rng = random.Random(SEED + 2)
    for margin in (0.0, 0.15, 0.5, 1.7):
        grown = shape.expanded(margin)
        for _ in range(250):
            probe = _probe(rng, mesh)
            nearest = min(math.dist((probe.x, probe.y, probe.z), m) for m in mesh)
            if nearest <= margin:
                assert grown.contains(probe), (margin, probe, nearest)


@pytest.mark.parametrize(("kind", "shape", "mesh"), SHAPES, ids=IDS)
def test_growing_never_uncovers_a_point(kind: str, shape, mesh) -> None:
    rng = random.Random(SEED + 3)
    grown = shape.expanded(0.3)
    for _ in range(300):
        probe = _probe(rng, mesh)
        if shape.contains(probe):
            assert grown.contains(probe)


def test_the_probes_actually_reach_the_solids() -> None:
    """The checks above are vacuous if no probe ever touches a mesh: count that many do, over all the shapes."""
    rng = random.Random(SEED + 1)
    reached = 0
    for _, _, mesh in SHAPES[:9]:
        for _ in range(400):
            probe = _probe(rng, mesh)
            radius = rng.choice([0.0, 0.1, 0.4, 1.0])
            reached += min(math.dist((probe.x, probe.y, probe.z), m) for m in mesh) <= radius
    assert reached > 400


def test_the_corners_of_a_rotated_box_are_inside() -> None:
    rng = random.Random(SEED + 4)
    for _ in range(200):
        box = OBB(
            _random_point(rng, 5),
            (rng.uniform(0.0, 3), rng.uniform(0.0, 3), rng.uniform(0.0, 3)),
            (rng.uniform(-720, 720), rng.uniform(-720, 720), rng.uniform(-720, 720)),
        )
        assert all(box.contains(corner) for corner in box.corners())


def test_far_points_are_not_breaches() -> None:
    """The shapes are not absurdly large either: a point farther than the bounding box is clear."""
    for _, shape, _ in SHAPES:
        box = shape.bounding_box()
        far = Point3D(box.max_corner.x + 1.0, box.max_corner.y + 1.0, box.max_corner.z + 1.0)
        assert not shape.intersects_sphere(far, 0.5)


def test_an_exact_shape_is_tighter_than_its_bounding_box_but_never_the_other_way() -> None:
    box = OBB(Point3D(0, 0, 0), (1, 1, 1), (0, 0, 45))
    corner_of_the_bounding_box = Point3D(1.3, 1.3, 0)
    assert not box.contains(corner_of_the_bounding_box)
    assert box.bounding_box().contains(corner_of_the_bounding_box)


def test_the_harness_catches_a_shape_that_is_too_small() -> None:
    """A control: a box shrunk by a tenth must fail the very check the real shapes pass."""
    real = SHAPES[0][1]
    assert isinstance(real, OBB)
    mesh = SHAPES[0][2]
    shrunk = OBB(real.center, tuple(h * 0.9 for h in real.half_extents), real.rotation_deg)  # type: ignore[arg-type]
    assert any(not shrunk.contains(Point3D(*p)) for p in mesh)
