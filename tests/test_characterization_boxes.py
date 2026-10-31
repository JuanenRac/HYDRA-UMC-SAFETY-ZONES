# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_characterization_boxes.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Characterization of the behaviour of axis-aligned box zones.

These tests pin what the service does today with the zones it has always
understood, so that any later change to the geometry (new shapes behind a
switch) can be shown not to move a single verdict of an existing zones file."""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from pathlib import Path

from hydra_umc_safety_zones.breach import DetectedObject, check_breaches
from hydra_umc_safety_zones.config import parse_zone_set
from hydra_umc_safety_zones.geometry import AABB, Point3D
from hydra_umc_safety_zones.main import main
from hydra_umc_safety_zones.obstacles import export_obstacles
from hydra_umc_safety_zones.zones import SeparationModel, Zone, ZoneLevel, scale_zones_for_velocity

_ZONES_JSON = {
    "zones": [
        {"id": "warn", "level": "warning", "min": {"x": -1, "y": -1, "z": 0}, "max": {"x": 4, "y": 3, "z": 2.5}},
        {"id": "danger", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 2, "y": 1.5, "z": 1}},
    ],
    "calibration": {"version": "c1", "source": "manual", "calibrated_at": "2099-01-01", "max_age_days": 30},
}


def _reference_inside(box: dict, p: tuple[float, float, float], radius: float) -> bool:
    """An independent statement of the rule: the sphere touches the box when the
    distance from its centre to the box is at most the radius (inclusive)."""
    d2 = 0.0
    for axis, value in zip("xyz", p, strict=True):
        lo, hi = box["min"][axis], box["max"][axis]
        gap = max(lo - value, 0.0, value - hi)
        d2 += gap * gap
    return d2 <= radius * radius


def test_a_grid_of_points_breaches_exactly_the_zones_the_box_rule_says() -> None:
    zone_set = parse_zone_set(_ZONES_JSON)
    axes = [x / 4 for x in range(-8, 21)]
    mismatches = 0
    for x, y, z in itertools.product(axes[::2], axes[::2], axes[::3]):
        for radius in (0.0, 0.3):
            obj = DetectedObject("o", Point3D(x, y, z), radius)
            got = {b.zone_id for b in check_breaches(zone_set.zones, (obj,))}
            want = {e["id"] for e in _ZONES_JSON["zones"] if _reference_inside(e, (x, y, z), radius)}
            mismatches += got != want
    assert mismatches == 0


def test_parsing_a_box_file_gives_axis_aligned_boxes_in_order() -> None:
    zone_set = parse_zone_set(_ZONES_JSON)
    assert [z.zone_id for z in zone_set.zones] == ["warn", "danger"]
    assert zone_set.zones[1].volume == AABB(Point3D(0, 0, 0), Point3D(2, 1.5, 1))
    assert zone_set.zones[0].level is ZoneLevel.WARNING


def test_the_velocity_and_separation_margins_grow_boxes_as_before() -> None:
    zone = Zone("z", ZoneLevel.DANGER, AABB(Point3D(0, 0, 0), Point3D(1, 1, 1)))
    (linear,) = scale_zones_for_velocity((zone,), 2.0)
    assert linear.volume == AABB(Point3D(-0.3, -0.3, -0.3), Point3D(1.3, 1.3, 1.3))
    model = SeparationModel(1.6, 0.5, 0.2)
    (grown,) = scale_zones_for_velocity((zone,), None, model)
    assert grown.volume == AABB(Point3D(-1.0, -1.0, -1.0), Point3D(2.0, 2.0, 2.0))


def test_the_obstacles_export_of_a_box_is_unchanged() -> None:
    zone = Zone("z", ZoneLevel.DANGER, AABB(Point3D(0, 0, 0), Point3D(1, 1, 0.5)))
    exported = export_obstacles((zone,), "all", 0.5)
    assert len(exported["obstacles"]) == 4
    first = exported["obstacles"][0]
    assert first["center"] == {"x": 0.25, "y": 0.25, "z": 0.25}
    assert first["radius"] == 0.433013  # 0.5 * sqrt(0.5^2 * 3), rounded up to 1e-6


def test_the_check_command_exit_codes_for_boxes(tmp_path: Path, capsys) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    zones = json.loads(json.dumps(_ZONES_JSON))
    zones["calibration"]["calibrated_at"] = today
    zones_file = tmp_path / "zones.json"
    zones_file.write_text(json.dumps(zones), encoding="utf-8")
    observation = tmp_path / "obs.json"
    observation.write_text(
        json.dumps({"active": True, "observedAt": datetime.now(timezone.utc).isoformat(), "maxAgeSeconds": 3600}), encoding="utf-8"
    )
    expected = {(1.0, 1.0, 0.5): 2, (3.0, 2.0, 1.0): 1, (9.0, 9.0, 9.0): 0}
    for (x, y, z), code in expected.items():
        detections = tmp_path / "det.json"
        detections.write_text(json.dumps({"objects": [{"id": "o", "position": {"x": x, "y": y, "z": z}}]}), encoding="utf-8")
        argv = ["check", "--zones", str(zones_file), "--detections", str(detections), "--observation", str(observation)]
        assert main(argv) == code
    capsys.readouterr()


def test_unknown_extra_keys_on_a_box_zone_are_ignored_today() -> None:
    """A box zone has always tolerated extra keys (a comment, a label)."""
    data = json.loads(json.dumps(_ZONES_JSON))
    data["zones"][0]["comment"] = "kept for humans"
    zone_set = parse_zone_set(data)
    assert len(zone_set.zones) == 2
