# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/hold.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The holds a caller asks for on a robot (``POST /hold``).

A hold is a request to pause one robot, made by a service that has the identity of the server (NODE-HEALING asks for one before it
touches a robot that holds a mission). Two things keep it safe:

* it is off unless the operator starts the service with ``--hold`` (the route answers 404 until then) and it never answers without a
  credential: with neither a token nor the identity of the server there is nobody to ask, so the route refuses;
* by default it only RECORDS the hold and lists it (``GET /holds``). It becomes a stop request, through the same requester as a breach,
  only when the operator also gave ``--hold-actuate``. Even then it is a request: cutting power is the job of the firmware.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass

MAX_HOLDS = 64
_ROBOT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$")


class HoldError(ValueError):
    """The request is not a valid hold."""


@dataclass(frozen=True)
class Hold:
    robot: str
    reason: str
    by: str
    seq: int

    def as_dict(self) -> dict:
        return {"robot": self.robot, "reason": self.reason, "by": self.by, "seq": self.seq}


def parse_hold_request(body: dict) -> tuple[str, str, bool]:
    """(robot, reason, release) of a body, or HoldError."""
    robot = body.get("robot")
    if not isinstance(robot, str) or not _ROBOT.match(robot):
        raise HoldError('"robot" must be a robot id of letters, digits and . _ : - (at most 64 characters)')
    release = body.get("release", False)
    if not isinstance(release, bool):
        raise HoldError('"release" must be true or false')
    reason = body.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 200:
        raise HoldError('"reason" must be a text of at most 200 characters')
    if not release and not reason.strip():
        raise HoldError('a hold needs a "reason"')
    return robot, reason.strip(), release


class HoldRegistry:
    """The holds in force, in memory: a restart of the service forgets them, which the caller sees when it asks again."""

    def __init__(self, limit: int = MAX_HOLDS) -> None:
        self._limit = limit
        self._holds: dict[str, Hold] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def hold(self, robot: str, reason: str, by: str) -> Hold:
        with self._lock:
            if robot not in self._holds and len(self._holds) >= self._limit:
                raise HoldError(f"at most {self._limit} robots can be held at once")
            self._seq += 1
            record = Hold(robot, reason, by, self._seq)
            self._holds[robot] = record
            return record

    def release(self, robot: str) -> bool:
        with self._lock:
            return self._holds.pop(robot, None) is not None

    def listing(self) -> list[dict]:
        with self._lock:
            return [h.as_dict() for h in sorted(self._holds.values(), key=lambda h: h.seq)]
