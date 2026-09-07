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
