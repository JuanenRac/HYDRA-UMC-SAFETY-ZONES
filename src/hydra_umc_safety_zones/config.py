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


def parse_zones(raw: dict) -> tuple[Zone, ...]:
    """Parses already-loaded JSON shaped like:
    ``{"zones": [{"id": "...", "level": "warning"|"danger",
    "min": {"x", "y", "z"}, "max": {"x", "y", "z"}}, ...]}``.

    Split out from `load_zones` so a real HTTP caller (api.py) can hand
    this already-parsed JSON body directly - a server-side file path
    only ever made sense for the CLI, which runs on the same machine as
    the file.
    """
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


def load_zones(path: str | Path) -> tuple[Zone, ...]:
    """Reads `path` and parses it via `parse_zones` - see that function's
    own docstring for the real JSON shape."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_zones(raw)


def parse_zone_set(raw: dict) -> ZoneSet:
    """Parses the same already-loaded JSON `parse_zones` does, plus an
    optional top-level ``"calibration"`` object shaped like:
    ``{"version": "...", "source": "...", "calibrated_at": "YYYY-MM-DD",
    "max_age_days": N}``. Data with no ``"calibration"`` key at all
    parses successfully with ``calibration=None`` - deliberately not a
    parse error, since the whole point of `ZoneSet` is to let a caller
    (see `safety_state.py`) fail safe on missing calibration rather than
    fail to parse at all.
    """
    zones = parse_zones(raw)
    calibration_data = raw.get("calibration")
    calibration = parse_calibration(calibration_data) if calibration_data is not None else None
    return ZoneSet(zones=zones, calibration=calibration)


def load_zone_set(path: str | Path) -> ZoneSet:
    """Reads `path` and parses it via `parse_zone_set` - see that
    function's own docstring for the real JSON shape."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_zone_set(raw)


def parse_detections(raw: dict) -> tuple[DetectedObject, ...]:
    """Parses already-loaded JSON shaped like:
    ``{"objects": [{"id": "...", "position": {"x", "y", "z"}}, ...]}``.
    """
    objects = []
    for entry in raw["objects"]:
        objects.append(
            DetectedObject(
                object_id=entry["id"], position=_point(entry["position"])
            )
        )
    return tuple(objects)


def load_detections(path: str | Path) -> tuple[DetectedObject, ...]:
    """Reads `path` and parses it via `parse_detections` - see that
    function's own docstring for the real JSON shape."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_detections(raw)
