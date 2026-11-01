# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_signing_and_can.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""The signed safety state (signing.py) and the E-STOP request and safe heartbeat over CAN (can_estop.py), with a simulated bus."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from hydra_umc_safety_zones.api import SafetyZonesServer
from hydra_umc_safety_zones.can_estop import (
    KIND_HEARTBEAT,
    KIND_STOP,
    CanConfigError,
    CanEStopConfig,
    CanEStopError,
    CanEStopRequester,
    HeartbeatSender,
    crc8,
    decode_frame,
    encode_frame,
    fnv16,
    load_config,
    parse_config,
    state_is_safe,
)
from hydra_umc_safety_zones.estop import EStopRequest
from hydra_umc_safety_zones.signing import (
    ALGORITHM,
    SigningError,
    StateSigner,
    effective_state,
    load_key_file,
    verify,
)

KEY = b"k" * 32


def _state(state: str = "READY", age_ms: int = 0) -> dict:
    when = datetime.now(timezone.utc) - timedelta(milliseconds=age_ms)
    return {"schema_version": "1.0", "state": state, "source": "hydra-umc-safety-zones", "timestamp_utc": when.isoformat()}


# --- the signature ---


def test_a_signed_verdict_verifies_and_the_counter_only_goes_up() -> None:
    signer = StateSigner(KEY, "line1", clock_ms=lambda: 1000)
    state = _state()
    first, second = signer.sign(state), signer.sign(state)
    assert first["alg"] == ALGORITHM and first["key_id"] == "line1"
    assert second["counter"] > first["counter"]  # the clock did not move and the counter still did
    result = verify(state, first, KEY, key_id="line1")
    assert result.ok and result.counter == first["counter"]


def test_the_counter_is_never_lower_than_the_clock() -> None:
    now = [5_000]
    signer = StateSigner(KEY, clock_ms=lambda: now[0])
    a = signer.sign(_state())["counter"]
    now[0] = 1  # the clock went back
    assert signer.sign(_state())["counter"] > a


def test_a_changed_state_a_wrong_key_or_another_key_id_does_not_verify() -> None:
    signer = StateSigner(KEY, "line1")
    state = _state("INHIBITED")
    signature = signer.sign(state)
    assert not verify({**state, "state": "READY"}, signature, KEY).ok
    assert not verify(state, signature, b"x" * 32).ok
    assert not verify(state, signature, KEY, key_id="other").ok
    assert not verify(state, {**signature, "counter": signature["counter"] + 1}, KEY).ok
    assert not verify(state, {**signature, "alg": "none"}, KEY).ok
    assert not verify(state, {**signature, "counter": True}, KEY).ok


def test_an_old_or_replayed_verdict_is_refused() -> None:
    signer = StateSigner(KEY)
    old = _state(age_ms=10_000)
    assert "old" in verify(old, signer.sign(old), KEY, max_age_ms=2000).reason
    state = _state()
    signature = signer.sign(state)
    assert verify(state, signature, KEY, last_counter=signature["counter"] - 1).ok
    replay = verify(state, signature, KEY, last_counter=signature["counter"])
    assert not replay.ok and "replay" in replay.reason


def test_a_consumer_reads_anything_not_valid_as_inhibited() -> None:
    signer = StateSigner(KEY)
    state = _state("READY")
    assert effective_state(state, signer.sign(state), KEY)[0] == "READY"
    assert effective_state(state, None, KEY)[0] == "INHIBITED"
    assert effective_state(state, {"alg": ALGORITHM, "key_id": "default", "counter": 1, "sig": "0" * 64}, KEY)[0] == "INHIBITED"
    assert effective_state("READY", signer.sign(state), KEY)[0] == "INHIBITED"
    stale = _state("READY", age_ms=60_000)
    assert effective_state(stale, signer.sign(stale), KEY)[0] == "INHIBITED"


def test_the_key_must_be_long_enough_and_its_file_readable(tmp_path) -> None:
    with pytest.raises(SigningError):
        StateSigner(b"short")
    with pytest.raises(SigningError):
        StateSigner(KEY, "bad id!")
    path = tmp_path / "key"
    path.write_text("k" * 32 + "\n", encoding="utf-8")
    assert load_key_file(path) == KEY
    path.write_text("short", encoding="utf-8")
    with pytest.raises(SigningError, match="at least 32"):
        load_key_file(path)
    with pytest.raises(SigningError):
        load_key_file(tmp_path / "missing")


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _zones() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "zones": [{"id": "danger1", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 2, "y": 2, "z": 2}}],
        "calibration": {"version": "c", "source": "manual", "calibrated_at": today, "max_age_days": 30},
    }


