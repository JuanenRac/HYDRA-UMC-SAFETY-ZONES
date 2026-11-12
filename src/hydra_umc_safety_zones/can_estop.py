# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/can_estop.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The OLDER E-STOP frames of this service on a CAN bus (identifiers 1 and 2 with a check byte), and the "safe" heartbeat that makes the chain fail safe.

The stop frame of the stack is the one HYDRA-UMC defines on identifier 0x001 (`hydra_estop.py`), and it is what the service sends by default (`"protocol": "hydra"`).
The frames below stay as a secondary path, selected with `"protocol": "legacy"`; this module also holds the configuration of both.

This implements the `EStopRequester` of `estop.py` over python-can. It still only REQUESTS a stop: the board that receives the frame
decides what to do with it and is the one that cuts power. Two frames, both with 8 data bytes:

    byte 0      kind        0x01 STOP REQUEST, 0x02 SAFE HEARTBEAT
    byte 1      version     1
    bytes 2-3   sequence    little-endian, wraps at 65535
    bytes 4-5   subject     STOP: 16-bit FNV-1a of the first zone id that was breached; HEARTBEAT: 0
    byte 6      flags       STOP: number of zones breached (at most 255); HEARTBEAT: 0
    byte 7      check       CRC-8 (polynomial 0x07) of bytes 0..6

Two arbitration identifiers (the lowest numbers win arbitration, so the stop comes before anything else on the bus): `estop_id` for the
stop request and `heartbeat_id` for the heartbeat. The identifiers, the period and everything else are in the CAN configuration file
(`can_estop.json`), not in the code.

The heartbeat is what turns the chain into a fail-safe one: the service sends it every `heartbeat_period_ms` ONLY while its last
verdict is READY and not older than `state_max_age_ms`. A board that stops seeing it must stop the machine, so a service that crashed,
froze, lost its observer or turned INHIBITED stops the machine just by going silent, without anything having to be sent.

Not tried on a real bus: the frame is a proposal for the firmware to adopt (it has to be agreed with HYDRA-UMC and the SDK), and it is
tested here with a simulated bus.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from hydra_umc_safety_zones.estop import EStopRequest

KIND_STOP = 0x01
KIND_HEARTBEAT = 0x02
FRAME_VERSION = 1
FRAME_LENGTH = 8


class CanConfigError(ValueError):
    """The CAN configuration cannot be used."""


class CanEStopError(RuntimeError):
    """The frame could not be sent. The request was NOT delivered."""


@dataclass(frozen=True)
class CanEStopConfig:
    enabled: bool = False
    interface: str = "socketcan"
    channel: str = "can0"
    bitrate: int = 500000
    extended_id: bool = False
    estop_id: int = 0x001
    heartbeat_id: int = 0x002
    heartbeat_period_ms: int = 100
    state_max_age_ms: int = 1000
    # how many times a stop frame is sent in a row (a bus may drop a frame)
    repeat: int = 3
    # "hydra": the TRIP of identifier 0x001 of HYDRA-UMC (hydra_estop.py); "legacy": the older frames below (identifiers estop_id and heartbeat_id)
    protocol: str = "hydra"
    # hydra: a request this long after the previous one starts a new stop (the epoch of the frame goes up by one)
    episode_gap_ms: int = 1000
    # hydra: also send the older heartbeat on heartbeat_id as a secondary path (ALIVE on 0x001 belongs to the brain alone and is never sent from here)
    legacy_heartbeat: bool = False


PROTOCOLS = ("hydra", "legacy")

_INT_BOUNDS = {
    "episode_gap_ms": (50, 60_000),
    "bitrate": (10_000, 1_000_000),
    "heartbeat_period_ms": (10, 5_000),
    "state_max_age_ms": (50, 60_000),
    "repeat": (1, 10),
}


