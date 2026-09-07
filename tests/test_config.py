import json

import pytest

from hydra_umc_safety_zones.config import ConfigError, load_detections, load_zone_set, load_zones
from hydra_umc_safety_zones.geometry import Point3D
from hydra_umc_safety_zones.zones import ZoneLevel


def test_load_zones(tmp_path):
    path = tmp_path / "zones.json"
    path.write_text(
        json.dumps(
            {
                "zones": [
                    {
                        "id": "d1",
                        "level": "danger",
                        "min": {"x": 0, "y": 0, "z": 0},
                        "max": {"x": 1, "y": 1, "z": 1},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    zones = load_zones(path)
    assert len(zones) == 1
    assert zones[0].zone_id == "d1"
    assert zones[0].level is ZoneLevel.DANGER
    assert zones[0].volume.contains(Point3D(0.5, 0.5, 0.5))


def test_load_zone_set_with_calibration(tmp_path):
    path = tmp_path / "zones.json"
    path.write_text(
        json.dumps(
            {
                "calibration": {
                    "version": "cal-1",
                    "source": "manual",
                    "calibrated_at": "2026-08-01",
                    "max_age_days": 30,
                },
                "zones": [
                    {
                        "id": "d1",
                        "level": "danger",
                        "min": {"x": 0, "y": 0, "z": 0},
                        "max": {"x": 1, "y": 1, "z": 1},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    zone_set = load_zone_set(path)
    assert zone_set.calibration is not None
    assert zone_set.calibration.version == "cal-1"
    assert len(zone_set.zones) == 1
    assert zone_set.zones[0].zone_id == "d1"


def test_load_zone_set_without_calibration_key_yields_none(tmp_path):
    path = tmp_path / "zones.json"
    path.write_text(
        json.dumps(
            {
                "zones": [
                    {
                        "id": "d1",
                        "level": "danger",
                        "min": {"x": 0, "y": 0, "z": 0},
                        "max": {"x": 1, "y": 1, "z": 1},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    zone_set = load_zone_set(path)
    assert zone_set.calibration is None
    assert len(zone_set.zones) == 1


def test_load_detections(tmp_path):
    path = tmp_path / "detections.json"
    path.write_text(
        json.dumps({"objects": [{"id": "op1", "position": {"x": 1, "y": 2, "z": 3}}]}),
        encoding="utf-8",
    )
    objects = load_detections(path)
    assert len(objects) == 1
    assert objects[0].object_id == "op1"
    assert objects[0].position == Point3D(1, 2, 3)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_load_detections_rejects_non_finite_coordinate(tmp_path, value):
    path = tmp_path / "detections.json"
    path.write_text(
        json.dumps({"objects": [{"id": "op1", "position": {"x": value, "y": 0, "z": 0}}]}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="point.x must be finite"):
        load_detections(path)
