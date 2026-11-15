# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_droid_estop.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The reading of the droid emergency stop: a cut droid inhibits new starts and is shown, never stops a machine that runs.

Everything is simulated: the messages of the droid bridge are built by hand, the clock is moved by hand, the broker is a fake. Nothing here was tried with a real droid."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from hydra_umc_safety_zones import main as safety_main
from hydra_umc_safety_zones.api import SafetyZonesServer
from hydra_umc_safety_zones.armor_alarm_mqtt import ArmorAlarmSubscriber
from hydra_umc_safety_zones.can_estop import state_is_safe
from hydra_umc_safety_zones.droid_estop import DroidEstopConfigError, DroidEstopGate, DroidEstopSettings, parse_settings

STATE = "hydra/bridges/droids/state"
AVAILABILITY = "hydra/bridges/droids/availability"
TOKEN = "tok"


def state(ready: bool = True, cut: str = "", link_lost: bool = False, **extra: object) -> bytes:
    return json.dumps({"session_ready": ready, "cut_reason": cut, "link_lost": link_lost, "machine_state": None, "global_stop": "OFF", "observed_at": 1, **extra}).encode()


def gate(**kw: object) -> DroidEstopGate:
    return DroidEstopGate(DroidEstopSettings(enabled=True, hysteresis_s=0.0, **kw))  # type: ignore[arg-type]


# -- the settings ------------------------------------------------------------------------------------------------------------------

def test_it_is_off_by_default_and_a_disabled_gate_reads_nothing() -> None:
    assert parse_settings(None).enabled is False
    g = DroidEstopGate(DroidEstopSettings())
    g.ingest(STATE, state(cut="cut"), 0.0)
    status = g.status(0.0)
    assert status.state == "disabled" and not status.inhibits and g.audit_records == []


@pytest.mark.parametrize(
    "data",
    [
        [], {"nope": 1}, {"enabled": "yes"}, {"safeStop": 1}, {"topic": "armor/alarm/+/state"}, {"topic": "hydra/bridges/+/state"}, {"topic": "hydra/bridges/droids/cmd"},
        {"maxAgeS": -1}, {"maxAgeS": True}, {"hysteresisS": 99999}, {"auditLog": 3}, {"broker": []}, {"broker": {"host": "10.0.0.5"}},
    ],
)
def test_malformed_settings_are_refused(data: object) -> None:
    with pytest.raises(DroidEstopConfigError):
        parse_settings(data)


def test_settings_are_read_with_conservative_defaults() -> None:
    s = parse_settings({"enabled": True})
    assert (s.topic, s.max_age_s, s.hysteresis_s, s.unknown_inhibits, s.safe_stop) == (STATE, 0.0, 10.0, True, False)
    assert s.broker.client_id == "hydra-umc-safety-zones-droids" and s.broker.is_local()
    other = parse_settings({"enabled": True, "topic": "hydra/bridges/spot2/state", "maxAgeS": 30, "broker": {"host": "10.0.0.5", "internal": True}})
    assert other.topic == "hydra/bridges/spot2/state" and other.broker.host == "10.0.0.5" and other.broker.client_id == "hydra-umc-safety-zones-droids"
    assert "droidEstop.broker" in str(pytest.raises(DroidEstopConfigError, parse_settings, {"broker": {"host": "10.0.0.5"}}).value)


# -- the gate ----------------------------------------------------------------------------------------------------------------------

def test_nothing_heard_is_unknown_and_inhibits_unless_the_installation_says_otherwise() -> None:
    assert gate().status(0.0).state == "unknown" and gate().status(0.0).inhibits
    assert not gate(unknown_inhibits=False).status(0.0).inhibits