def parse_config(raw: object) -> CanEStopConfig:
    """Reads the configuration; a field that is wrong stops the start instead of being ignored."""
    if not isinstance(raw, dict):
        raise CanConfigError("the CAN configuration must be a JSON object")
    known = set(CanEStopConfig.__dataclass_fields__)
    unknown = sorted(set(raw) - known)
    if unknown:
        raise CanConfigError(f"unknown field(s) in the CAN configuration: {', '.join(unknown)}")
    values: dict[str, Any] = dict(raw)
    for name in ("enabled", "extended_id", "legacy_heartbeat"):
        if name in values and not isinstance(values[name], bool):
            raise CanConfigError(f"{name} must be true or false")
    if "protocol" in values and values["protocol"] not in PROTOCOLS:
        raise CanConfigError(f"protocol must be one of: {', '.join(PROTOCOLS)}")
    for name in ("interface", "channel"):
        if name in values and (not isinstance(values[name], str) or not values[name] or len(values[name]) > 64):
            raise CanConfigError(f"{name} must be a non-empty text of at most 64 characters")
    for name, (low, high) in _INT_BOUNDS.items():
        if name in values:
            v = values[name]
            if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
                raise CanConfigError(f"{name} must be a whole number from {low} to {high}")
    config = CanEStopConfig(**values)
    limit = 0x1FFFFFFF if config.extended_id else 0x7FF
    for name in ("estop_id", "heartbeat_id"):
        v = getattr(config, name)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= limit:
            raise CanConfigError(f"{name} must be a whole number from 0 to {limit}")
    if config.estop_id == config.heartbeat_id:
        raise CanConfigError("estop_id and heartbeat_id must differ")
    if config.estop_id > config.heartbeat_id:
        raise CanConfigError("estop_id must be lower than heartbeat_id: the stop has to win arbitration")
    if config.state_max_age_ms < config.heartbeat_period_ms:
        raise CanConfigError("state_max_age_ms must not be shorter than heartbeat_period_ms")
    if config.protocol == "hydra" and (config.estop_id != 0x001 or config.extended_id):
        raise CanConfigError('with protocol "hydra" the stop is on the standard identifier 1 (the contract of HYDRA-UMC); use protocol "legacy" for other identifiers')
    return config


def load_config(path: str | Path) -> CanEStopConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise CanConfigError(f"the CAN configuration cannot be read: {exc.strerror or type(exc).__name__}") from exc
    except ValueError as exc:
        raise CanConfigError(f"the CAN configuration is not valid JSON: {exc}") from exc
    return parse_config(raw)


def crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def fnv16(text: str) -> int:
    h = 0x811C9DC5
    for byte in text.encode("utf-8"):
        h = ((h ^ byte) * 0x01000193) & 0xFFFFFFFF
    return ((h >> 16) ^ h) & 0xFFFF


def encode_frame(kind: int, sequence: int, subject: int = 0, flags: int = 0) -> bytes:
    body = bytes([kind, FRAME_VERSION, sequence & 0xFF, (sequence >> 8) & 0xFF, subject & 0xFF, (subject >> 8) & 0xFF, flags & 0xFF])
    return body + bytes([crc8(body)])


def decode_frame(data: bytes) -> dict[str, int]:
    """The fields of a frame; raises ValueError when the length, the version or the check byte is wrong."""
    if len(data) != FRAME_LENGTH:
        raise ValueError("a frame has 8 data bytes")
    if data[1] != FRAME_VERSION:
        raise ValueError("unknown frame version")
    if crc8(data[:7]) != data[7]:
        raise ValueError("the check byte does not match")
    if data[0] not in (KIND_STOP, KIND_HEARTBEAT):
        raise ValueError("unknown frame kind")
    return {"kind": data[0], "sequence": data[2] | (data[3] << 8), "subject": data[4] | (data[5] << 8), "flags": data[6]}


class CanBus(Protocol):
    def send(self, msg: Any, timeout: float | None = None) -> None: ...

    def shutdown(self) -> None: ...


MessageFactory = Callable[[int, bytes, bool], Any]


def python_can_message(arbitration_id: int, data: bytes, extended_id: bool) -> Any:
    import can  # noqa: PLC0415 - python-can is optional; only a real bus needs it

    return can.Message(arbitration_id=arbitration_id, data=data, is_extended_id=extended_id)


