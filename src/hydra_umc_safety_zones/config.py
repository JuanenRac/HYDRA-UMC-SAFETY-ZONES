# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/config.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Loading zones and detected objects from plain JSON files.

JSON, not YAML: `pyproject.toml`'s dependency list is still `[]` - `json`
is stdlib, `pyyaml` is real future work once there is an actual
zone-authoring tool worth serializing for.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from hydra_umc_safety_zones.breach import DetectedObject
from hydra_umc_safety_zones.calibration import parse_calibration
from hydra_umc_safety_zones.geometry import AABB, Point3D
from hydra_umc_safety_zones.zones import Zone, ZoneLevel, ZoneSet


class ConfigError(ValueError):
    """Raised when a safety JSON coordinate cannot represent a real point."""


def _point(data: object) -> Point3D:
    if not isinstance(data, dict):
        raise ConfigError("point must be an object with x, y and z")
    values: dict[str, float] = {}
    for axis in ("x", "y", "z"):
        try:
            value = float(data[axis])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"point.{axis} must be numeric") from exc
        if not math.isfinite(value):
            raise ConfigError(f"point.{axis} must be finite")
        values[axis] = value
    return Point3D(x=values["x"], y=values["y"], z=values["z"])


def load_zones(path: str | Path) -> tuple[Zone, ...]:
    """Parses a JSON file shaped like:
    ``{"zones": [{"id": "...", "level": "warning"|"danger",
    "min": {"x", "y", "z"}, "max": {"x", "y", "z"}}, ...]}``.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    zones = []
    for entry in raw["zones"]:
        zones.append(
            Zone(
                zone_id=entry["id"],
                level=ZoneLevel(entry["level"]),
                volume=AABB(_point(entry["min"]), _point(entry["max"])),
            )
        )
    return tuple(zones)


def load_zone_set(path: str | Path) -> ZoneSet:
    """Parses the same file `load_zones` does, plus an optional top-level
    ``"calibration"`` object shaped like:
    ``{"version": "...", "source": "...", "calibrated_at": "YYYY-MM-DD",
    "max_age_days": N}``. A file with no ``"calibration"`` key at all
    loads successfully with ``calibration=None`` - deliberately not a load
    error, since the whole point of `ZoneSet` is to let a caller (see
    `safety_state.py`) fail safe on missing calibration rather than fail
    to load at all.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    zones = load_zones(path)
    calibration_data = raw.get("calibration")
    calibration = parse_calibration(calibration_data) if calibration_data is not None else None
    return ZoneSet(zones=zones, calibration=calibration)


def load_detections(path: str | Path) -> tuple[DetectedObject, ...]:
    """Parses a JSON file shaped like:
    ``{"objects": [{"id": "...", "position": {"x", "y", "z"}}, ...]}``.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    objects = []
    for entry in raw["objects"]:
        objects.append(
            DetectedObject(
                object_id=entry["id"], position=_point(entry["position"])
            )
        )
    return tuple(objects)