def test_a_cut_inhibits_and_is_lifted_only_by_a_clear_message_after_the_hysteresis() -> None:
    g = DroidEstopGate(DroidEstopSettings(enabled=True, hysteresis_s=10.0))
    g.ingest(STATE, state(), 0.0)
    assert g.status(0.0).state == "holding" and g.status(0.0).inhibits  # the very first clear also waits
    assert g.status(10.0).state == "clear" and not g.status(10.0).inhibits
    g.ingest(STATE, state(ready=False, cut="cut by an operator request over MQTT"), 20.0)
    cut = g.status(20.0)
    assert cut.state == "cut" and cut.inhibits and not cut.safe_stop and "operator" in cut.reason
    assert g.status(1000.0).state == "cut"  # silence does not lift it
    g.ingest(STATE, state(), 30.0)
    assert g.status(30.0).state == "holding"
    assert g.status(39.0).inhibits and not g.status(40.0).inhibits
    g.ingest(STATE, state(ready=False, cut="again"), 41.0)
    g.ingest(STATE, state(), 42.0)
    assert g.status(42.5).state == "holding"  # a new cut restarted the wait


def test_a_lost_link_inhibits_like_a_cut() -> None:
    g = gate()
    g.ingest(STATE, state(link_lost=True), 0.0)
    assert g.status(0.0).state == "cut" and "link" in g.status(0.0).reason
    g.ingest(STATE, state(), 1.0)
    assert g.status(1.0).state == "clear"


def test_a_session_that_never_started_is_not_a_cut() -> None:
    g = gate()
    g.ingest(STATE, state(ready=False), 0.0)
    assert g.status(0.0).state == "clear"


def test_a_retained_message_is_applied_in_both_directions() -> None:
    g = gate()
    g.ingest(STATE, state(cut="cut"), 0.0, retained=True)
    assert g.status(0.0).state == "cut"
    g.ingest(STATE, state(), 1.0, retained=True)
    assert g.status(1.0).state == "clear"


def test_the_safe_stop_only_when_switched_on() -> None:
    g = gate(safe_stop=True)
    g.ingest(STATE, state(cut="cut"), 0.0)
    assert g.status(0.0).safe_stop is True
    h = gate()
    h.ingest(STATE, state(cut="cut"), 0.0)
    assert h.status(0.0).safe_stop is False


@pytest.mark.parametrize(
    "payload",
    [None, b"", b"x" * 20000, b"{not json", b"[]", b'{"session_ready": 1, "cut_reason": ""}', b'{"session_ready": true}', b'{"session_ready": true, "cut_reason": 3}',
     b'{"session_ready": true, "cut_reason": "", "link_lost": "no"}', b'{"session_ready": NaN, "cut_reason": ""}', "é".encode("latin-1")],
)
def test_a_corrupt_message_inhibits_until_a_good_one_comes_back(payload: bytes | None) -> None:
    g = gate()
    g.ingest(STATE, state(), 0.0)
    assert g.status(0.0).state == "clear"
    g.ingest(STATE, payload, 1.0)
    assert g.status(1.0).state == "cut" and g.status(1.0).inhibits and any(r["event"] == "corrupt" for r in g.audit_records)
    g.ingest(STATE, state(), 2.0)
    assert g.status(2.0).state == "clear"


def test_a_string_payload_and_a_long_reason_are_read() -> None:
    g = gate()
    g.ingest(STATE, state(cut="x" * 500).decode(), 0.0)
    assert len(g.status(0.0).detail) == 200


def test_an_offline_bridge_is_unknown_and_an_online_one_goes_on() -> None:
    g = gate()
    g.ingest(STATE, state(), 0.0)
    g.ingest(AVAILABILITY, b"offline", 1.0)
    assert g.status(1.0).state == "unknown" and g.status(1.0).inhibits
    g.ingest(AVAILABILITY, "online", 2.0)
    assert g.status(2.0).state == "holding" or g.status(2.0).state == "clear"
    g.ingest(AVAILABILITY, b"maybe", 3.0)
    assert g.status(3.0).state == "cut" and "neither" in g.status(3.0).reason
    g.ingest(STATE, state(), 4.0)
    assert g.status(4.0).state == "clear"
    assert not gate(unknown_inhibits=False).status(0.0).inhibits
    h = gate(unknown_inhibits=False)
    h.ingest(AVAILABILITY, b"offline", 0.0)
    assert h.status(0.0).state == "unknown" and not h.status(0.0).inhibits


