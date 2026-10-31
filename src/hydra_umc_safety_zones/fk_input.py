# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/fk_input.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Turns the JSON that ``hydra-umc-physics-replica fk --json`` prints (one pose per joint) into a robot-body file.

This module reads two JSON documents that are already in memory. It never runs
the other program: whoever chains them runs ``fk`` first and hands the file over.

The segments file says which joint frames each segment joins and declares its
radius, which has no default::

    {"base": {"x": 0, "y": 0, "z": 0},                       (the point called "base", when a segment starts there)
     "frame": {"translation": {...}, "rotation_deg": {...}}, (optional, copied to the body)
     "segments": [{"id": "upper_arm", "from": "base", "to": "elbow", "radius": 0.08},
                  {"id": "tool", "from": "wrist", "to": {"x": 0.4, "y": 0.0, "z": 0.9}, "radius": 0.05}]}

``from`` and ``to`` are the name of a joint of the ``fk`` output, ``"base"``, or an
explicit point. Nothing is inferred: a name that is not there is an error.
"""
from __future__ import annotations

import math
from typing import Any


class FkInputError(ValueError):
    """The ``fk`` output or the segments file cannot be used; no body is produced."""


def _coordinates(value: object, what: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise FkInputError(f"{what} must be an object with x, y and z")
    out: dict[str, float] = {}
    for axis in ("x", "y", "z"):
        number = value.get(axis)
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise FkInputError(f"{what}.{axis} must be a finite number, got {number!r}")
        out[axis] = float(number)
    return out


def _poses(fk: object) -> dict[str, dict[str, float]]:
    if not isinstance(fk, list) or not fk:
        raise FkInputError("the fk output must be a non-empty array of joint poses")
    poses: dict[str, dict[str, float]] = {}
    for entry in fk:
        if not isinstance(entry, dict) or not isinstance(entry.get("joint"), str) or not entry["joint"]:
            raise FkInputError("each fk entry needs a joint name")
        name = entry["joint"]
        if name == "base":
            raise FkInputError("a joint called 'base' would be confused with the base point of the segments file")
        if name in poses:
            raise FkInputError(f"joint {name!r} appears twice in the fk output")
        poses[name] = _coordinates(entry.get("position"), f"joint {name!r} position")
    return poses


def body_from_fk(fk: object, spec: object) -> dict[str, Any]:
    """The ``robotBody`` object for ``check --body`` and ``POST /check`` from an ``fk --json`` array and a segments description."""
    poses = _poses(fk)
    if not isinstance(spec, dict):
        raise FkInputError("the segments file must be a JSON object")
    unknown = sorted(set(spec) - {"base", "frame", "segments"})
    if unknown:
        raise FkInputError(f"the segments file has unknown field(s): {unknown}")
    base = _coordinates(spec["base"], "base") if "base" in spec else None
    entries = spec.get("segments")
    if not isinstance(entries, list) or not entries:
        raise FkInputError("segments must be a non-empty list")

    def resolve(ref: object, what: str) -> dict[str, float]:
        if isinstance(ref, dict):
            return _coordinates(ref, what)
        if ref == "base":
            if base is None:
                raise FkInputError(f"{what} is 'base' but the segments file declares no base point")
            return dict(base)
        if isinstance(ref, str) and ref in poses:
            return dict(poses[ref])
        raise FkInputError(f"{what} names {ref!r}, which is neither 'base', a joint of the fk output nor a point")

    segments = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise FkInputError("each segment must be an object")
        extra = sorted(set(entry) - {"id", "from", "to", "radius"})
        if extra:
            raise FkInputError(f"a segment has unknown field(s): {extra}")
        missing = sorted(key for key in ("id", "from", "to", "radius") if key not in entry)
        if missing:
            raise FkInputError(f"a segment is missing field(s): {missing} (the radius has no default: declare it)")
        segment_id = entry["id"]
        if not isinstance(segment_id, str) or not segment_id.strip():
            raise FkInputError(f"a segment id must be a non-empty string, got {segment_id!r}")
        radius = entry["radius"]
        if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not math.isfinite(radius) or radius <= 0:
            raise FkInputError(f"segment {segment_id!r}: radius must be a finite number above zero, got {radius!r}")
        segments.append(
            {
                "id": segment_id,
                "start": resolve(entry["from"], f"segment {segment_id!r} from"),
                "end": resolve(entry["to"], f"segment {segment_id!r} to"),
                "radius": float(radius),
            }
        )
    body: dict[str, Any] = {"segments": segments}
    if spec.get("frame") is not None:
        body["frame"] = spec["frame"]
    return body
