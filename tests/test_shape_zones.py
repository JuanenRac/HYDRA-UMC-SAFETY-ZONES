# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_shape_zones.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Oriented boxes, cylinders and capsules in a zones file: the switch, the
refusals, the verdicts, the margins and the obstacles export."""
from __future__ import annotations

import copy
import math

import pytest

from hydra_umc_safety_zones.breach import DetectedObject, check_breaches
from hydra_umc_safety_zones.config import ConfigError, parse_zone_set, parse_zones
from hydra_umc_safety_zones.geometry import AABB, Point3D
from hydra_umc_safety_zones.obstacles import export_obstacles
from hydra_umc_safety_zones.shapes import OBB, Capsule, Cylinder, GeometrySettings, parse_geometry_settings
from hydra_umc_safety_zones.zones import SeparationModel, scale_zones_for_velocity

CAL = {"version": "c", "source": "manual", "calibrated_at": "2099-01-01", "max_age_days": 30}


def _file(*zones: dict, geometry: object = "default") -> dict:
    data: dict = {"zones": list(zones), "calibration": CAL}
    if geometry == "default":
        data["geometry"] = {"shapes": True}
    elif geometry is not None:
        data["geometry"] = geometry
    return data


OBB_ZONE = {
    "id": "diag", "level": "danger", "shape": "obb",
    "center": {"x": 0, "y": 0, "z": 1}, "half_extents": {"x": 2, "y": 0.5, "z": 1}, "rotation_deg": {"yaw": 45},
}
CYL_ZONE = {"id": "base", "level": "warning", "shape": "cylinder", "base_center": {"x": 5, "y": 0, "z": 0}, "radius": 1, "height": 2}
CAP_ZONE = {"id": "arm", "level": "danger", "shape": "capsule", "start": {"x": 0, "y": 5, "z": 1}, "end": {"x": 3, "y": 5, "z": 1}, "radius": 0.4}


def _breached(zone_set, x: float, y: float, z: float, radius: float = 0.0) -> set[str]:
    return {b.zone_id for b in check_breaches(zone_set.zones, (DetectedObject("o", Point3D(x, y, z), radius),))}


def test_the_shapes_are_off_unless_the_file_turns_them_on() -> None:
    for zone in (OBB_ZONE, CYL_ZONE, CAP_ZONE):
        for geometry in (None, {}, {"shapes": False}):
            with pytest.raises(ConfigError, match="shapes"):
                parse_zone_set(_file(zone, geometry=geometry))


def test_a_file_without_shapes_reads_exactly_as_before() -> None:
    box = {"id": "b", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 1, "y": 1, "z": 1}}
    assert parse_zone_set(_file(box, geometry=None)).zones == parse_zone_set(_file(box)).zones
    assert parse_zone_set(_file(box, geometry=None)).zones[0].volume == AABB(Point3D(0, 0, 0), Point3D(1, 1, 1))
    explicit = dict(box, shape="box")
    assert parse_zone_set(_file(explicit, geometry=None)).zones[0].volume == AABB(Point3D(0, 0, 0), Point3D(1, 1, 1))


def test_the_default_settings_are_the_current_behaviour() -> None:
    assert parse_geometry_settings(None) == GeometrySettings(shapes=False, mode="exact")


@pytest.mark.parametrize("geometry", [[], "yes", {"shapes": "true"}, {"shapes": 1}, {"mode": "loose"}, {"mode": None}, {"extra": 1}])
def test_a_bad_geometry_object_is_refused(geometry: object) -> None:
    with pytest.raises(ConfigError):
        parse_zones(_file(OBB_ZONE, geometry=geometry))


def test_the_three_shapes_parse_to_their_volumes() -> None:
    zones = parse_zone_set(_file(OBB_ZONE, CYL_ZONE, CAP_ZONE)).zones
    assert zones[0].volume == OBB(Point3D(0, 0, 1), (2, 0.5, 1), (0, 0, 45))
    assert zones[1].volume == Cylinder(Point3D(5, 0, 0), 1, 2)
    assert zones[2].volume == Capsule(Point3D(0, 5, 1), Point3D(3, 5, 1), 0.4)


def test_an_oriented_box_follows_its_rotation() -> None:
    zone_set = parse_zone_set(_file(OBB_ZONE))
    along = math.sqrt(0.5) * 1.8  # on the rotated long axis, within 2 m
    assert _breached(zone_set, along, along, 1) == {"diag"}
    assert _breached(zone_set, 1.8, -1.8, 1) == set()  # across the short axis, far outside it
    assert _breached(zone_set, 1.7, 0, 1) == set()  # inside the bounding box, outside the box


def test_a_cylinder_and_a_capsule_breach_inside_and_not_outside() -> None:
    zone_set = parse_zone_set(_file(CYL_ZONE, CAP_ZONE))
    assert _breached(zone_set, 5.9, 0, 1) == {"base"}
    assert _breached(zone_set, 5.9, 0.9, 1) == set()  # inside the bounding square, outside the circle
    assert _breached(zone_set, 6.0, 0, 2.0) == {"base"}  # the rim and the top are in
    assert _breached(zone_set, 6.0, 0, 2.01) == set()
    assert _breached(zone_set, 1.5, 5.39, 1) == {"arm"}
    assert _breached(zone_set, 3.35, 5.35, 1) == set()  # past the rounded end
    assert _breached(zone_set, 3.35, 5.35, 1, radius=0.2) == {"arm"}  # a person's arm reaches it


def test_the_bounding_mode_replaces_every_shape_by_a_box_that_contains_it() -> None:
    exact = parse_zone_set(_file(OBB_ZONE, CYL_ZONE, CAP_ZONE))
    bounding = parse_zone_set(_file(OBB_ZONE, CYL_ZONE, CAP_ZONE, geometry={"shapes": True, "mode": "bounding"}))
    assert all(isinstance(z.volume, AABB) for z in bounding.zones)
    assert _breached(exact, 1.7, 0, 1) == set()
    assert _breached(bounding, 1.7, 0, 1) == {"diag"}
    assert _breached(bounding, 5.9, 0.9, 1) == {"base"}
    for x in range(-40, 80, 3):
        for y in range(-40, 80, 3):
            for z in (0.0, 1.0, 2.0):
                assert _breached(exact, x / 10, y / 10, z) <= _breached(bounding, x / 10, y / 10, z)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda z: z.pop("half_extents"),
        lambda z: z.update(half_extents={"x": 1, "y": 1}),
        lambda z: z.update(half_extents={"x": -1, "y": 1, "z": 1}),
        lambda z: z.update(half_extents={"x": float("nan"), "y": 1, "z": 1}),
        lambda z: z.update(half_extents={"x": True, "y": 1, "z": 1}),
        lambda z: z.update(rotation_deg={"spin": 3}),
        lambda z: z.update(rotation_deg={"yaw": "45"}),
        lambda z: z.update(rotation_deg=[0, 0, 45]),
        lambda z: z.update(min={"x": 0, "y": 0, "z": 0}),
        lambda z: z.update(center={"x": 0, "y": 0}),
        lambda z: z.update(extra=1),
    ],
)
def test_a_malformed_oriented_box_is_refused(mutate) -> None:
    zone = copy.deepcopy(OBB_ZONE)
    mutate(zone)
    with pytest.raises(ConfigError):
        parse_zone_set(_file(zone))


@pytest.mark.parametrize(
    "zone",
    [
        {**CYL_ZONE, "radius": 0},
        {**CYL_ZONE, "radius": -1},
        {**CYL_ZONE, "height": 0},
        {**CYL_ZONE, "height": None},
        {**CYL_ZONE, "radius": float("inf")},
        {k: v for k, v in CYL_ZONE.items() if k != "height"},
        {**CAP_ZONE, "radius": 0},
        {**CAP_ZONE, "radius": "0.4"},
        {k: v for k, v in CAP_ZONE.items() if k != "end"},
        {**CAP_ZONE, "min": {"x": 0, "y": 0, "z": 0}},
        {**CYL_ZONE, "shape": "sphere"},
        {**CYL_ZONE, "shape": "OBB"},
    ],
)
def test_a_malformed_cylinder_or_capsule_or_an_unknown_shape_is_refused(zone: dict) -> None:
    with pytest.raises(ConfigError):
        parse_zone_set(_file(zone))


def test_a_unknown_shape_is_refused_even_with_the_shapes_off() -> None:
    with pytest.raises(ConfigError):
        parse_zone_set(_file({**CYL_ZONE, "shape": "sphere"}, geometry=None))


def test_a_nan_in_a_shape_in_the_json_text_is_refused() -> None:
    from hydra_umc_safety_zones.config import strict_json_loads

    with pytest.raises(ValueError):
        strict_json_loads('{"radius": NaN}')


def test_margins_grow_the_shapes_and_never_shrink_them() -> None:
    zone_set = parse_zone_set(_file(OBB_ZONE, CYL_ZONE, CAP_ZONE))
    assert _breached(zone_set, 6.4, 0, 1) == set()
    grown = scale_zones_for_velocity(zone_set.zones, 2.0)  # 0.3 m
    assert {b.zone_id for b in check_breaches(grown, (DetectedObject("o", Point3D(6.25, 0, 1)),))} == {"base"}
    model = SeparationModel(1.6, 0.5, 0.2)  # 1.0 m
    modelled = scale_zones_for_velocity(zone_set.zones, None, model)
    assert {b.zone_id for b in check_breaches(modelled, (DetectedObject("o", Point3D(5.9, 0, 3.0)),))} == {"base"}
    assert scale_zones_for_velocity(zone_set.zones, None) == zone_set.zones
    for zone, bigger in zip(zone_set.zones, grown, strict=True):
        assert bigger.zone_id == zone.zone_id and bigger.level is zone.level


def test_the_obstacle_spheres_cover_every_point_of_a_shape() -> None:
    zone_set = parse_zone_set(_file(OBB_ZONE, CYL_ZONE, CAP_ZONE))
    spheres = export_obstacles(zone_set.zones, "all", 0.5)["obstacles"]
    for zone in zone_set.zones:
        box = zone.volume.bounding_box()
        for i in range(0, 11):
            for j in range(0, 11):
                for k in range(0, 11):
                    point = Point3D(
                        box.min_corner.x + (box.max_corner.x - box.min_corner.x) * i / 10,
                        box.min_corner.y + (box.max_corner.y - box.min_corner.y) * j / 10,
                        box.min_corner.z + (box.max_corner.z - box.min_corner.z) * k / 10,
                    )
                    if zone.volume.contains(point):
                        assert any(
                            math.dist((s["center"]["x"], s["center"]["y"], s["center"]["z"]), (point.x, point.y, point.z)) <= s["radius"]
                            for s in spheres
                        )