def _check_body(x: float) -> dict:
    return {
        "zones": _zones(),
        "detections": {"objects": [{"id": "op1", "position": {"x": x, "y": x, "z": x}}]},
        "observation": {"active": True, "observedAt": datetime.now(timezone.utc).isoformat(), "maxAgeSeconds": 3600, "error": None},
    }


def _serve(server: SafetyZonesServer) -> str:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


def test_the_service_signs_every_verdict_when_it_has_a_key() -> None:
    server = SafetyZonesServer(("127.0.0.1", 0), "test", signer=StateSigner(KEY, "line1"))
    try:
        base = _serve(server)
        body = _post(f"{base}/check", _check_body(50))
        assert body["sdkSafetyState"]["state"] == "READY"
        assert verify(body["sdkSafetyState"], body["sdkSafetyStateSignature"], KEY, key_id="line1").ok
        # the last verdict that GET /state serves carries the signature too
        remembered = server.last_verdict()
        assert remembered is not None and remembered["sdkSafetyStateSignature"]["counter"] == body["sdkSafetyStateSignature"]["counter"]
    finally:
        server.shutdown()
        server.server_close()


def test_the_service_does_not_sign_by_default() -> None:
    server = SafetyZonesServer(("127.0.0.1", 0), "test")
    try:
        body = _post(f"{_serve(server)}/check", _check_body(50))
        assert "sdkSafetyStateSignature" not in body
    finally:
        server.shutdown()
        server.server_close()


# --- the CAN frames and the configuration ---


class FakeBus:
    def __init__(self, fail: bool = False) -> None:
        self.frames: list[tuple[int, bytes]] = []
        self.fail = fail

    def send(self, msg, timeout=None) -> None:  # noqa: ANN001
        if self.fail:
            raise OSError("bus off")
        self.frames.append(msg)

    def shutdown(self) -> None:
        pass


def _factory(arbitration_id: int, data: bytes, extended_id: bool):
    return (arbitration_id, data, extended_id)


def test_frames_have_a_check_byte_and_decode_back() -> None:
    frame = encode_frame(KIND_STOP, 0x1234, fnv16("danger1"), 2)
    assert len(frame) == 8 and frame[7] == crc8(frame[:7])
    assert decode_frame(frame) == {"kind": KIND_STOP, "sequence": 0x1234, "subject": fnv16("danger1"), "flags": 2}
    broken = bytes([frame[0] ^ 0x80]) + frame[1:]
    with pytest.raises(ValueError):
        decode_frame(broken)
    with pytest.raises(ValueError):
        decode_frame(frame[:7])
    assert crc8(b"123456789") == 0xF4  # the standard CRC-8 check value


def test_the_stop_request_goes_out_repeated_on_the_stop_identifier() -> None:
    bus = FakeBus()
    config = CanEStopConfig(enabled=True, repeat=3)
    requester = CanEStopRequester(bus, config, _factory)
    requester.send(EStopRequest("op1", "danger1", "breach", ("danger1", "danger2")))
    assert len(bus.frames) == 3
    arbitration_id, data, extended = bus.frames[0]
    assert (arbitration_id, extended) == (config.estop_id, False)
    fields = decode_frame(data)
    assert fields["kind"] == KIND_STOP and fields["subject"] == fnv16("danger1") and fields["flags"] == 2


def test_a_stop_that_could_not_be_sent_is_an_error_not_a_silent_success() -> None:
    requester = CanEStopRequester(FakeBus(fail=True), CanEStopConfig(enabled=True), _factory)
    with pytest.raises(CanEStopError, match="bus off"):
        requester.send(EStopRequest("op1", "danger1", "breach"))
    assert requester.sent_count == 0


def test_the_heartbeat_goes_out_only_while_the_chain_is_safe() -> None:
    bus = FakeBus()
    safe = [True]
    sender = HeartbeatSender(bus, CanEStopConfig(enabled=True), lambda: safe[0], _factory)
    assert sender.beat() is True
    assert decode_frame(bus.frames[0][1])["kind"] == KIND_HEARTBEAT and bus.frames[0][0] == CanEStopConfig().heartbeat_id
    safe[0] = False
    assert sender.beat() is False and len(bus.frames) == 1 and sender.withheld == 1

    def boom() -> bool:
        raise RuntimeError("no verdict")

    sender.is_safe = boom
    assert sender.beat() is False  # a check that fails counts as not safe
    failing = HeartbeatSender(FakeBus(fail=True), CanEStopConfig(enabled=True), lambda: True, _factory)
    assert failing.beat() is False and failing.errors == 1