def test_silence_counts_only_when_a_maximum_age_is_set() -> None:
    quiet = gate()
    quiet.ingest(STATE, state(), 0.0)
    assert quiet.status(100000.0).state == "clear"
    g = gate(max_age_s=30.0)
    g.ingest(STATE, state(), 0.0)
    assert g.status(10.0).state == "clear"
    assert g.status(31.0).state == "unknown" and g.status(31.0).inhibits
    assert g.status(-5.0).state == "unknown"  # a clock that went back


def test_a_foreign_topic_is_ignored_and_the_topics_are_the_state_and_the_availability() -> None:
    g = gate()
    g.ingest("hydra/bridges/uav/state", state(cut="cut"), 0.0)
    assert g.status(0.0).state == "unknown" and g.audit_records[0]["event"] == "ignored"
    assert g.topics() == (STATE, AVAILABILITY)


def test_the_audit_sink_gets_every_line_and_a_failing_sink_changes_nothing() -> None:
    lines: list[dict] = []
    g = DroidEstopGate(DroidEstopSettings(enabled=True, hysteresis_s=0.0), lines.append)
    g.ingest(STATE, state(cut="cut"), 0.0)
    g.status(0.0)
    assert {r["event"] for r in lines} >= {"cut", "status"} and all(r["source"] == "droid-estop" for r in lines)

    def boom(_: dict) -> None:
        raise OSError("disk full")

    h = DroidEstopGate(DroidEstopSettings(enabled=True), boom)
    h.ingest(STATE, state(cut="cut"), 0.0)
    assert h.status(0.0).state == "cut"


# -- the subscriber is the one of the alarm ----------------------------------------------------------------------------------------

class FakeClient:
    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.started = False

    def reconnect_delay_set(self, **_: object) -> None:
        pass

    def connect(self, host: str, port: int, keepalive: int) -> None:
        self.host = (host, port)

    def loop_start(self) -> None:
        self.started = True

    def loop_stop(self) -> None:
        self.started = False

    def disconnect(self) -> None:
        pass

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append(topic)


def test_the_subscriber_listens_to_the_state_and_the_availability_and_feeds_the_gate() -> None:
    g = gate()
    now = [0.0]
    fake = FakeClient()
    sub = ArmorAlarmSubscriber(g, clock=lambda: now[0], client=fake)
    sub.start()
    sub._on_connect(fake, None, {}, 0)  # noqa: SLF001
    assert fake.subscribed == [STATE, AVAILABILITY]
    sub._on_message(fake, None, SimpleNamespace(topic=STATE, payload=state(cut="cut"), retain=True))  # noqa: SLF001
    assert g.status(0.0).state == "cut"
    sub.stop()


# -- through the service -----------------------------------------------------------------------------------------------------------

class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _zones() -> dict:
    return {
        "zones": [{"id": "d", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 2, "y": 2, "z": 2}}],
        "calibration": {"version": "c", "source": "manual", "calibrated_at": datetime.now(timezone.utc).date().isoformat(), "max_age_days": 30},
    }


def _check_body(x: float = 9.0) -> dict:
    obs = {"active": True, "observedAt": datetime.now(timezone.utc).isoformat(), "maxAgeSeconds": 3600, "error": None}
    return {"zones": _zones(), "detections": {"objects": [{"id": "p", "position": {"x": x, "y": 0, "z": 0}}]}, "observation": obs}


@contextmanager
def _server(g: DroidEstopGate | None, clock: FakeClock):  # noqa: ANN202
    server = SafetyZonesServer(("127.0.0.1", 0), "test", token=TOKEN, clock=clock, droid_gate=g)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _call(base: str, method: str, path: str, body: object = None, token: str | None = TOKEN) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def test_the_service_without_the_gate_is_unchanged() -> None:
    with _server(None, FakeClock()) as (base, _):
        status, answer = _call(base, "POST", "/check", _check_body())
        assert status == 200 and answer["state"] == "ready" and "droidEstop" not in answer
        assert _call(base, "GET", "/droid-estop")[0] == 404


