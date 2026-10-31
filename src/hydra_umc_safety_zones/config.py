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
from typing import Any

from hydra_umc_safety_zones.breach import DetectedObject
from hydra_umc_safety_zones.calibration import parse_calibration
from hydra_umc_safety_zones.geometry import AABB, Point3D
from hydra_umc_safety_zones.shapes import (
    OBB,
    Capsule,
    Cylinder,
    GeometrySettings,
    Volume,
    parse_geometry_settings,
)
from hydra_umc_safety_zones.observation import ObservationStatus, parse_observation_status
from hydra_umc_safety_zones.robot_body import (
    BodySegment,
    RobotBody,
    RobotBodyDisabled,
    RobotBodyError,
    RobotBodySettings,
    RobotFrame,
    build_robot_body,
)
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


def _read_json_file(path: str | Path) -> Any:
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


_SHAPE_FIELDS: dict[str, set[str]] = {
    "obb": {"center", "half_extents", "rotation_deg"},
    "cylinder": {"base_center", "radius", "height"},
    "capsule": {"start", "end", "radius"},
}


def _number(data: object, what: str, *, required: bool = True, default: float = 0.0) -> float:
    if data is None and not required:
        return default
    if isinstance(data, bool) or not isinstance(data, (int, float)):
        raise ConfigError(f"{what} must be a number, got {data!r}")
    if not math.isfinite(data):
        raise ConfigError(f"{what} must be finite")
    return float(data)


def _triple(data: object, keys: tuple[str, str, str], what: str, *, required: bool = True) -> tuple[float, float, float]:
    """An object with exactly the given keys (or, when not required, a subset: the missing ones are 0)."""
    if not isinstance(data, dict):
        raise ConfigError(f"{what} must be an object with {', '.join(keys)}")
    extra = sorted(set(data) - set(keys))
    if extra:
        raise ConfigError(f"{what} has unknown field(s): {extra}")
    values = []
    for key in keys:
        values.append(_number(data.get(key), f"{what}.{key}", required=required, default=0.0))
    return values[0], values[1], values[2]


def _shape_volume(shape: str, entry: dict, zone_id: str, geometry: GeometrySettings) -> Volume:
    """The volume of a zone that is not an axis-aligned box. Only reachable when the zones file switched the shapes on."""
    allowed = _SHAPE_FIELDS[shape] | {"id", "level", "shape"}
    extra = sorted(set(entry) - allowed)
    if extra:
        raise ConfigError(f"zone {zone_id!r}: a {shape} zone has unknown field(s): {extra}")
    missing = sorted(key for key in _SHAPE_FIELDS[shape] - ({"rotation_deg"} if shape == "obb" else set()) if key not in entry)
    if missing:
        raise ConfigError(f"zone {zone_id!r}: a {shape} zone is missing field(s): {missing}")
    try:
        volume: Volume
        if shape == "obb":
            half = _triple(entry["half_extents"], ("x", "y", "z"), f"zone {zone_id!r} half_extents")
            rotation = _triple(entry.get("rotation_deg", {}), ("roll", "pitch", "yaw"), f"zone {zone_id!r} rotation_deg", required=False)
            volume = OBB(_point(entry["center"]), half, rotation)
        elif shape == "cylinder":
            volume = Cylinder(
                _point(entry["base_center"]),
                _number(entry["radius"], f"zone {zone_id!r} radius"),
                _number(entry["height"], f"zone {zone_id!r} height"),
            )
        else:
            volume = Capsule(_point(entry["start"]), _point(entry["end"]), _number(entry["radius"], f"zone {zone_id!r} radius"))
    except ValueError as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"zone {zone_id!r}: {exc}") from exc
    # In the conservative mode every shape is replaced by the axis-aligned box that contains it.
    return volume.bounding_box() if geometry.mode == "bounding" else volume


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
    try:
        geometry = parse_geometry_settings(raw.get("geometry"))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
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
        shape = entry.get("shape", "box")
        volume: Volume
        if shape == "box":
            try:
                volume = AABB(_point(entry["min"]), _point(entry["max"]))
            except KeyError as exc:
                raise ConfigError(f"zone {zone_id!r} is missing {exc}") from exc
        elif shape in _SHAPE_FIELDS and geometry.shapes:
            volume = _shape_volume(shape, entry, zone_id, geometry)
        elif shape in _SHAPE_FIELDS:
            # The shapes are off unless the file switches them on: a shape that would be read as something else is a refused file, never a smaller zone.
            raise ConfigError(f"zone {zone_id!r}: shape {shape!r} needs \"geometry\": {{\"shapes\": true}} in the zones file")
        else:
            raise ConfigError(f"zone {zone_id!r}: shape must be one of ['box', 'obb', 'cylinder', 'capsule'], got {shape!r}")
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
    return ZoneSet(zones=zones, calibration=calibration, separation=separation, robot_body=parse_body_settings(raw.get("geometry")))


