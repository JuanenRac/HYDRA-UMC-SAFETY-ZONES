# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_can_estop_edges.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The frames and the decisions of the CAN link, at their edges: what makes a frame invalid (every length, the version, the kind, any corrupted byte), the check byte
against a known vector, a seeded walk of frames through the codec, the opening of a bus with the library missing or failing, and what `state_is_safe` says for every
shape of verdict. A heartbeat that goes out when the chain is not known to be healthy is the failure these tests are for. The bus is a stand-in; nothing is sent."""
from __future__ import annotations

import random
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from hydra_umc_safety_zones import can_estop as ce
from hydra_umc_safety_zones.estop import EStopRequest

NOW = 1_800_000_000.0


class Bus:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[object, float | None]] = []
        self.fail = fail

    def send(self, msg, timeout=None):
        if self.fail:
            raise OSError("bus off")
        self.sent.append((msg, timeout))

    def shutdown(self):
        pass


def factory(arbitration_id: int, data: bytes, extended: bool):
    return (arbitration_id, data, extended)


class TestCodec:
    def test_the_check_byte_is_crc8_with_the_polynomial_07_and_matches_the_standard_vector(self) -> None:
        assert ce.crc8(b"123456789") == 0xF4, "CRC-8/SMBUS check value"
        assert ce.crc8(b"") == 0
        assert ce.crc8(b"\x00") == 0

    def test_a_valid_frame_decodes_to_the_fields_that_were_put_in(self) -> None:
        data = ce.encode_frame(ce.KIND_STOP, 0x1234, 0xBEEF, 7)
        assert len(data) == ce.FRAME_LENGTH
        assert ce.decode_frame(data) == {"kind": ce.KIND_STOP, "sequence": 0x1234, "subject": 0xBEEF, "flags": 7}

    def test_a_seeded_walk_of_frames_round_trips_and_the_sequence_wraps_to_16_bits(self) -> None:
        rng = random.Random(8080)
        for _ in range(500):
            kind = rng.choice((ce.KIND_STOP, ce.KIND_HEARTBEAT))
            sequence, subject, flags = rng.randrange(0, 1 << 20), rng.randrange(0, 1 << 20), rng.randrange(0, 1 << 10)
            decoded = ce.decode_frame(ce.encode_frame(kind, sequence, subject, flags))
            assert decoded == {"kind": kind, "sequence": sequence & 0xFFFF, "subject": subject & 0xFFFF, "flags": flags & 0xFF}

    @pytest.mark.parametrize("length", [0, 1, 7, 9, 16])
    def test_a_frame_of_another_length_is_refused(self, length: int) -> None:
        with pytest.raises(ValueError, match="8 data bytes"):
            ce.decode_frame(bytes(length))

    def test_another_version_is_refused_even_with_a_matching_check_byte(self) -> None:
        body = bytes([ce.KIND_STOP, ce.FRAME_VERSION + 1, 0, 0, 0, 0, 0])
        with pytest.raises(ValueError, match="version"):
            ce.decode_frame(body + bytes([ce.crc8(body)]))

    def test_an_unknown_kind_is_refused_even_with_a_matching_check_byte(self) -> None:
        for kind in (0x00, 0x03, 0x7F, 0xFF):
            body = bytes([kind, ce.FRAME_VERSION, 0, 0, 0, 0, 0])
            with pytest.raises(ValueError, match="unknown frame kind"):
                ce.decode_frame(body + bytes([ce.crc8(body)]))

    def test_any_single_changed_byte_of_a_frame_is_caught(self) -> None:
        data = ce.encode_frame(ce.KIND_HEARTBEAT, 513, 77, 1)
        for position in range(ce.FRAME_LENGTH):
            for flip in (0x01, 0x80, 0xFF):
                changed = bytearray(data)
                changed[position] ^= flip
                with pytest.raises(ValueError):
                    ce.decode_frame(bytes(changed))

    def test_the_name_of_a_zone_becomes_a_stable_16_bit_number_and_different_names_differ(self) -> None:
        assert ce.fnv16("press") == ce.fnv16("press")
        assert all(0 <= ce.fnv16(name) <= 0xFFFF for name in ("", "a", "press", "zona-ñandú", "x" * 500))
        names = [f"zone-{i}" for i in range(200)]
        assert len({ce.fnv16(name) for name in names}) > 190, "a 16-bit hash of 200 names collides rarely"
        assert ce.fnv16("press") != ce.fnv16("Press")


class TestOpenBus:
    def test_without_python_can_it_says_what_to_install(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "can", None)
        with pytest.raises(ce.CanEStopError, match="python-can is not installed"):
            ce.open_bus(ce.CanEStopConfig(enabled=True))

    def test_a_bus_that_cannot_be_opened_is_an_error_with_the_cause(self, monkeypatch) -> None:
        library = types.ModuleType("can")

        def bus(**kwargs):
            raise PermissionError("no access to can0")

        library.Bus = bus  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "can", library)
        with pytest.raises(ce.CanEStopError, match="could not be opened: PermissionError: no access to can0"):
            ce.open_bus(ce.CanEStopConfig(enabled=True))

    def test_the_interface_channel_and_bitrate_of_the_configuration_are_what_is_asked_of_the_library(self, monkeypatch) -> None:
        seen: dict = {}
        library = types.ModuleType("can")

        def bus(**kwargs):
            seen.update(kwargs)
            return "the-bus"

        library.Bus = bus  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "can", library)
        config = ce.CanEStopConfig(enabled=True, interface="virtual", channel="vcan7", bitrate=250000)
        assert ce.open_bus(config) == "the-bus"
        assert seen == {"interface": "virtual", "channel": "vcan7", "bitrate": 250000}

    def test_a_message_is_made_by_the_library_with_the_identifier_data_and_width(self, monkeypatch) -> None:
        library = types.ModuleType("can")
        library.Message = lambda **kwargs: kwargs  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "can", library)
        assert ce.python_can_message(0x123, b"\x01\x02", True) == {"arbitration_id": 0x123, "data": b"\x01\x02", "is_extended_id": True}
        assert ce.python_can_message(0x1, b"", False)["is_extended_id"] is False


class TestStopFrames:
    def request(self, *zones: str) -> EStopRequest:
        return EStopRequest(object_id="o1", zone_id=zones[0] if zones else "z", reason="inside", zone_ids=tuple(zones))

    def test_the_frame_names_the_first_zone_and_counts_all_of_them(self) -> None:
        bus = Bus()
        requester = ce.CanEStopRequester(bus, ce.CanEStopConfig(enabled=True, repeat=2), factory)
        requester.send(self.request("press", "belt", "door"))
        assert len(bus.sent) == 2, "repeated, because a bus may drop a frame"
        arbitration, data, extended = bus.sent[0][0]
        decoded = ce.decode_frame(data)
        assert (arbitration, extended) == (0x001, False)
        assert decoded["kind"] == ce.KIND_STOP and decoded["subject"] == ce.fnv16("press") and decoded["flags"] == 3
        assert bus.sent[0][1] == 0.05, "a send never waits for long"

    def test_a_request_without_a_list_of_zones_uses_the_single_zone_and_the_count_is_capped_at_255(self) -> None:
        bus = Bus()
        requester = ce.CanEStopRequester(bus, ce.CanEStopConfig(enabled=True, repeat=1), factory)
        requester.send(EStopRequest(object_id="o", zone_id="solo", reason="r"))
        assert ce.decode_frame(bus.sent[0][0][1])["flags"] == 1
        requester.send(EStopRequest(object_id="o", zone_id="a", reason="r", zone_ids=tuple(f"z{i}" for i in range(300))))
        assert ce.decode_frame(bus.sent[1][0][1])["flags"] == 255

    def test_each_stop_has_its_own_sequence_number(self) -> None:
        bus = Bus()
        requester = ce.CanEStopRequester(bus, ce.CanEStopConfig(enabled=True, repeat=1), factory)
        for _ in range(3):
            requester.send(self.request("z"))
        assert [ce.decode_frame(m[0][1])["sequence"] for m in bus.sent] == [1, 2, 3]
        assert requester.sent_count == 3

    def test_a_bus_that_fails_every_time_is_an_error_and_is_not_counted_as_sent(self) -> None:
        requester = ce.CanEStopRequester(Bus(fail=True), ce.CanEStopConfig(enabled=True, repeat=3), factory)
        with pytest.raises(ce.CanEStopError, match="OSError: bus off"):
            requester.send(self.request("z"))
        assert requester.sent_count == 0

    def test_one_frame_that_goes_out_of_several_tries_is_a_success(self) -> None:
        class Flaky(Bus):
            def send(self, msg, timeout=None):
                if not self.sent and not getattr(self, "tried", False):
                    self.tried = True
                    raise OSError("first one lost")
                super().send(msg, timeout)

        bus = Flaky()
        requester = ce.CanEStopRequester(bus, ce.CanEStopConfig(enabled=True, repeat=3), factory)
        requester.send(self.request("z"))
        assert len(bus.sent) == 2 and requester.sent_count == 1


class TestHeartbeat:
    def test_a_check_that_raises_is_not_safe_and_nothing_is_sent(self) -> None:
        bus = Bus()

        def broken() -> bool:
            raise RuntimeError("no verdict")

        sender = ce.HeartbeatSender(bus, ce.CanEStopConfig(enabled=True), broken, factory)
        assert sender.beat() is False
        assert (sender.sent, sender.withheld, sender.errors, bus.sent) == (0, 1, 0, [])

    def test_a_value_that_is_only_truthy_counts_by_its_truth(self) -> None:
        bus = Bus()
        sender = ce.HeartbeatSender(bus, ce.CanEStopConfig(enabled=True), lambda: "yes", factory)  # type: ignore[arg-type,return-value]
        assert sender.beat() is True
        sender = ce.HeartbeatSender(bus, ce.CanEStopConfig(enabled=True), lambda: 0, factory)  # type: ignore[arg-type,return-value]
        assert sender.beat() is False

    def test_a_send_that_fails_is_counted_and_reported_as_not_sent(self) -> None:
        sender = ce.HeartbeatSender(Bus(fail=True), ce.CanEStopConfig(enabled=True), lambda: True, factory)
        assert sender.beat() is False
        assert (sender.sent, sender.errors, sender.withheld) == (0, 1, 0)

    def test_the_heartbeat_goes_out_on_its_own_identifier_with_a_growing_sequence(self) -> None:
        bus = Bus()
        sender = ce.HeartbeatSender(bus, ce.CanEStopConfig(enabled=True, heartbeat_id=0x7F0), lambda: True, factory)
        for _ in range(3):
            assert sender.beat() is True
        assert [m[0][0] for m in bus.sent] == [0x7F0] * 3
        assert [ce.decode_frame(m[0][1])["sequence"] for m in bus.sent] == [1, 2, 3]
        assert all(ce.decode_frame(m[0][1])["kind"] == ce.KIND_HEARTBEAT for m in bus.sent)

    def test_starting_twice_starts_one_thread_and_stopping_ends_it(self) -> None:
        sender = ce.HeartbeatSender(Bus(), ce.CanEStopConfig(enabled=True, heartbeat_period_ms=10), lambda: True, factory)
        sender.start()
        first = sender._thread  # noqa: SLF001
        sender.start()
        assert sender._thread is first  # noqa: SLF001
        sender.stop()
        assert first is not None and not first.is_alive()

    def test_stopping_a_sender_that_never_started_is_fine(self) -> None:
        ce.HeartbeatSender(Bus(), ce.CanEStopConfig(enabled=True), lambda: True, factory).stop()


def verdict(state: str = "READY", stamp: object = None, **extra) -> dict:
    when = stamp if stamp is not None else datetime.fromtimestamp(NOW, tz=timezone.utc).isoformat()
    return {"sdkSafetyState": {"state": state, "timestamp_utc": when}, **extra}


class TestStateIsSafe:
    def safe(self, value, max_age_ms: int = 1000) -> bool:
        return ce.state_is_safe(value, max_age_ms, now_s=lambda: NOW)

    def test_no_verdict_or_a_verdict_that_is_not_an_object_is_not_safe(self) -> None:
        for value in (None, {}, {"sdkSafetyState": None}, {"sdkSafetyState": "READY"}, {"sdkSafetyState": []}):
            assert self.safe(value) is False

    def test_only_ready_is_safe_among_the_states_and_a_misspelled_state_is_not(self) -> None:
        assert self.safe(verdict("READY")) is True
        for state in ("WARNING", "SAFE_STOP", "INHIBITED", "ready", "", "UNKNOWN"):
            assert self.safe(verdict(state)) is False, state

    def test_inhibited_by_the_house_alarm_alone_keeps_the_heartbeat_and_nothing_else_does(self) -> None:
        assert self.safe(verdict("INHIBITED", armorInhibitOnly=True)) is True
        for flag in (False, None, "true", 1, 0):
            assert self.safe(verdict("INHIBITED", armorInhibitOnly=flag)) is False, repr(flag)
        assert self.safe(verdict("SAFE_STOP", armorInhibitOnly=True)) is False
        assert self.safe(verdict("WARNING", armorInhibitOnly=True)) is False

    def test_the_age_is_inclusive_at_the_limit_in_both_directions(self) -> None:
        def at(offset_ms: float) -> str:
            return (datetime.fromtimestamp(NOW, tz=timezone.utc) - timedelta(milliseconds=offset_ms)).isoformat()
        assert self.safe(verdict(stamp=at(1000))) is True
        assert self.safe(verdict(stamp=at(1001))) is False
        assert self.safe(verdict(stamp=at(-1000))) is True, "a clock a second ahead is tolerated"
        assert self.safe(verdict(stamp=at(-1001))) is False
        assert self.safe(verdict(stamp=at(100)), max_age_ms=50) is False
        assert self.safe(verdict(stamp=at(40)), max_age_ms=50) is True

    def test_a_time_with_a_z_a_time_with_no_zone_and_a_time_with_an_offset_are_all_read(self) -> None:
        base = datetime.fromtimestamp(NOW, tz=timezone.utc)
        assert self.safe(verdict(stamp=base.strftime("%Y-%m-%dT%H:%M:%S") + "Z")) is True
        assert self.safe(verdict(stamp=base.strftime("%Y-%m-%dT%H:%M:%S"))) is True, "no zone means UTC"
        plus_two = base.astimezone(timezone(timedelta(hours=2))).isoformat()
        assert plus_two.endswith("+02:00")
        assert self.safe(verdict(stamp=plus_two)) is True

    def test_a_time_that_is_missing_unreadable_or_not_text_is_not_safe(self) -> None:
        for stamp in ("", "yesterday", "2026-13-45T99:99:99", 1_800_000_000, None):
            value = verdict(stamp=stamp) if stamp is not None else {"sdkSafetyState": {"state": "READY"}}
            assert self.safe(value) is False, repr(stamp)

    def test_the_real_clock_is_used_when_none_is_given(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        assert ce.state_is_safe(verdict(stamp=now), 5000) is True
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert ce.state_is_safe(verdict(stamp=old), 5000) is False
