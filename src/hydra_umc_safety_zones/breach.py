# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/breach.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Real zone-breach checking: given zones and detected object positions,
which zones does each object actually violate.

Where the detected positions come from is deliberately out of scope here -
today they can come from a hand-written test fixture or a JSON file loaded
by `config.py`; tomorrow they would come from the real Hailo-8 spatial
segmentation pipeline this project's README describes. `DetectedObject` is
the narrow interface between the two: one 3D point per tracked object.
"""
from __future__ import annotations

from dataclasses import dataclass

from hydra_umc_safety_zones.geometry import Point3D
from hydra_umc_safety_zones.zones import Zone, ZoneLevel


@dataclass(frozen=True)
class DetectedObject:
    """One tracked object's position in the shared 3D workspace frame."""

    object_id: str
    position: Point3D


@dataclass(frozen=True)
class Breach:
    """One object found inside one zone."""

    zone_id: str
    level: ZoneLevel
    object_id: str
    position: Point3D


def check_breaches(
    zones: tuple[Zone, ...], objects: tuple[DetectedObject, ...]
) -> tuple[Breach, ...]:
    """Every (object, zone) pair where the object's position is inside the
    zone's volume. A single object breaching both a Warning and a Danger
    zone at once produces two separate `Breach` entries - real, since both
    are true - rather than only the more severe one; `worst_level_per_object`
    exists for callers (like E-STOP requesting) that specifically want the
    single worst outcome per object instead.
    """
    breaches: list[Breach] = []
    for obj in objects:
        for zone in zones:
            if zone.volume.contains(obj.position):
                breaches.append(
                    Breach(
                        zone_id=zone.zone_id,
                        level=zone.level,
                        object_id=obj.object_id,
                        position=obj.position,
                    )
                )
    return tuple(breaches)


def worst_level_per_object(
    breaches: tuple[Breach, ...],
) -> dict[str, ZoneLevel]:
    """Collapse possibly-multiple breaches per object down to the single
    worst (DANGER beats WARNING) level reached, keyed by object_id."""
    worst: dict[str, ZoneLevel] = {}
    for b in breaches:
        current = worst.get(b.object_id)
        if current is None or (
            current is ZoneLevel.WARNING and b.level is ZoneLevel.DANGER
        ):
            worst[b.object_id] = b.level
    return worst
