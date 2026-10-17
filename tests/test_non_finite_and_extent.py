# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_non_finite_and_extent.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""A NaN must never switch a zone off, and an object has an extent: a person whose centre is outside a zone but whose arm reaches into it breaches the zone."""
from __future__ import annotations

import math

import pytest

from hydra_umc_safety_zones.breach import DetectedObject, check_breaches
from hydra_umc_safety_zones.config import parse_detections
from hydra_umc_safety_zones.geometry import AABB, Point3D
from hydra_umc_safety_zones.zones import Zone, ZoneLevel, scale_zones_for_velocity, velocity_margin_m

BOX = AABB(Point3D(0, 0, 0), Point3D(1, 1, 1))
ZONE = Zone("cell", ZoneLevel.DANGER, BOX)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_point_needs_finite_coordinates(bad):
    with pytest.raises(ValueError, match="finite"):
        Point3D(bad, 0, 0)
    with pytest.raises(ValueError, match="finite"):
        Point3D(0, bad, 0)
    with pytest.raises(ValueError, match="finite"):
        Point3D(0, 0, bad)


def test_a_zone_cannot_be_built_with_a_nan_corner_that_would_switch_it_off():
    with pytest.raises(ValueError):
        AABB(Point3D(math.nan, 0, 0), Point3D(1, 1, 1))


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_the_velocity_margin_refuses_a_value_that_is_not_a_finite_number(bad):
    with pytest.raises(ValueError, match="finite"):
        velocity_margin_m(bad)
    with pytest.raises(ValueError, match="finite"):
        scale_zones_for_velocity((ZONE,), bad)


def test_expanding_by_nan_is_refused_not_a_box_of_nan():
    with pytest.raises(ValueError, match="finite"):
        BOX.expanded(math.nan)
    with pytest.raises(ValueError, match="finite"):
        BOX.expanded(math.inf)


def test_the_zones_stay_active_for_a_normal_velocity():
    scaled = scale_zones_for_velocity((ZONE,), 1.0)
    assert scaled[0].volume.contains(Point3D(0.5, 0.5, 0.5))
    assert scaled[0].volume.contains(Point3D(1.1, 0.5, 0.5))   # grown by 0.15 m


def test_an_object_with_a_nan_position_cannot_exist():
    with pytest.raises(ValueError):
        DetectedObject("person", Point3D(math.nan, 0, 0))


# ---- the extent of an object ----

OUTSIDE = Point3D(1.2, 0.5, 0.5)   # 0.2 m beyond the +x face


def test_a_point_object_outside_the_zone_does_not_breach_it():
    assert check_breaches((ZONE,), (DetectedObject("person", OUTSIDE),)) == ()


def test_the_same_object_with_a_radius_that_reaches_the_zone_breaches_it():
    breaches = check_breaches((ZONE,), (DetectedObject("person", OUTSIDE, radius_m=0.3),))
    assert [(b.zone_id, b.object_id) for b in breaches] == [("cell", "person")]


def test_touching_counts_and_a_radius_that_falls_short_does_not():
    assert check_breaches((ZONE,), (DetectedObject("a", OUTSIDE, radius_m=0.2),)) != ()      # exactly touching: inclusive, like contains()
    assert check_breaches((ZONE,), (DetectedObject("a", OUTSIDE, radius_m=0.19),)) == ()


def test_a_radius_is_measured_to_the_nearest_point_of_the_box_in_every_direction():
    corner = Point3D(1.1, 1.1, 1.1)                         # sqrt(3) * 0.1 = 0.1732 m from the corner
    assert check_breaches((ZONE,), (DetectedObject("a", corner, radius_m=0.18),)) != ()
    assert check_breaches((ZONE,), (DetectedObject("a", corner, radius_m=0.17),)) == ()


def test_radius_zero_is_exactly_contains():
    for point in (Point3D(0.5, 0.5, 0.5), Point3D(1, 1, 1), Point3D(1.0001, 0.5, 0.5), Point3D(-1, 0, 0)):
        assert (check_breaches((ZONE,), (DetectedObject("a", point),)) != ()) == BOX.contains(point)


@pytest.mark.parametrize("bad", [-0.1, math.nan, math.inf, True, "0.3", None])
def test_a_bad_radius_is_refused(bad):
    with pytest.raises(ValueError, match="radius_m"):
        DetectedObject("a", OUTSIDE, radius_m=bad)


def test_the_json_edge_reads_radiusM_and_refuses_a_bad_one():
    objects = parse_detections({"objects": [{"id": "p", "position": {"x": 1.2, "y": 0.5, "z": 0.5}, "radiusM": 0.3}, {"id": "q", "position": {"x": 5, "y": 5, "z": 5}}]})
    assert [o.radius_m for o in objects] == [0.3, 0.0]
    for bad in (-1, "x", True, float("nan")):
        with pytest.raises(ValueError):
            parse_detections({"objects": [{"id": "p", "position": {"x": 0, "y": 0, "z": 0}, "radiusM": bad}]})