def test_a_cut_droid_inhibits_a_ready_verdict_and_lifts_after_the_hysteresis() -> None:
    clock = FakeClock()
    g = DroidEstopGate(DroidEstopSettings(enabled=True, hysteresis_s=10.0))
    with _server(g, clock) as (base, server):
        g.ingest(STATE, state(), clock())
        clock.now += 10
        assert _call(base, "POST", "/check", _check_body())[1]["state"] == "ready"
        g.ingest(STATE, state(ready=False, cut="cut"), clock())
        status, answer = _call(base, "POST", "/check", _check_body())
        assert status == 200 and answer["state"] == "inhibited" and answer["sdkSafetyState"]["state"] == "INHIBITED"
        assert answer["droidEstop"]["state"] == "cut" and answer["droidInhibitOnly"] is True and answer["estopRequests"] == []
        assert state_is_safe(server.last_verdict(), 1000)  # inhibited by the droid only: a running machine goes on
        g.ingest(STATE, state(), clock())
        assert _call(base, "POST", "/check", _check_body())[1]["state"] == "inhibited"
        clock.now += 10
        assert _call(base, "POST", "/check", _check_body())[1]["state"] == "ready"
        status, shown = _call(base, "GET", "/droid-estop")
        assert status == 200 and shown["state"] == "clear" and shown["audit"]
        assert _call(base, "GET", "/droid-estop", token=None)[0] in (401, 403)


def test_a_danger_breach_stays_a_danger_under_a_cut_droid() -> None:
    clock = FakeClock()
    g = gate()
    with _server(g, clock) as (base, _):
        g.ingest(STATE, state(cut="cut"), clock())
        answer = _call(base, "POST", "/check", _check_body(x=1.0))[1]
        assert answer["state"] == "danger" and answer["breaches"] and "droidInhibitOnly" not in answer


def test_an_observer_that_is_down_still_silences_the_heartbeat_under_a_cut_droid() -> None:
    clock = FakeClock()
    g = gate()
    with _server(g, clock) as (base, server):
        g.ingest(STATE, state(cut="cut"), clock())
        body = _check_body()
        body["observation"]["active"] = False
        _call(base, "POST", "/check", body)
        assert not state_is_safe(server.last_verdict(), 1000)


def test_the_safe_stop_switch_makes_a_cut_droid_a_stop_for_the_cell() -> None:
    clock = FakeClock()
    g = gate(safe_stop=True)
    with _server(g, clock) as (base, _):
        g.ingest(STATE, state(cut="cut"), clock())
        answer = _call(base, "POST", "/check", _check_body())[1]
        assert answer["state"] == "danger" and answer["sdkSafetyState"]["state"] == "SAFE_STOP" and "droidInhibitOnly" not in answer


# -- the start-up --------------------------------------------------------------------------------------------------------------------

def test_the_start_helper_is_off_unless_the_zones_file_switches_it_on(tmp_path: Path) -> None:
    assert safety_main._start_droid_estop("") is None  # noqa: SLF001
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps(_zones()), encoding="utf-8")
    assert safety_main._start_droid_estop(str(plain)) is None  # noqa: SLF001
    off = tmp_path / "off.json"
    off.write_text(json.dumps({**_zones(), "droidEstop": {"enabled": False}}), encoding="utf-8")
    assert safety_main._start_droid_estop(str(off)) is None  # noqa: SLF001
    on = tmp_path / "on.json"
    on.write_text(json.dumps({**_zones(), "droidEstop": {"enabled": True}}), encoding="utf-8")
    runtime = safety_main._start_droid_estop(str(on))  # noqa: SLF001
    assert runtime is not None and runtime[0].settings.enabled and isinstance(runtime[1], ArmorAlarmSubscriber)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**_zones(), "droidEstop": {"enabled": True, "topic": "x"}}), encoding="utf-8")
    with pytest.raises(DroidEstopConfigError):
        safety_main._start_droid_estop(str(bad))  # noqa: SLF001
