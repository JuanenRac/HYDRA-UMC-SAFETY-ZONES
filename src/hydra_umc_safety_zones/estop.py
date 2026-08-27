# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/estop.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Real E-STOP *requesting* - deliberately not asserting.

This module is the code-level enforcement of the README's "detect-vs-
enforce boundary": everything here can decide that a stop is needed and
hand a `EStopRequest` to an `EStopRequester`, but nothing in this project
is allowed to cut motor power itself. The real CAN transport that turns a
request into an actual physical stop lives in HYDRA-UMC (the firmware),
on hardware built and certified for that role - out of scope for this
repository by design, not by omission.

`NullEStopRequester` is the only requester implementation here: it just
records what would have been sent. That is honest for this stage - there
is no real CAN bus code in this repository yet, and pretending otherwise
would blur exactly the boundary this module exists to keep sharp. A real
`CanEStopRequester` (python-can, see mejoras_futuras.txt) is future work,
and it should only ever be *additive* here: something that implements the
same `EStopRequester` protocol, never a change to how `request_estop_for`
decides when a stop is warranted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from hydra_umc_safety_zones.breach import Breach, worst_level_per_object
from hydra_umc_safety_zones.zones import ZoneLevel


@dataclass(frozen=True)
class EStopRequest:
    """A single request to stop - not a stop. Whether this ever becomes a
    real motor-power cut is entirely up to the firmware receiving it."""

    object_id: str
    zone_id: str
    reason: str


class EStopRequester(Protocol):
    """What any real transport (CAN, or a test double) must implement."""

    def send(self, request: EStopRequest) -> None: ...


@dataclass
class NullEStopRequester:
    """Records every request it is asked to send, without transmitting
    anything anywhere. Safe to use in tests and in this CLI's default
    configuration, precisely because there is no real hardware behind it
    to accidentally trigger."""

    sent: list[EStopRequest] = field(default_factory=list)

    def send(self, request: EStopRequest) -> None:
        self.sent.append(request)


def request_estop_for(
    breaches: tuple[Breach, ...], requester: EStopRequester
) -> tuple[EStopRequest, ...]:
    """Request one E-STOP per object whose worst breach is DANGER. WARNING-
    only breaches never reach this function's request path - the README's
    "Warning (slowdown)" is a real, distinct outcome from "Danger (stop)",
    and this function only ever handles the latter.
    """
    worst = worst_level_per_object(breaches)
    danger_zone_by_object = {
        b.object_id: b.zone_id
        for b in breaches
        if b.level is ZoneLevel.DANGER
    }
    requests: list[EStopRequest] = []
    for object_id, level in worst.items():
        if level is not ZoneLevel.DANGER:
            continue
        zone_id = danger_zone_by_object[object_id]
        request = EStopRequest(
            object_id=object_id,
            zone_id=zone_id,
            reason=f"object '{object_id}' breached danger zone '{zone_id}'",
        )
        requester.send(request)
        requests.append(request)
    return tuple(requests)
