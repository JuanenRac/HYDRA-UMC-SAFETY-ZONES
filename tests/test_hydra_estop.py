# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_hydra_estop.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The stop request in the frame of identifier 0x001: the bytes against vectors made with the reference of HYDRA-UMC, the epoch and the sequence, the decoding, the
selection of the protocol in the configuration, and the old frames staying available. The bus is a stand-in; nothing is sent."""
from __future__ import annotations

import pytest

from hydra_umc_safety_zones import can_estop as ce
from hydra_umc_safety_zones import hydra_estop as he
from hydra_umc_safety_zones.estop import EStopRequest

REQUEST = EStopRequest(object_id="person-1", zone_id="cell-a", reason="danger", zone_ids=("cell-a", "cell-b"))


class Bus:
    def __init__(self, fail_first: int = 0, fail_all: bool = False) -> None:
        self.sent: list[tuple[int, bytes, bool]] = []
        self.fail_first = fail_first
        self.fail_all = fail_all

    def send(self, msg, timeout=None):
        if self.fail_all or self.fail_first > 0:
            self.fail_first -= 1
            raise OSError("bus off")
        self.sent.append(msg)

    def shutdown(self):
        pass


def factory(arbitration_id: int, data: bytes, extended: bool):
    return (arbitration_id, data, extended)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def config(**kw) -> ce.CanEStopConfig:
    return ce.CanEStopConfig(enabled=True, **kw)


class TestFrame:
    @pytest.mark.parametrize(
        ("epoch", "sequence", "hexa"),
        [(0, 1, "010410000100fefb"), (3, 0x1234, "010410033412fefb"), (255, 65535, "010410fffffffefb")],
    )
    def test_the_trip_is_the_same_bytes_as_the_reference_of_the_stack(self, epoch: int, sequence: int, hexa: str) -> None:
        assert he.encode_trip(he.REASON_SAFETY_ZONE, he.SOURCE_CM5, epoch, sequence).hex() == hexa

    def test_a_frame_round_trips_and_the_sequence_wraps_to_16_bits(self) -> None:
        decoded = he.decode_frame(he.encode_trip(0x04, 0x10, 7, 0x10005))
        assert decoded == {"kind": he.KIND_TRIP, "detail": 4, "source": 0x10, "epoch": 7, "sequence": 5}

    @pytest.mark.parametrize("bad", [b"", b"\x01" * 7, b"\x01" * 9])
    def test_a_wrong_length_is_refused(self, bad: bytes) -> None:
        with pytest.raises(ValueError):
            he.decode_frame(bad)

    def test_a_damaged_complement_or_an_unknown_kind_is_refused(self) -> None:
        good = bytearray(he.encode_trip(4, 0x10, 1, 1))
        for index in (6, 7):
            damaged = bytearray(good)
            damaged[index] ^= 0x01
            with pytest.raises(ValueError):
                he.decode_frame(bytes(damaged))
        unknown = bytes([0x09, 0x00, 0x00, 0x00, 0x00, 0x00, 0x09 ^ 0xFF, 0xFF])
        with pytest.raises(ValueError):
            he.decode_frame(unknown)

    def test_a_field_that_does_not_fit_a_byte_is_refused(self) -> None:
        for args in ((256, 0x10, 0, 1), (4, 256, 0, 1), (4, 0x10, 256, 1), (-1, 0x10, 0, 1)):
            with pytest.raises(ValueError):
                he.encode_trip(*args)

    def test_alive_and_release_decode_but_are_never_built_here(self) -> None:
        alive = bytes([he.KIND_ALIVE, 0, 0, 1, 1, 0, he.KIND_ALIVE ^ 0xFF, 0xFF])
        assert he.decode_frame(alive)["kind"] == he.KIND_ALIVE
        assert not hasattr(he, "encode_alive") and not hasattr(he, "encode_release")


class TestRequester:
    def test_a_stop_goes_on_0x001_as_a_trip_of_a_safety_zone_from_the_cm5(self) -> None:
        bus = Bus()
        requester = he.HydraStopRequester(bus, config(repeat=1), factory, Clock())
        requester.send(REQUEST)
        (arbitration_id, data, extended), = bus.sent
        assert (arbitration_id, extended) == (0x001, False)
        assert he.decode_frame(data) == {"kind": 1, "detail": 4, "source": 0x10, "epoch": 1, "sequence": 1}
        assert requester.sent_count == 1

    def test_the_identifier_is_fixed_by_the_contract_not_by_the_old_setting(self) -> None:
        bus = Bus()
        he.HydraStopRequester(bus, config(repeat=1, estop_id=0x05, heartbeat_id=0x06), factory, Clock()).send(REQUEST)
        assert bus.sent[0][0] == 0x001

    def test_every_repeated_frame_takes_its_own_sequence_and_the_epoch_stays(self) -> None:
        bus = Bus()
        requester = he.HydraStopRequester(bus, config(repeat=3), factory, Clock())
        requester.send(REQUEST)
        decoded = [he.decode_frame(m[1]) for m in bus.sent]
        assert [d["sequence"] for d in decoded] == [1, 2, 3]
        assert {d["epoch"] for d in decoded} == {1}

    def test_a_request_soon_after_the_last_is_the_same_stop_and_a_later_one_is_a_new_stop(self) -> None:
        bus, clock = Bus(), Clock()
        requester = he.HydraStopRequester(bus, config(repeat=1, episode_gap_ms=1000), factory, clock)
        requester.send(REQUEST)
        clock.now += 0.5
        requester.send(REQUEST)
        clock.now += 1.5
        requester.send(REQUEST)
        assert [he.decode_frame(m[1])["epoch"] for m in bus.sent] == [1, 1, 2]
        assert [he.decode_frame(m[1])["sequence"] for m in bus.sent] == [1, 2, 3]

    def test_the_epoch_wraps_at_256_and_the_sequence_at_65536(self) -> None:
        bus, clock = Bus(), Clock()
        requester = he.HydraStopRequester(bus, config(repeat=1, episode_gap_ms=50), factory, clock)
        requester._epoch = 255
        requester._sequence = 0xFFFF
        requester.send(REQUEST)
        frame = he.decode_frame(bus.sent[0][1])
        assert (frame["epoch"], frame["sequence"]) == (0, 0)
        assert requester.epoch == 0

    def test_one_lost_frame_of_several_is_not_an_error_but_all_lost_is(self) -> None:
        bus = Bus(fail_first=2)
        requester = he.HydraStopRequester(bus, config(repeat=3), factory, Clock())
        requester.send(REQUEST)
        assert len(bus.sent) == 1 and requester.sent_count == 1
        failing = he.HydraStopRequester(Bus(fail_all=True), config(repeat=3), factory, Clock())
        with pytest.raises(ce.CanEStopError):
            failing.send(REQUEST)
        assert failing.sent_count == 0

    def test_the_default_clock_and_factory_need_no_injection_for_a_failing_bus(self) -> None:
        requester = he.HydraStopRequester(Bus(fail_all=True), config(repeat=1), factory)
        with pytest.raises(ce.CanEStopError):
            requester.send(REQUEST)


class TestConfiguration:
    def test_the_stack_frame_is_the_default_and_the_old_one_is_asked_for_by_name(self) -> None:
        assert ce.parse_config({"enabled": True}).protocol == "hydra"
        assert ce.parse_config({"protocol": "legacy"}).protocol == "legacy"

    def test_an_unknown_protocol_stops_the_start(self) -> None:
        for bad in ("", "can", 1, None):
            with pytest.raises(ce.CanConfigError):
                ce.parse_config({"protocol": bad})

    def test_the_stack_frame_needs_the_standard_identifier_1_and_no_extended_one(self) -> None:
        for bad in ({"estop_id": 5, "heartbeat_id": 6}, {"extended_id": True}):
            with pytest.raises(ce.CanConfigError):
                ce.parse_config(bad)
        # the old frames keep their freedom
        assert ce.parse_config({"protocol": "legacy", "estop_id": 5, "heartbeat_id": 6}).estop_id == 5

    def test_the_gap_between_stops_and_the_old_heartbeat_switch_are_checked(self) -> None:
        assert ce.parse_config({}).episode_gap_ms == 1000
        assert ce.parse_config({}).legacy_heartbeat is False
        assert ce.parse_config({"legacy_heartbeat": True}).legacy_heartbeat is True
        for bad in ({"episode_gap_ms": 0}, {"episode_gap_ms": 60001}, {"episode_gap_ms": True}, {"legacy_heartbeat": 1}):
            with pytest.raises(ce.CanConfigError):
                ce.parse_config(bad)

    def test_the_old_requester_still_builds_the_old_frame_whatever_the_protocol_says(self) -> None:
        bus = Bus()
        ce.CanEStopRequester(bus, config(repeat=1), factory).send(REQUEST)
        assert ce.decode_frame(bus.sent[0][1])["kind"] == ce.KIND_STOP


class TestStart:
    @pytest.mark.parametrize(("protocol", "expected"), [("hydra", he.HydraStopRequester), ("legacy", ce.CanEStopRequester)])
    def test_serve_builds_the_requester_of_the_protocol_in_the_file(self, protocol, expected, tmp_path, monkeypatch) -> None:
        import json

        from hydra_umc_safety_zones import main as m

        path = tmp_path / "can_estop.json"
        path.write_text(json.dumps({"enabled": True, "protocol": protocol}), encoding="utf-8")
        monkeypatch.setattr(m, "open_bus", lambda config: Bus())
        requester, heartbeat, parsed = m._start_can(str(path))
        assert type(requester) is expected and parsed.protocol == protocol
        assert heartbeat.bus is requester.bus

    def test_a_frame_that_a_node_would_take_for_a_stop_never_comes_from_the_heartbeat_path(self) -> None:
        # The older heartbeat keeps its own identifier (2): nothing but the TRIP is ever put on 0x001 from this service.
        bus = Bus()
        sender = ce.HeartbeatSender(bus, config(), lambda: True, factory)
        assert sender.beat()
        assert bus.sent[0][0] == 0x002 != he.ESTOP_CAN_ID
