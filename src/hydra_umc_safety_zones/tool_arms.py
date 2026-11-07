# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/tool_arms.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The tool arms of the URTC tools that a caller reports (``POST /tool-arms``) and that panels read (``GET /tool-arms``).

A tool is ARMED when its power stage may act (a soldering iron that heats, a spindle that may turn). The service that drives the tool reports
the arm state of one tool of one robot at a time; this module only keeps those reports and says which of them are old. It never arms or
disarms anything and it never requests a stop: a panel shows the list next to the verdict of ``GET /state``, and the stop stays the job of
the zones and of the firmware.

Rules, all on the side of keeping the service closed:

* off unless the operator starts the service with ``--tool-arms`` (the routes answer 404 until then) and never without a credential;
* a tool is named by its number (0 to 24, the ``ToolMode_t`` of the URTC firmware; 25 is the invalid value and is refused);
* a report that is older than the time limit is listed as ``stale`` and is counted apart: a panel must read a stale ARMED tool as "not
  confirmed", never as safe, and a stale disarmed tool as "not confirmed" too;
* at most ``MAX_ARMS`` (robot, tool) pairs are kept; a new pair beyond that is refused (429) and a known pair can always be updated.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass

SCHEMA_VERSION = "1.0"
MAX_ARMS = 256
TOOL_ID_MAX = 24
DEFAULT_TTL_SECONDS = 30.0
_ROBOT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$")


class ToolArmError(ValueError):
    """The report is not a valid tool arm report."""


class ToolArmLimitError(ToolArmError):
    """Too many (robot, tool) pairs are kept."""


@dataclass(frozen=True)
class ToolArm:
    robot: str
    tool_id: int
    armed: bool
    by: str
    seq: int
    reported_at: float


def parse_tool_arm_report(body: dict) -> tuple[str, int, bool]:
    """(robot, tool_id, armed) of a body, or ToolArmError."""
    robot = body.get("robot")
    if not isinstance(robot, str) or not _ROBOT.match(robot):
        raise ToolArmError('"robot" must be a robot id of letters, digits and . _ : - (at most 64 characters)')
    tool_id = body.get("toolId")
    if isinstance(tool_id, bool) or not isinstance(tool_id, int) or not 0 <= tool_id <= TOOL_ID_MAX:
        raise ToolArmError(f'"toolId" must be a whole number from 0 to {TOOL_ID_MAX}')
    armed = body.get("armed")
    if not isinstance(armed, bool):
        raise ToolArmError('"armed" must be true or false')
    return robot, tool_id, armed


class ToolArmRegistry:
    """The last report of each (robot, tool), in memory: a restart forgets them, which the caller sees when it reports again."""

    def __init__(self, clock: Callable[[], float], ttl_seconds: float = DEFAULT_TTL_SECONDS, limit: int = MAX_ARMS) -> None:
        if not ttl_seconds > 0:
            raise ValueError("ttl_seconds must be above zero")
        self._clock = clock
        self.ttl_seconds = float(ttl_seconds)
        self._limit = limit
        self._lock = threading.Lock()
        self._arms: dict[tuple[str, int], ToolArm] = {}
        self._seq = 0

    def report(self, robot: str, tool_id: int, armed: bool, by: str) -> ToolArm:
        with self._lock:
            key = (robot, tool_id)
            if key not in self._arms and len(self._arms) >= self._limit:
                raise ToolArmLimitError(f"at most {self._limit} tool arms are kept")
            self._seq += 1
            record = ToolArm(robot, tool_id, armed, by, self._seq, self._clock())
            self._arms[key] = record
            return record

    def listing(self) -> dict:
        """The answer of ``GET /tool-arms``."""
        now = self._clock()
        with self._lock:
            arms = sorted(self._arms.values(), key=lambda a: (a.robot, a.tool_id))
        items = []
        for arm in arms:
            age = max(0.0, now - arm.reported_at)
            items.append({
                "robot": arm.robot, "toolId": arm.tool_id, "armed": arm.armed, "by": arm.by, "seq": arm.seq,
                "ageSeconds": round(age, 3), "stale": age > self.ttl_seconds,
            })
        return {
            "schema_version": SCHEMA_VERSION,
            "ttlSeconds": self.ttl_seconds,
            "arms": items,
            "armedCount": sum(1 for i in items if i["armed"] and not i["stale"]),
            "staleCount": sum(1 for i in items if i["stale"]),
        }