_BODY_SETTING_FIELDS = ("enabled", "required", "resolution_m", "margin_m", "max_segments", "max_pieces_per_segment")


def parse_body_settings(geometry: object) -> RobotBodySettings:
    """The ``robot_body`` entry of the ``geometry`` object of a zones file: ``true``/``false`` for the switch alone, or an object
    with ``enabled`` and the tuning values. Missing means off; anything unknown or malformed makes the whole file invalid."""
    if not isinstance(geometry, dict) or "robot_body" not in geometry:
        return RobotBodySettings()
    raw = geometry["robot_body"]
    try:
        if isinstance(raw, bool):
            return RobotBodySettings(enabled=raw)
        if not isinstance(raw, dict):
            raise RobotBodyError(f"geometry.robot_body must be true, false or an object, got {raw!r}")
        unknown = sorted(set(raw) - set(_BODY_SETTING_FIELDS))
        if unknown:
            raise RobotBodyError(f"geometry.robot_body has unknown field(s): {unknown}")
        return RobotBodySettings(**raw)
    except RobotBodyError as exc:
        raise ConfigError(str(exc)) from exc


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
        value = float(data)  # type: ignore[arg-type]
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


def parse_robot_body(raw: object, settings: RobotBodySettings | None) -> RobotBody:
    """Reads the ``robotBody`` object of a request (or the body file of the CLI)::

        {"frame": {"translation": {x, y, z}, "rotation_deg": {roll, pitch, yaw}},     (optional: poses -> frame of the zones)
         "segments": [{"id": "forearm", "start": {x, y, z}, "end": {x, y, z}, "radius": 0.06}, ...]}

    Every length is in metres and every ``radius`` is the one the installation declares for that segment: there is no default.
    Raises RobotBodyDisabled when the zones file did not switch the feature on, and RobotBodyError (a ValueError) for anything else.
    """
    if settings is None or not settings.enabled:
        raise RobotBodyDisabled("this request carries a robot body but the zones file does not switch it on (geometry.robot_body)")
    try:
        if not isinstance(raw, dict):
            raise RobotBodyError("robotBody must be a JSON object")
        unknown = sorted(set(raw) - {"frame", "segments"})
        if unknown:
            raise RobotBodyError(f"robotBody has unknown field(s): {unknown}")
        frame = None
        if raw.get("frame") is not None:
            frame_raw = raw["frame"]
            if not isinstance(frame_raw, dict):
                raise RobotBodyError("robotBody.frame must be an object")
            extra = sorted(set(frame_raw) - {"translation", "rotation_deg"})
            if extra:
                raise RobotBodyError(f"robotBody.frame has unknown field(s): {extra}")
            frame = RobotFrame(
                _triple(frame_raw.get("translation", {}), ("x", "y", "z"), "robotBody.frame.translation", required=False),
                _triple(frame_raw.get("rotation_deg", {}), ("roll", "pitch", "yaw"), "robotBody.frame.rotation_deg", required=False),
            )
        entries = raw.get("segments")
        if not isinstance(entries, list):
            raise RobotBodyError("robotBody.segments must be a list")
        if len(entries) > settings.max_segments:
            raise RobotBodyError(f"a robot body may have {settings.max_segments} segments at most, got {len(entries)}")
        segments = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise RobotBodyError("each segment must be an object")
            extra = sorted(set(entry) - {"id", "start", "end", "radius"})
            if extra:
                raise RobotBodyError(f"segment has unknown field(s): {extra}")
            missing = sorted(key for key in ("id", "start", "end", "radius") if key not in entry)
            if missing:
                raise RobotBodyError(f"segment is missing field(s): {missing} (the radius has no default: declare it)")
            segment_id = _identifier(entry["id"], "segment id")
            segments.append(
                BodySegment(
                    segment_id,
                    _point(entry["start"]),
                    _point(entry["end"]),
                    _number(entry["radius"], f"segment {segment_id!r} radius"),
                )
            )
        return build_robot_body(tuple(segments), frame, settings)
    except RobotBodyError:
        raise
    except ValueError as exc:  # ConfigError from the helpers above
        raise RobotBodyError(str(exc)) from exc
