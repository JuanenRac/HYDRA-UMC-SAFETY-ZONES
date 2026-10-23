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
from hydra_umc_safety_zones.observation import ObservationStatus, parse_observation_status
from hydra_umc_safety_zones.zones import SeparationModel, Zone, ZoneLevel, ZoneSet, parse_separation_model


class ConfigError(ValueError):
    """Raised when a safety JSON coordinate cannot represent a real point."""


def _reject_constant(name: str) -> object:
    raise ValueError(f"{name} is not a valid number in a safety file")


def strict_json_loads(raw: str | bytes) -> object:
    """`json.loads` that refuses the literals NaN, Infinity and -Infinity,
    which Python accepts by default. A NaN where a number is expected would
    silently switch a check off (every comparison with NaN is false), so the
    one place that reads safety JSON refuses them for the whole document."""
    return json.loads(raw, parse_constant=_reject_constant)


def _read_json_file(path: str | Path) -> object:
    return strict_json_loads(Path(path).read_text(encoding="utf-8"))


def _identifier(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{what} must be a non-empty string, got {value!r}")
    return value


def _point(data: object) -> Point3D:
    if not isinstance(data, dict):
        raise ConfigError("point must be an object with x, y and z")
    values: dict[str, float] = {}
    for axis in ("x", "y", "z"):
        raw = data.get(axis)
        # `float(True)` is 1.0 and `float("0.5")` is 0.5: a boolean or a
        # string in a coordinate is an authoring mistake, not a position.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ConfigError(f"point.{axis} must be a number, got {raw!r}")
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
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
    entries = raw["zones"]
    if not isinstance(entries, list):
        raise ConfigError("zones must be a list")
    # No zones at all would read as "nothing to protect": READY for any
    # object anywhere. A cell with no perimeter is a configuration that has
    # not been written yet, not a safe cell.
    if not entries:
        raise ConfigError("zones must not be empty: a cell with no zones is not a safe cell")
    zones = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError("each zone must be an object")
        zone_id = _identifier(entry.get("id"), "zone id")
        if zone_id in seen:
            raise ConfigError(f"zone id {zone_id!r} is used more than once")
        seen.add(zone_id)
        try:
            level = ZoneLevel(entry.get("level"))
        except ValueError as exc:
            raise ConfigError(
                f"zone {zone_id!r}: level must be one of {[lv.value for lv in ZoneLevel]} (lower case), got {entry.get('level')!r}"
            ) from exc
        try:
            volume = AABB(_point(entry["min"]), _point(entry["max"]))
        except KeyError as exc:
            raise ConfigError(f"zone {zone_id!r} is missing {exc}") from exc
        zones.append(Zone(zone_id=zone_id, level=level, volume=volume))
    return tuple(zones)


def load_zones(path: str | Path) -> tuple[Zone, ...]:
    """Reads `path` and parses it via `parse_zones` - see that function's
    own docstring for the real JSON shape."""
    raw = _read_json_file(path)
    return parse_zones(raw)


def parse_zone_set(raw: dict) -> ZoneSet:
    """Parses the same already-loaded JSON `parse_zones` does, plus an
    optional top-level ``"calibration"`` object shaped like:
    ``{"version": "...", "source": "...", "calibrated_at": "YYYY-MM-DD",
    "max_age_days": N}`` (plus an optional ``"calibrated_by"`` name). Data with no ``"calibration"`` key at all
    parses successfully with ``calibration=None`` - deliberately not a
    parse error, since the whole point of `ZoneSet` is to let a caller
    (see `safety_state.py`) fail safe on missing calibration rather than
    fail to parse at all.
    """
    zones = parse_zones(raw)
    calibration_data = raw.get("calibration")
    calibration = parse_calibration(calibration_data) if calibration_data is not None else None
    separation_data = raw.get("separation")
    separation: SeparationModel | None = parse_separation_model(separation_data) if separation_data is not None else None
    return ZoneSet(zones=zones, calibration=calibration, separation=separation)


def load_zone_set(path: str | Path) -> ZoneSet:
    """Reads `path` and parses it via `parse_zone_set` - see that
    function's own docstring for the real JSON shape."""
    raw = _read_json_file(path)
    return parse_zone_set(raw)


def _radius(value: object) -> float:
    """The optional `radiusM` of a detection: a finite number >= 0 (a boolean, a string or NaN is refused)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"radiusM must be a number, got {value!r}")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"radiusM must be finite and non-negative, got {value!r}")
    return float(value)


def parse_detections(raw: dict) -> tuple[DetectedObject, ...]:
    """Parses already-loaded JSON shaped like:
    ``{"objects": [{"id": "...", "position": {"x", "y", "z"}, "radiusM": 0.3}, ...]}``.
    ``radiusM`` is optional (default 0: the object is its one point); with it, an object breaches every zone its sphere touches.
    """
    entries = raw["objects"]
    if not isinstance(entries, list):
        raise ConfigError("objects must be a list")
    objects = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError("each object must be an object")
        object_id = _identifier(entry.get("id"), "object id")
        if object_id in seen:
            raise ConfigError(f"object id {object_id!r} is used more than once: reports and stop requests would be ambiguous")
        seen.add(object_id)
        try:
            position = _point(entry["position"])
            radius = _radius(entry.get("radiusM", 0.0))
        except KeyError as exc:
            raise ConfigError(f"object {object_id!r} is missing {exc}") from exc
        except ValueError as exc:
            raise ConfigError(f"object {object_id!r}: {exc}") from exc
        objects.append(DetectedObject(object_id=object_id, position=position, radius_m=radius))
    return tuple(objects)


def load_detections(path: str | Path) -> tuple[DetectedObject, ...]:
    """Reads `path` and parses it via `parse_detections` - see that
    function's own docstring for the real JSON shape."""
    raw = _read_json_file(path)
    return parse_detections(raw)


def parse_tool_velocity(data: object) -> float:
    """Parses the optional real head/tool speed (meters/second) a caller
    can supply to `evaluate_safety()`'s own velocity-proportional envelope
    scaling (see zones.py's `scale_zones_for_velocity`). Deliberately
    strict rather than silently clamping a bad value to 0 or to the static
    behavior - a negative or non-finite speed is a real caller/sensor bug
    the caller needs to know about, not something this module should paper
    over for safety-relevant geometry.
    """
    try:
        value = float(data)
    except (TypeError, ValueError) as exc:
        raise ConfigError("toolVelocityMps must be numeric") from exc
    if not math.isfinite(value):
        raise ConfigError("toolVelocityMps must be finite")
    if value < 0:
        raise ConfigError("toolVelocityMps must be non-negative")
    return value


def load_observation_status(path: str | Path) -> ObservationStatus:
    """Reads `path` and parses it via `parse_observation_status`
    (observation.py) - see that function's own docstring for the real
    JSON shape."""
    raw = _read_json_file(path)
    if not isinstance(raw, dict):
        raise ConfigError("an observation file must hold a JSON object")
    return parse_observation_status(raw)