def test_the_heartbeat_thread_beats_and_goes_silent_when_the_state_changes() -> None:
    bus = FakeBus()
    safe = [True]
    sender = HeartbeatSender(bus, CanEStopConfig(enabled=True, heartbeat_period_ms=10), lambda: safe[0], _factory)
    sender.start()
    try:
        deadline = time.time() + 3
        while len(bus.frames) < 3 and time.time() < deadline:
            time.sleep(0.01)
        assert len(bus.frames) >= 3
        safe[0] = False
        time.sleep(0.05)
        count = len(bus.frames)
        time.sleep(0.1)
        assert len(bus.frames) == count, "no heartbeat may go out after the chain stopped being safe"
    finally:
        sender.stop()


def test_a_state_is_safe_only_when_ready_and_fresh() -> None:
    ready = {"sdkSafetyState": _state("READY")}
    assert state_is_safe(ready, 1000)
    assert not state_is_safe({"sdkSafetyState": _state("READY", age_ms=5000)}, 1000)
    assert not state_is_safe({"sdkSafetyState": _state("INHIBITED")}, 1000)
    assert not state_is_safe({"sdkSafetyState": _state("SAFE_STOP")}, 1000)
    assert not state_is_safe(None, 1000)
    assert not state_is_safe({"sdkSafetyState": {"state": "READY"}}, 1000)


def test_the_configuration_is_checked(tmp_path) -> None:
    assert parse_config({"enabled": True}).estop_id == 0x001
    for bad in (
        [],
        {"enabled": "yes"},
        {"nope": 1},
        {"bitrate": 5},
        {"repeat": 0},
        {"estop_id": 0x800},
        {"estop_id": 5, "heartbeat_id": 5},
        {"estop_id": 9, "heartbeat_id": 3},
        {"heartbeat_period_ms": 500, "state_max_age_ms": 100},
        {"channel": ""},
    ):
        with pytest.raises(CanConfigError):
            parse_config(bad)
    assert parse_config({"extended_id": True, "estop_id": 0x800, "heartbeat_id": 0x801}).extended_id
    path = tmp_path / "can_estop.json"
    path.write_text(json.dumps({"enabled": True, "interface": "virtual", "channel": "sim"}), encoding="utf-8")
    assert load_config(path).interface == "virtual"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(CanConfigError, match="not valid JSON"):
        load_config(path)
    with pytest.raises(CanConfigError):
        load_config(tmp_path / "missing.json")


def test_a_danger_verdict_sends_the_stop_through_the_service_and_a_failing_bus_does_not_hide_it() -> None:
    bus = FakeBus()
    ok = SafetyZonesServer(("127.0.0.1", 0), "test", estop_requester=CanEStopRequester(bus, CanEStopConfig(enabled=True, repeat=1), _factory))
    broken = SafetyZonesServer(("127.0.0.1", 0), "test", estop_requester=CanEStopRequester(FakeBus(fail=True), CanEStopConfig(enabled=True), _factory))
    try:
        body = _post(f"{_serve(ok)}/check", _check_body(1))
        assert body["state"] == "danger" and len(body["estopRequests"]) == 1 and len(bus.frames) == 1
        failed = _post(f"{_serve(broken)}/check", _check_body(1))
        assert failed["state"] == "danger" and failed["sdkSafetyState"]["state"] == "SAFE_STOP"
        assert failed["estopRequests"] == [] and "CanEStopError" in failed["estopTransportError"]
    finally:
        for server in (ok, broken):
            server.shutdown()
            server.server_close()


def test_python_can_virtual_bus_carries_the_frames_when_the_library_is_installed() -> None:
    can = pytest.importorskip("can")
    config = CanEStopConfig(enabled=True, interface="virtual", channel="safety-zones-test", repeat=1)
    sender_bus = can.Bus(interface="virtual", channel="safety-zones-test")
    receiver = can.Bus(interface="virtual", channel="safety-zones-test")
    try:
        CanEStopRequester(sender_bus, config).send(EStopRequest("op1", "danger1", "breach"))
        message = receiver.recv(timeout=1)
        assert message is not None and message.arbitration_id == config.estop_id
        assert decode_frame(bytes(message.data))["kind"] == KIND_STOP
    finally:
        sender_bus.shutdown()
        receiver.shutdown()
