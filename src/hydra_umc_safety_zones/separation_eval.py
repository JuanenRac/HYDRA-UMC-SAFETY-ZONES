# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/separation_eval.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The verdict of the separation distance, zone by zone: evaluated and safe, evaluated and unsafe, or NOT evaluated.

The separation distance is S = K * T + C (ISO 13855 / ISO 13857 form; indicative, this is not a certification): K is the approach
speed, T the total stopping time and C the intrusion margin. All three come from the installation's own risk assessment and have
NO default value here. A zone whose figures are not all given (its own, or the machine-wide ones) is declared NOT EVALUATED with the
reason; it is never computed with a made-up figure. A consumer must read NOT EVALUATED as "not released": only EVALUATED_SAFE for
every zone means the separation distance was checked and kept.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from hydra_umc_safety_zones.breach import Breach
from hydra_umc_safety_zones.zones import SeparationModel, ZoneSet


class SeparationVerdict(str, Enum):
    EVALUATED_SAFE = "evaluated_safe"
    EVALUATED_UNSAFE = "evaluated_unsafe"
    NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True)
class ZoneSeparation:
    zone_id: str
    verdict: SeparationVerdict
    reason: str
    # S for a still tool in meters, only when the zone was evaluated.
    distance_m: float | None = None

    def as_dict(self) -> dict:
        out: dict = {"zone": self.zone_id, "verdict": self.verdict.value, "reason": self.reason}
        if self.distance_m is not None:
            out["distanceM"] = self.distance_m
        return out


def model_for_zone(zone_set: ZoneSet, zone) -> tuple[SeparationModel | None, tuple[str, ...], str]:
    """The model that applies to a zone, the figures it lacks and where it comes from ("zone" or "machine")."""
    if zone.separation is not None:
        return zone.separation, (), "zone"
    if zone.separation_missing and len(zone.separation_missing) < 3:
        # the zone began to declare its own figures and left some empty: its own figures are incomplete, the machine's are not mixed in
        return None, zone.separation_missing, "zone"
    if zone_set.separation is not None:
        return zone_set.separation, (), "machine"
    return None, zone_set.separation_missing, "machine"


def evaluate_separation(zone_set: ZoneSet, scaled_breaches: tuple[Breach, ...]) -> tuple[ZoneSeparation, ...]:
    """One entry per zone. ``scaled_breaches`` are the breaches against the zones already grown by their own S (collect_breaches)."""
    breached = {b.zone_id for b in scaled_breaches}
    out: list[ZoneSeparation] = []
    for zone in zone_set.zones:
        model, missing, origin = model_for_zone(zone_set, zone)
        if model is None:
            names = ", ".join(missing)
            where = "its own separation figures are incomplete" if origin == "zone" else "no separation figures (zone or machine)"
            out.append(ZoneSeparation(zone.zone_id, SeparationVerdict.NOT_EVALUATED, f"not evaluated: {where}; empty: {names}"))
            continue
        distance = model.margin_m(0.0)
        if zone.zone_id in breached:
            out.append(ZoneSeparation(zone.zone_id, SeparationVerdict.EVALUATED_UNSAFE, f"an object is inside the zone grown by S={distance:.3f} m ({origin} figures)", distance))
        else:
            out.append(ZoneSeparation(zone.zone_id, SeparationVerdict.EVALUATED_SAFE, f"no object inside the zone grown by S={distance:.3f} m ({origin} figures)", distance))
    return tuple(out)


def overall_separation(zones: tuple[ZoneSeparation, ...]) -> SeparationVerdict:
    """The worst of the zones: unsafe, then not evaluated, then safe. Only safe may be read as released."""
    verdicts = {z.verdict for z in zones}
    if SeparationVerdict.EVALUATED_UNSAFE in verdicts:
        return SeparationVerdict.EVALUATED_UNSAFE
    if SeparationVerdict.NOT_EVALUATED in verdicts or not verdicts:
        return SeparationVerdict.NOT_EVALUATED
    return SeparationVerdict.EVALUATED_SAFE


def separation_report(zone_set: ZoneSet, scaled_breaches: tuple[Breach, ...]) -> dict:
    """The optional ``separation`` field of an answer: overall verdict, whether the zones file requires it, and the zones."""
    zones = evaluate_separation(zone_set, scaled_breaches)
    return {
        "status": overall_separation(zones).value,
        "required": zone_set.separation_required,
        "zones": [z.as_dict() for z in zones],
    }
