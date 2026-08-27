import json

from hydra_umc_safety_zones.config import load_detections, load_zones
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
