import json
import math

import pytest

from hydra_umc_safety_zones.geometry import AABB, Point3D
from hydra_umc_safety_zones.main import main
from hydra_umc_safety_zones.obstacles import MAX_OBSTACLES, export_obstacles, zone_obstacles
from hydra_umc_safety_zones.zones import SeparationModel, Zone, ZoneLevel


def _zone(zone_id="z", level=ZoneLevel.DANGER, lo=(0, 0, 0), hi=(2, 1, 1)):
    return Zone(zone_id, level, AABB(Point3D(*lo), Point3D(*hi)))


def _covered(spheres, p):
    return any(math.dist((s["center"]["x"], s["center"]["y"], s["center"]["z"]), p) <= s["radius"] for s in spheres)


def test_spheres_cover_every_corner_and_a_grid_of_points():
    zone = _zone(hi=(2.3, 1.1, 0.7))
    spheres = zone_obstacles(zone, 0.5)
    for x in (0, 0.5, 1.15, 2.3):
        for y in (0, 0.3, 1.1):
            for z in (0, 0.35, 0.7):
                assert _covered(spheres, (x, y, z)), (x, y, z)


def test_smaller_cell_means_more_and_tighter_spheres():
    zone = _zone()
    coarse, fine = zone_obstacles(zone, 1.0), zone_obstacles(zone, 0.25)
    assert len(fine) > len(coarse)
    assert max(s["radius"] for s in fine) < max(s["radius"] for s in coarse)


def test_flat_box_gives_one_layer():
    assert zone_obstacles(_zone(hi=(1, 1, 0)), 1.0)


@pytest.mark.parametrize("cell", [0, -1, float("nan"), float("inf"), True, "1"])
def test_bad_cell_is_refused(cell):
    with pytest.raises(ValueError):
        zone_obstacles(_zone(), cell)


def test_too_many_spheres_is_refused():
    with pytest.raises(ValueError):
        zone_obstacles(_zone(hi=(100, 100, 100)), 0.5)
    assert MAX_OBSTACLES > 0


def test_level_filter():
    zones = (_zone("w", ZoneLevel.WARNING, hi=(4, 4, 4)), _zone("d", ZoneLevel.DANGER, hi=(1, 1, 1)))
    everything = export_obstacles(zones, "all", 1.0)["obstacles"]
    danger = export_obstacles(zones, "danger", 1.0)["obstacles"]
    warning = export_obstacles(zones, "warning", 1.0)["obstacles"]
    assert len(everything) == len(danger) + len(warning)
    assert len(danger) == 1
    with pytest.raises(ValueError):
        export_obstacles(zones, "other")


def test_velocity_and_separation_grow_the_export():
    zones = (_zone(hi=(1, 1, 1)),)
    base = export_obstacles(zones, "all", 10.0)["obstacles"][0]["radius"]
    faster = export_obstacles(zones, "all", 10.0, tool_velocity_mps=2.0)["obstacles"][0]["radius"]
    model = SeparationModel(1.6, 0.5, 0.2)
    with_model = export_obstacles(zones, "all", 10.0, separation=model)["obstacles"][0]["radius"]
    assert faster > base and with_model > base


def _zones_file(tmp_path):
    path = tmp_path / "zones.json"
    path.write_text(
        json.dumps({"zones": [{"id": "d", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 1, "y": 1, "z": 1}}]}),
        encoding="utf-8",
    )
    return path


def test_cli_prints_planner_scenario_obstacles(tmp_path, capsys):
    assert main(["obstacles", "--zones", str(_zones_file(tmp_path)), "--cell", "1"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["obstacles"]) == 1
    assert set(data["obstacles"][0]) == {"center", "radius"}


def test_cli_writes_a_file(tmp_path, capsys):
    out = tmp_path / "o.json"
    assert main(["obstacles", "--zones", str(_zones_file(tmp_path)), "--output", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["obstacles"]


def test_cli_bad_input_exits_3_and_writes_nothing(tmp_path, capsys):
    out = tmp_path / "o.json"
    assert main(["obstacles", "--zones", str(tmp_path / "missing.json"), "--output", str(out)]) == 3
    assert not out.exists()
    assert main(["obstacles", "--zones", str(_zones_file(tmp_path)), "--cell", "0"]) == 3
