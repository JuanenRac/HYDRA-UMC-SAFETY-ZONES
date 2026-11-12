# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/hydra_estop.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The stop request in the words of the emergency-stop frame of HYDRA-UMC (identifier 0x001).

The emergency stop of the stack has one frame, defined by HYDRA-UMC, on identifier 0x001. This service speaks it as a node that asks for a stop:

    byte 0      kind        0x01 TRIP
    byte 1      detail      0x04 safety zone (the reason)
    byte 2      source      0x10 the CM5 bridge (this service runs on the CM5)
    byte 3      epoch       counts stops, modulo 256: +1 every time a new stop starts
    bytes 4-5   sequence    little-endian, +1 on EVERY frame this sender puts on 0x001, modulo 65536
    byte 6      kind        complement (kind XOR 0xFF)
    byte 7      detail      complement (detail XOR 0xFF)

Only a TRIP is sent from here. ALIVE and RELEASE belong to the kinematic brain alone: a frame that claims them from another source is treated by every node as a
TRIP, so this service never sends a heartbeat on 0x001. The older frames of this service (identifiers 1 and 2 with a check byte, see `can_estop.py`) stay as a
secondary path that is selected with `"protocol": "legacy"`; the heartbeat of that path can still go out on its own identifier with `"legacy_heartbeat": true`.

The epoch of this sender is its own count of stops. A node latches on any TRIP whatever the epoch, so a difference with the epoch of the brain never delays a
stop; the brain learns the epoch it releases for from the frames it hears.

Not tried on a real bus: the frame is checked against the reference of HYDRA-UMC with the vectors of the tests, and sent through a simulated bus.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from hydra_umc_safety_zones.can_estop import (
    CanBus,
    CanEStopConfig,
    CanEStopError,
    MessageFactory,
    python_can_message,
)
from hydra_umc_safety_zones.estop import EStopRequest

ESTOP_CAN_ID = 0x001

KIND_TRIP = 0x01
KIND_RELEASE = 0x02
KIND_ALIVE = 0x03

REASON_SAFETY_ZONE = 0x04
SOURCE_CM5 = 0x10

FRAME_LENGTH = 8


def encode_trip(reason: int, source: int, epoch: int, sequence: int) -> bytes:
    """The 8 data bytes of a TRIP; raises ValueError for a field that does not fit its byte."""
    if not (0 <= reason < 256 and 0 <= source < 256 and 0 <= epoch < 256):
        raise ValueError("field out of range")
    sequence &= 0xFFFF
    return bytes([KIND_TRIP, reason, source, epoch, sequence & 0xFF, sequence >> 8, KIND_TRIP ^ 0xFF, reason ^ 0xFF])


def decode_frame(data: bytes) -> dict[str, int]:
    """The fields of a frame of 0x001; raises ValueError when the length or a complement is wrong (a node would treat it as a TRIP)."""
    if len(data) != FRAME_LENGTH:
        raise ValueError("a frame has 8 data bytes")
    kind, detail, source, epoch, seq_lo, seq_hi, inv_kind, inv_detail = data
    if inv_kind != kind ^ 0xFF or inv_detail != detail ^ 0xFF:
        raise ValueError("the complement bytes do not match")
    if kind not in (KIND_TRIP, KIND_RELEASE, KIND_ALIVE):
        raise ValueError("unknown frame kind")
    return {"kind": kind, "detail": detail, "source": source, "epoch": epoch, "sequence": seq_lo | (seq_hi << 8)}


class HydraStopRequester:
    """An `EStopRequester` that sends the TRIP of 0x001 for a danger verdict. A send that fails everywhere raises `CanEStopError`: a request that did not go out is never reported as sent.

    Every frame takes the next sequence number (the nodes ignore a sender that repeats one). The epoch goes up by one when a stop starts: a request that comes more than
    `episode_gap_ms` after the previous one is a new stop, one that comes sooner is the same stop still being asked for.
    """

    def __init__(
        self,
        bus: CanBus,
        config: CanEStopConfig,
        message_factory: MessageFactory = python_can_message,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus
        self.config = config
        self._factory = message_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._sequence = 0
        self._epoch = 0
        self._last_request_s: float | None = None
        self.sent_count = 0

    @property
    def epoch(self) -> int:
        return self._epoch

    def _next_sequence(self) -> int:
        self._sequence = (self._sequence + 1) & 0xFFFF
        return self._sequence

    def send(self, request: EStopRequest) -> None:  # noqa: ARG002 - the frame carries no zone: the reason says "safety zone"
        with self._lock:
            now = self._clock()
            if self._last_request_s is None or (now - self._last_request_s) * 1000.0 > self.config.episode_gap_ms:
                self._epoch = (self._epoch + 1) & 0xFF
            self._last_request_s = now
            last_error: Exception | None = None
            delivered = 0
            for _ in range(self.config.repeat):
                data = encode_trip(REASON_SAFETY_ZONE, SOURCE_CM5, self._epoch, self._next_sequence())
                message = self._factory(ESTOP_CAN_ID, data, False)
                try:
                    self.bus.send(message, timeout=0.05)
                    delivered += 1
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
            if delivered == 0:
                raise CanEStopError(f"the stop frame could not be sent: {type(last_error).__name__}: {last_error}") from last_error
            self.sent_count += 1
