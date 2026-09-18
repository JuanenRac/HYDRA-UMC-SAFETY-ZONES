import pytest

from hydra_umc_safety_zones.geometry import AABB, Point3D


def test_contains_inside():
    box = AABB(Point3D(0, 0, 0), Point3D(2, 2, 2))
    assert box.contains(Point3D(1, 1, 1))


def test_contains_outside():
    box = AABB(Point3D(0, 0, 0), Point3D(2, 2, 2))
    assert not box.contains(Point3D(3, 1, 1))
    assert not box.contains(Point3D(1, -1, 1))


def test_contains_boundary_is_inside():
    box = AABB(Point3D(0, 0, 0), Point3D(2, 2, 2))
    assert box.contains(Point3D(0, 1, 1))
    assert box.contains(Point3D(2, 2, 2))


def test_corners_are_normalized_regardless_of_order():
    box = AABB(Point3D(2, 2, 2), Point3D(0, 0, 0))
    assert box.min_corner == Point3D(0, 0, 0)
    assert box.max_corner == Point3D(2, 2, 2)
    assert box.contains(Point3D(1, 1, 1))


def test_expanded_grows_every_axis_by_the_margin():
    box = AABB(Point3D(0, 0, 0), Point3D(2, 2, 2))
    grown = box.expanded(0.5)
    assert grown.min_corner == Point3D(-0.5, -0.5, -0.5)
    assert grown.max_corner == Point3D(2.5, 2.5, 2.5)


def test_expanded_zero_margin_leaves_extent_unchanged():
    box = AABB(Point3D(0, 0, 0), Point3D(2, 2, 2))
    grown = box.expanded(0)
    assert grown.min_corner == box.min_corner
    assert grown.max_corner == box.max_corner


def test_expanded_a_point_now_outside_the_original_box_is_inside_the_grown_one():
    box = AABB(Point3D(0, 0, 0), Point3D(2, 2, 2))
    assert not box.contains(Point3D(2.3, 1, 1))
    grown = box.expanded(0.5)
    assert grown.contains(Point3D(2.3, 1, 1))


def test_expanded_rejects_a_negative_margin():
    box = AABB(Point3D(0, 0, 0), Point3D(2, 2, 2))
    with pytest.raises(ValueError):
        box.expanded(-0.1)
