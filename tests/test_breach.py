from hydra_umc_safety_zones.breach import DetectedObject, check_breaches, worst_level_per_object
from hydra_umc_safety_zones.geometry import AABB, Point3D
from hydra_umc_safety_zones.zones import Zone, ZoneLevel

WARNING_ZONE = Zone("w1", ZoneLevel.WARNING, AABB(Point3D(0, 0, 0), Point3D(10, 10, 10)))
DANGER_ZONE = Zone("d1", ZoneLevel.DANGER, AABB(Point3D(0, 0, 0), Point3D(2, 2, 2)))


def test_no_breach_when_object_outside_all_zones():
    obj = DetectedObject("op1", Point3D(50, 50, 50))
    breaches = check_breaches((WARNING_ZONE, DANGER_ZONE), (obj,))
    assert breaches == ()


def test_breach_in_warning_only():
    obj = DetectedObject("op1", Point3D(5, 5, 5))
    breaches = check_breaches((WARNING_ZONE, DANGER_ZONE), (obj,))
    assert len(breaches) == 1
    assert breaches[0].zone_id == "w1"
    assert breaches[0].level is ZoneLevel.WARNING


def test_object_inside_both_zones_produces_two_breaches():
    obj = DetectedObject("op1", Point3D(1, 1, 1))
    breaches = check_breaches((WARNING_ZONE, DANGER_ZONE), (obj,))
    zone_ids = {b.zone_id for b in breaches}
    assert zone_ids == {"w1", "d1"}


def test_worst_level_per_object_prefers_danger():
    obj = DetectedObject("op1", Point3D(1, 1, 1))
    breaches = check_breaches((WARNING_ZONE, DANGER_ZONE), (obj,))
    worst = worst_level_per_object(breaches)
    assert worst["op1"] is ZoneLevel.DANGER


def test_worst_level_per_object_warning_only():
    obj = DetectedObject("op1", Point3D(5, 5, 5))
    breaches = check_breaches((WARNING_ZONE, DANGER_ZONE), (obj,))
    worst = worst_level_per_object(breaches)
    assert worst["op1"] is ZoneLevel.WARNING


def test_multiple_objects_independent():
    safe = DetectedObject("safe", Point3D(50, 50, 50))
    danger = DetectedObject("intruder", Point3D(1, 1, 1))
    breaches = check_breaches((WARNING_ZONE, DANGER_ZONE), (safe, danger))
    object_ids = {b.object_id for b in breaches}
    assert object_ids == {"intruder"}
