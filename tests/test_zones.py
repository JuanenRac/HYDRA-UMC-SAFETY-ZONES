import pytest

from hydra_umc_safety_zones.geometry import AABB, Point3D
from hydra_umc_safety_zones.zones import (
    VELOCITY_MARGIN_MAX_M,
    Zone,
    ZoneLevel,
    scale_zones_for_velocity,
    velocity_margin_m,
)

DANGER_ZONE = Zone("danger1", ZoneLevel.DANGER, AABB(Point3D(0, 0, 0), Point3D(2, 2, 2)))
WARNING_ZONE = Zone("warn1", ZoneLevel.WARNING, AABB(Point3D(0, 0, 0), Point3D(10, 10, 10)))


def test_velocity_margin_is_zero_at_zero_velocity():
    assert velocity_margin_m(0.0) == 0.0


def test_velocity_margin_grows_linearly_below_the_cap():
    assert velocity_margin_m(1.0) == pytest.approx(0.15)
    assert velocity_margin_m(2.0) == pytest.approx(0.30)


def test_velocity_margin_is_capped():
    assert velocity_margin_m(100.0) == VELOCITY_MARGIN_MAX_M


def test_velocity_margin_rejects_negative_velocity():
    with pytest.raises(ValueError):
        velocity_margin_m(-1.0)


def test_scale_zones_for_velocity_none_returns_the_exact_same_zones_unchanged():
    # Fail-safe floor: omitted velocity must be indistinguishable from the
    # static, pre-existing behavior - not merely "close", the SAME objects.
    zones = (WARNING_ZONE, DANGER_ZONE)
    assert scale_zones_for_velocity(zones, None) is zones


def test_scale_zones_for_velocity_zero_velocity_leaves_volumes_unchanged():
    scaled = scale_zones_for_velocity((DANGER_ZONE,), 0.0)
    assert scaled[0].volume.min_corner == DANGER_ZONE.volume.min_corner
    assert scaled[0].volume.max_corner == DANGER_ZONE.volume.max_corner


def test_scale_zones_for_velocity_grows_the_volume_and_keeps_id_and_level():
    scaled = scale_zones_for_velocity((DANGER_ZONE,), 2.0)
    zone = scaled[0]
    assert zone.zone_id == DANGER_ZONE.zone_id
    assert zone.level == DANGER_ZONE.level
    margin = velocity_margin_m(2.0)
    assert zone.volume.min_corner == Point3D(-margin, -margin, -margin)
    assert zone.volume.max_corner == Point3D(2 + margin, 2 + margin, 2 + margin)


def test_scale_zones_for_velocity_never_shrinks_below_the_static_extent():
    # A point exactly on the original static boundary must still be inside
    # the scaled zone for any non-negative velocity - the static volume is
    # the unconditional floor.
    for velocity in (0.0, 0.5, 5.0, 500.0):
        scaled = scale_zones_for_velocity((DANGER_ZONE,), velocity)
        assert scaled[0].volume.contains(Point3D(2, 2, 2))
        assert scaled[0].volume.contains(Point3D(0, 0, 0))
