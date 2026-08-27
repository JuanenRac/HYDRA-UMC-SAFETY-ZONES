# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/zones.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Real Warning/Danger zone definitions, matching the two-level perimeter
model described in the README (slowdown vs stop)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from hydra_umc_safety_zones.geometry import AABB


class ZoneLevel(str, Enum):
    """Ordered by severity: DANGER always outranks WARNING when a single
    object breaches zones of both levels at once (see `breach.py`)."""

    WARNING = "warning"
    DANGER = "danger"


@dataclass(frozen=True)
class Zone:
    """A single named Warning or Danger perimeter."""

    zone_id: str
    level: ZoneLevel
    volume: AABB