def open_bus(config: CanEStopConfig) -> CanBus:
    """Opens the bus with python-can (an optional dependency: `pip install python-can`). `interface: "virtual"` is the simulator."""
    try:
        import can  # noqa: PLC0415
    except ImportError as exc:
        raise CanEStopError("python-can is not installed (pip install python-can)") from exc
    try:
        return can.Bus(interface=config.interface, channel=config.channel, bitrate=config.bitrate)  # type: ignore[no-any-return]
    except Exception as exc:  # noqa: BLE001 - python-can raises many kinds of error
        raise CanEStopError(f"the CAN bus could not be opened: {type(exc).__name__}: {exc}") from exc


class _Sequence:
    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self._n = (self._n + 1) & 0xFFFF
            return self._n


class CanEStopRequester:
    """An `EStopRequester` that sends the stop frame on a CAN bus. A send that fails raises `CanEStopError`: a request that did not go out is never reported as sent."""

    def __init__(self, bus: CanBus, config: CanEStopConfig, message_factory: MessageFactory = python_can_message, sequence: _Sequence | None = None) -> None:
        self.bus = bus
        self.config = config
        self._factory = message_factory
        self._sequence = sequence or _Sequence()
        self.sent_count = 0

    def send(self, request: EStopRequest) -> None:
        zones = request.zone_ids or (request.zone_id,)
        data = encode_frame(KIND_STOP, self._sequence.next(), fnv16(zones[0]), min(len(zones), 255))
        message = self._factory(self.config.estop_id, data, self.config.extended_id)
        last_error: Exception | None = None
        delivered = 0
        for _ in range(self.config.repeat):
            try:
                self.bus.send(message, timeout=0.05)
                delivered += 1
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        if delivered == 0:
            raise CanEStopError(f"the stop frame could not be sent: {type(last_error).__name__}: {last_error}") from last_error
        self.sent_count += 1


class HeartbeatSender:
    """Sends the safe heartbeat while `is_safe()` says the chain is healthy, and nothing otherwise."""

    def __init__(
        self,
        bus: CanBus,
        config: CanEStopConfig,
        is_safe: Callable[[], bool],
        message_factory: MessageFactory = python_can_message,
        sequence: _Sequence | None = None,
    ) -> None:
        self.bus = bus
        self.config = config
        self.is_safe = is_safe
        self._factory = message_factory
        self._sequence = sequence or _Sequence()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.sent = 0
        self.withheld = 0
        self.errors = 0

    def beat(self) -> bool:
        """One period: sends the heartbeat when the chain is safe. Returns whether a frame went out."""
        try:
            safe = bool(self.is_safe())
        except Exception:  # noqa: BLE001 - a check that fails is "not safe"
            safe = False
        if not safe:
            self.withheld += 1
            return False
        message = self._factory(self.config.heartbeat_id, encode_frame(KIND_HEARTBEAT, self._sequence.next()), self.config.extended_id)
        try:
            self.bus.send(message, timeout=0.05)
        except Exception:  # noqa: BLE001
            self.errors += 1
            return False
        self.sent += 1
        return True

    def start(self) -> None:
        if self._thread is not None:
            return

        def run() -> None:
            period = self.config.heartbeat_period_ms / 1000.0
            while not self._stop.wait(period):
                self.beat()

        self._thread = threading.Thread(target=run, name="safety-zones-can-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def state_is_safe(last_verdict: dict | None, max_age_ms: int, now_s: Callable[[], float] = time.time) -> bool:
    """True when the last verdict is READY (or INHIBITED only by the house alarm) and its timestamp is not older than `max_age_ms` (no verdict yet means not safe)."""
    from datetime import datetime, timezone

    if not last_verdict:
        return False
    sdk = last_verdict.get("sdkSafetyState")
    if not isinstance(sdk, dict):
        return False
    # A verdict that is INHIBITED only because the house alarm (A.R.M.O.R.) sounds keeps the heartbeat: it stops new starts, never a running machine.
    if sdk.get("state") != "READY" and not (sdk.get("state") == "INHIBITED" and last_verdict.get("armorInhibitOnly") is True):
        return False
    stamp = sdk.get("timestamp_utc")
    if not isinstance(stamp, str):
        return False
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age_ms = (now_s() - when.timestamp()) * 1000.0
    return -max_age_ms <= age_ms <= max_age_ms
