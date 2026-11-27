# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_state_expiry_and_gate_stop.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""GET /state does not serve an old verdict, and the safe stop of a droid or of the alarm can be requested on the bus. Simulated clock and bus."""
from __future__ import annotations

import pytest

from hydra_umc_safety_zones.api import DEFAULT_STATE_MAX_AGE_S, SafetyZonesServer
from hydra_umc_safety_zones.armor_alarm import ArmorAlarmConfigError, parse_settings as parse_alarm_settings
from hydra_umc_safety_zones.droid_estop import DroidEstopConfigError, DroidEstopGate, DroidEstopSettings, parse_settings as parse_droid_settings
from hydra_umc_safety_zones.estop import NullEStopRequester
from hydra_umc_safety_zones.stateful import StatefulSettings

from test_droid_estop import STATE, FakeClock, _call, _check_body, _server, state


def _cut_gate(clock: FakeClock, **settings: object) -> DroidEstopGate:
    gate = DroidEstopGate(DroidEstopSettings(enabled=True, hysteresis_s=0.0, **settings))  # type: ignore[arg-type]
    gate.ingest(STATE, state(ready=False, cut="cut"), clock())
    return gate


def test_an_old_verdict_is_answered_as_inhibited() -> None:
    clock = FakeClock()
    with _server(None, clock) as (base, server):
        _call(base, "POST", "/check", _check_body())
        assert _call(base, "GET", "/state")[1]["sdkSafetyState"]["state"] == "READY"
        clock.now += DEFAULT_STATE_MAX_AGE_S + 0.1
        answer = _call(base, "GET", "/state")[1]
        assert answer["sdkSafetyState"]["state"] != "READY" and "nothing renewed it" in answer["reason"]
        server.state_max_age_s = 0.0  # the switch: the old behaviour
        assert _call(base, "GET", "/state")[1]["sdkSafetyState"]["state"] == "READY"


@pytest.mark.parametrize("body", [{}, {"zones": [], "detections": "x"}, {"zones": 3, "detections": []}])
def test_a_refused_check_does_not_leave_the_earlier_ready_behind(body: dict) -> None:
    with _server(None, FakeClock()) as (base, _):
        _call(base, "POST", "/check", _check_body())
        assert _call(base, "POST", "/check", body)[0] == 400
        answer = _call(base, "GET", "/state")[1]
        assert answer["sdkSafetyState"]["state"] != "READY" and answer["reason"].startswith("a check was refused")


def test_the_safe_stop_of_a_droid_requests_nothing_unless_it_is_asked_to() -> None:
    clock = FakeClock()
    requester = NullEStopRequester()
    with _server(_cut_gate(clock, safe_stop=True), clock) as (base, server):
        server.estop_requester = requester
        answer = _call(base, "POST", "/check", _check_body())[1]
        server.watch_once()
        assert answer["state"] == "danger" and answer["estopRequests"] == [] and requester.sent == []


def test_with_request_stop_the_stop_is_requested_with_its_own_reason_even_when_nobody_checks() -> None:
    clock = FakeClock()
    requester = NullEStopRequester()
    with _server(_cut_gate(clock, safe_stop=True, request_stop=True), clock) as (base, server):
        server.estop_requester = requester
        server.watch_once()
        assert [request.zone_id for request in requester.sent] == ["droid-estop"]
        answer = _call(base, "POST", "/check", _check_body())[1]
        assert answer["state"] == "danger" and answer["estopRequests"][0]["reason"].startswith("droid emergency stop safe stop:")


def test_a_bus_that_fails_does_not_hide_the_verdict() -> None:
    class Broken:
        def send(self, request: object) -> None:
            raise OSError("no bus")

    clock = FakeClock()
    with _server(_cut_gate(clock, safe_stop=True, request_stop=True), clock) as (base, server):
        server.estop_requester = Broken()
        server.watch_once()
        assert server.gate_stop_error == "OSError"
        status, answer = _call(base, "POST", "/check", _check_body())
        assert status == 200 and answer["state"] == "danger" and answer["estopTransportError"] == "OSError: no bus"


def test_request_stop_needs_safe_stop_and_is_off_by_default() -> None:
    assert parse_droid_settings({"enabled": True}).request_stop is False
    assert parse_alarm_settings(None).request_stop is False
    with pytest.raises(DroidEstopConfigError):
        parse_droid_settings({"requestStop": True})
    with pytest.raises(ArmorAlarmConfigError):
        parse_alarm_settings({"requestStop": True})
    assert parse_droid_settings({"safeStop": True, "requestStop": True}).request_stop is True


def test_the_expiry_of_the_stateful_mode_reaches_get_state() -> None:
    clock = FakeClock()
    server = SafetyZonesServer(("127.0.0.1", 0), "test role", stateful=StatefulSettings(enabled=True), clock=clock)
    try:
        server.log_decision({"state": "ready", "reason": "ok", "sdkSafetyState": {"schema_version": "1.0", "state": "READY", "source": "t", "timestamp_utc": "2000-01-01T00:00:00+00:00"}})
        clock.now += 100.0
        server.watch_once()
        held = server.last_verdict()
        assert held is not None and held["sdkSafetyState"]["state"] != "READY"
    finally:
        server.server_close()
