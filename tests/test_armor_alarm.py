# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_armor_alarm.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The reading of the ARMOR alarm: a sounding alarm inhibits new starts and is shown, never stops a machine that runs.

Everything is simulated: messages are built by hand, the clock is moved by hand, the broker is a fake. Nothing here was tried with a real alarm."""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from hydra_umc_safety_zones.api import SafetyZonesServer
from hydra_umc_safety_zones.armor_alarm import (
    DEFAULT_PHASE_TABLE,
    ArmorAlarmConfigError,
    ArmorAlarmGate,
    ArmorAlarmSettings,
    armor_alarm_to_sdk_state,
    combine,
    parse_settings,
)
from hydra_umc_safety_zones.armor_alarm_mqtt import ArmorAlarmSubscriber
from hydra_umc_safety_zones.can_estop import state_is_safe

TOPIC = "armor/alarm/alarm-1/state"


def msg(phase: str = "armed", ts: int = 1000, node: str = "alarm-1", **extra: object) -> bytes:
    body = {"kind": "alarm", "node_id": node, "timestamp_ms": ts, "phase": phase, "siren": phase == "alarm", **extra}
    return json.dumps(body).encode()


def gate(**kw: object) -> ArmorAlarmGate:
    return ArmorAlarmGate(ArmorAlarmSettings(enabled=True, **kw))  # type: ignore[arg-type]


# -- off by default ----------------------------------------------------------------------------------------------------------------


def test_off_by_default_and_ignores_everything() -> None:
    g = ArmorAlarmGate(ArmorAlarmSettings())
    g.ingest(TOPIC, msg("alarm"), 0.0)
    status = g.status(1.0)
    assert status.state == "disabled" and not status.inhibits
    assert parse_settings(None).enabled is False
    assert parse_settings({}).enabled is False


# -- conversion (the table is a parameter) -----------------------------------------------------------------------------------------


def test_conversion_matches_the_sdk_default_table() -> None:
    for phase, expected in DEFAULT_PHASE_TABLE.items():
        state = armor_alarm_to_sdk_state(json.loads(msg(phase)), DEFAULT_PHASE_TABLE, "alarm-1")
        assert state["state"] == expected and state["source"] == "armor:alarm-1" and state["armor_phase"] == phase
    assert armor_alarm_to_sdk_state(json.loads(msg("alarm", 1500)), DEFAULT_PHASE_TABLE)["timestamp_utc"] == "1970-01-01T00:00:01.500Z"


def test_conversion_agrees_with_the_sdk_when_it_is_next_door() -> None:
    sdk_src = Path(__file__).resolve().parents[2] / "HYDRA-UMC-SDK" / "clients" / "python" / "src"
    if not (sdk_src / "hydra_umc_sdk" / "armor_alignment.py").exists():
        pytest.skip("HYDRA-UMC-SDK is not next to this repository")
    sys.path.insert(0, str(sdk_src))
    try:
        from hydra_umc_sdk.armor_alignment import armor_alarm_to_safety_state  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        pytest.skip("the SDK client cannot be imported here")
    finally:
        sys.path.remove(str(sdk_src))
    for phase in DEFAULT_PHASE_TABLE:
        message = json.loads(msg(phase, 4242))
        assert armor_alarm_to_sdk_state(message, DEFAULT_PHASE_TABLE) == armor_alarm_to_safety_state(message)


@pytest.mark.parametrize(
    "message",
    [
        {"kind": "node_health", "node_id": "alarm-1", "timestamp_ms": 1, "phase": "armed"},
        {"kind": "alarm", "node_id": "Alarm 1", "timestamp_ms": 1, "phase": "armed"},
        {"kind": "alarm", "node_id": "alarm-1", "timestamp_ms": -1, "phase": "armed"},
        {"kind": "alarm", "node_id": "alarm-1", "timestamp_ms": True, "phase": "armed"},
        {"kind": "alarm", "node_id": "alarm-1", "timestamp_ms": 1, "phase": "burning"},
        {"kind": "alarm", "node_id": "alarm-1", "timestamp_ms": 1},
        {"kind": "alarm", "node_id": "other", "timestamp_ms": 1, "phase": "armed"},
        {"kind": "alarm", "node_id": "alarm-1", "timestamp_ms": 10**30, "phase": "armed"},
    ],
)
def test_malformed_messages_are_refused(message: dict) -> None:
    with pytest.raises(ValueError):
        armor_alarm_to_sdk_state(message, DEFAULT_PHASE_TABLE, "alarm-1")


# -- the state machine -------------------------------------------------------------------------------------------------------------


def test_nothing_heard_is_unknown_and_inhibits_and_is_shown() -> None:
    g = gate(nodes=("alarm-1",))
    status = g.status(0.0)
    assert status.state == "unknown" and status.inhibits and "never heard" in status.reason
    assert gate().status(0.0).state == "unknown"


def test_unknown_may_be_configured_to_only_show() -> None:
    g = gate(nodes=("alarm-1",), unknown_inhibits=False)
    status = g.status(0.0)
    assert status.state == "unknown" and not status.inhibits


def test_alarm_inhibits_and_lifts_only_after_a_confirmed_clear_and_the_hysteresis() -> None:
    g = gate(hysteresis_s=10.0)
    g.ingest(TOPIC, msg("alarm", 1000), 0.0)
    s = g.status(0.0)
    assert s.state == "alarm" and s.inhibits and not s.safe_stop
    g.ingest(TOPIC, msg("disarmed", 2000), 5.0)
    s = g.status(5.0)
    assert s.state == "holding" and s.inhibits
    g.ingest(TOPIC, msg("disarmed", 3000), 14.9)
    assert g.status(14.9).inhibits  # 9.9 s clear
    g.ingest(TOPIC, msg("disarmed", 4000), 15.0)
    s = g.status(15.0)
    assert s.state == "clear" and not s.inhibits


def test_a_new_alarm_during_the_hysteresis_restarts_the_wait() -> None:
    g = gate(hysteresis_s=10.0)
    g.ingest(TOPIC, msg("disarmed", 1000), 0.0)
    g.ingest(TOPIC, msg("alarm", 2000), 8.0)
    g.ingest(TOPIC, msg("disarmed", 3000), 9.0)
    assert g.status(18.9).inhibits
    assert not g.status(19.0).inhibits


def test_silence_is_unknown_not_clear() -> None:
    g = gate(hysteresis_s=0.0, max_age_s=30.0)
    g.ingest(TOPIC, msg("armed"), 0.0)
    assert not g.status(1.0).inhibits
    s = g.status(31.0)
    assert s.state == "unknown" and s.inhibits and "silent" in s.reason
    # when the panel talks again it was clear already, so it clears at once
    g.ingest(TOPIC, msg("armed", 2000), 40.0)
    assert not g.status(40.0).inhibits


def test_a_clock_that_goes_backwards_is_unknown_not_clear() -> None:
    g = gate(hysteresis_s=0.0)
    g.ingest(TOPIC, msg("armed"), 100.0)
    assert g.status(50.0).state == "unknown"


def test_the_start_needs_the_hysteresis_too() -> None:
    g = gate(hysteresis_s=10.0)
    g.ingest(TOPIC, msg("armed"), 0.0)
    assert g.status(1.0).state == "holding"
    assert g.status(10.0).state == "clear"


@pytest.mark.parametrize(
    "payload",
    [b"{not json", b"", b"[]", b'{"kind":"alarm"}', b'{"kind":"alarm","node_id":"alarm-1","timestamp_ms":NaN,"phase":"armed"}', b"\xff\xfe", b"x" * 70000],
    ids=["not-json", "empty", "list", "no-fields", "nan", "bad-utf8", "too-big"],
)
def test_corrupt_data_inhibits(payload: bytes) -> None:
    g = gate(hysteresis_s=0.0)
    g.ingest(TOPIC, msg("armed", 1000), 0.0)
    assert not g.status(0.0).inhibits
    g.ingest(TOPIC, payload, 1.0)
    s = g.status(1.0)
    assert s.state == "alarm" and s.inhibits and "corrupt" in s.reason


def test_corrupt_inhibition_is_lifted_only_by_good_messages_and_the_hysteresis() -> None:
    g = gate(hysteresis_s=5.0)
    g.ingest(TOPIC, msg("armed", 1000), 0.0)
    g.ingest(TOPIC, b"garbage", 10.0)
    assert g.status(10.0).inhibits
    g.ingest(TOPIC, msg("armed", 2000), 11.0)
    assert g.status(15.9).inhibits
    assert not g.status(16.0).inhibits


def test_contradictions_inhibit() -> None:
    g = gate(hysteresis_s=0.0)
    g.ingest(TOPIC, msg("armed", 1000), 0.0)
    # the topic says alarm-1, the body says another panel
    g.ingest(TOPIC, msg("disarmed", 2000, node="alarm-2"), 1.0)
    assert g.status(1.0).inhibits
    g2 = gate(hysteresis_s=0.0)
    g2.ingest(TOPIC, msg("armed", 1000, siren=True), 0.0)  # a siren that sounds in a clear phase
    assert g2.status(0.0).inhibits
    g3 = gate(hysteresis_s=0.0)
    g3.ingest(TOPIC, msg("alarm", 5000), 0.0)
    g3.ingest(TOPIC, msg("disarmed", 4000), 1.0)  # an old "no alarm" replayed
    s = g3.status(1.0)
    assert s.inhibits and "backwards" in s.reason


def test_a_replayed_clear_cannot_lift_an_alarm() -> None:
    g = gate(hysteresis_s=0.0)
    g.ingest(TOPIC, msg("armed", 1000), 0.0)
    g.ingest(TOPIC, msg("alarm", 3000), 1.0)
    g.ingest(TOPIC, msg("armed", 1000), 2.0)  # the old message again
    assert g.status(100.0).state != "clear"


def test_retained_messages_are_never_applied() -> None:
    g = gate(hysteresis_s=0.0)
    g.ingest(TOPIC, msg("armed"), 0.0, retained=True)
    assert g.status(0.0).state == "unknown"


def test_nodes_allow_list_and_topic_filter() -> None:
    g = gate(nodes=("alarm-1",), hysteresis_s=0.0)
    g.ingest("armor/alarm/intruder/state", msg("armed", node="intruder"), 0.0)
    g.ingest("armor/alarm/alarm-1/command", msg("armed"), 0.0)
    g.ingest("armor/node/alarm-1/state", msg("armed"), 0.0)
    assert g.status(0.0).state == "unknown"
    g.ingest(TOPIC, msg("armed"), 0.0)
    assert g.status(0.0).state == "clear"
    only = gate(topic="armor/alarm/alarm-1/state", hysteresis_s=0.0)
    only.ingest("armor/alarm/alarm-2/state", msg("alarm", node="alarm-2"), 0.0)
    assert only.status(0.0).state == "unknown"


def test_several_panels_the_worst_wins() -> None:
    g = gate(nodes=("alarm-1", "alarm-2"), hysteresis_s=0.0)
    g.ingest(TOPIC, msg("armed"), 0.0)
    assert g.status(0.0).state == "unknown"  # the second one was never heard
    g.ingest("armor/alarm/alarm-2/state", msg("alarm", node="alarm-2"), 0.0)
    s = g.status(0.0)
    assert s.state == "alarm" and "alarm-2" in s.reason


# -- SAFE_STOP only with the switch ------------------------------------------------------------------------------------------------


def test_safe_stop_in_the_table_is_read_as_inhibited_while_the_switch_is_off() -> None:
    table = dict(DEFAULT_PHASE_TABLE, alarm="SAFE_STOP")
    g = gate(phase_table=table)
    g.ingest(TOPIC, msg("alarm"), 0.0)
    s = g.status(0.0)
    assert s.inhibits and not s.safe_stop
    assert any(r["event"] == "downgraded" for r in g.audit_records)
    assert combine("ready", "r", s) == ("inhibited", s.reason)


def test_safe_stop_only_with_the_switch_and_the_table() -> None:
    table = dict(DEFAULT_PHASE_TABLE, alarm="SAFE_STOP")
    g = gate(phase_table=table, safe_stop=True)
    g.ingest(TOPIC, msg("alarm"), 0.0)
    s = g.status(0.0)
    assert s.safe_stop
    assert combine("ready", "r", s)[0] == "danger"
    assert combine("inhibited", "r", s)[0] == "danger"
    # the switch on with the default table (INHIBITED) still stops nothing
    d = gate(safe_stop=True)
    d.ingest(TOPIC, msg("alarm"), 0.0)
    assert not d.status(0.0).safe_stop


def test_combine_never_lowers_a_verdict() -> None:
    clear = gate(hysteresis_s=0.0)
    clear.ingest(TOPIC, msg("armed"), 0.0)
    s = clear.status(0.0)
    for state in ("ready", "warning", "danger", "inhibited"):
        assert combine(state, "x", s) == (state, "x")
    alarm = gate()
    alarm.ingest(TOPIC, msg("alarm"), 0.0)
    a = alarm.status(0.0)
    assert combine("danger", "x", a) == ("danger", "x")
    assert combine("warning", "x", a)[0] == "warning"
    assert combine("inhibited", "x", a) == ("inhibited", "x")


# -- audit -----------------------------------------------------------------------------------------------------------------------


def test_audit_records_the_changes() -> None:
    lines: list[dict] = []
    g = ArmorAlarmGate(ArmorAlarmSettings(enabled=True, hysteresis_s=0.0), lines.append)
    g.ingest(TOPIC, msg("armed", 1000), 0.0)
    g.status(0.0)
    g.ingest(TOPIC, msg("alarm", 2000), 1.0)
    g.status(1.0)
    g.ingest(TOPIC, b"junk", 2.0)
    events = [(r["event"], r["source"]) for r in lines]
    assert ("status", "armor-alarm") in events and ("corrupt", "armor-alarm") in events
    assert sum(1 for r in lines if r["event"] == "status") == 2  # clear, then alarm; not repeated
    assert all(set(r) >= {"at", "event", "detail"} for r in lines)


def test_a_failing_audit_sink_changes_nothing() -> None:
    def boom(_: dict) -> None:
        raise OSError("disk full")

    g = ArmorAlarmGate(ArmorAlarmSettings(enabled=True), boom)
    g.ingest(TOPIC, msg("alarm"), 0.0)
    assert g.status(0.0).inhibits


# -- settings ----------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"enabeld": True},
        {"enabled": "yes"},
        {"topic": "armor/#"},
        {"topic": "armor/alarm/+/command"},
        {"nodes": ["Bad Id"]},
        {"phaseTable": {}},
        {"phaseTable": {"alarm": "STOP"}},
        {"maxAgeS": 0},
        {"maxAgeS": float("nan")},
        {"hysteresisS": -1},
        {"hysteresisS": True},
        {"broker": {"host": "10.0.0.5"}},  # not on this machine and not declared internal
        {"broker": {"port": 0}},
        {"broker": {"hostt": "x"}},
    ],
)
def test_bad_settings_are_refused(raw: object) -> None:
    with pytest.raises(ArmorAlarmConfigError):
        parse_settings(raw)


def test_good_settings() -> None:
    s = parse_settings(
        {
            "enabled": True, "nodes": ["alarm-1"], "maxAgeS": 20, "hysteresisS": 30, "unknownInhibits": False, "safeStop": True,
            "phaseTable": {"alarm": "SAFE_STOP", "armed": "READY"},
            "broker": {"host": "10.0.0.5", "internal": True, "port": 8883, "tls": True},
        }
    )
    assert s.enabled and s.nodes == ("alarm-1",) and s.max_age_s == 20 and s.hysteresis_s == 30 and not s.unknown_inhibits and s.safe_stop
    assert s.broker.host == "10.0.0.5" and s.broker.tls


# -- the transport with a fake broker ----------------------------------------------------------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.credentials: tuple[str, str | None] | None = None
        self.started = False

    def username_pw_set(self, user: str, password: str | None) -> None:
        self.credentials = (user, password)

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


def test_subscriber_subscribes_and_feeds_the_gate(tmp_path: Path) -> None:
    user, password = tmp_path / "u", tmp_path / "p"
    user.write_text("safety\n", encoding="utf-8")
    password.write_text("hunter2\n", encoding="utf-8")
    settings = parse_settings({"enabled": True, "hysteresisS": 0, "broker": {"usernameFile": str(user), "passwordFile": str(password)}})
    g = ArmorAlarmGate(settings)
    now = [0.0]
    fake = FakeClient()
    sub = ArmorAlarmSubscriber(g, clock=lambda: now[0], client=fake)
    sub.start()
    assert fake.started and fake.credentials == ("safety", "hunter2") and fake.host == ("127.0.0.1", 1883)
    sub._on_connect(fake, None, {}, 0)  # noqa: SLF001
    assert fake.subscribed == ["armor/alarm/+/state"] and sub.connected
    sub._on_message(fake, None, SimpleNamespace(topic=TOPIC, payload=msg("alarm"), retain=False))  # noqa: SLF001
    assert g.status(0.0).state == "alarm"
    sub._on_message(fake, None, SimpleNamespace(topic=TOPIC, payload=msg("disarmed", 2000), retain=True))  # noqa: SLF001
    assert g.status(0.0).state == "alarm"  # retained: ignored
    sub._on_disconnect(fake, None)  # noqa: SLF001
    assert not sub.connected
    sub.stop()
    assert not fake.started


def test_a_missing_secret_file_stops_the_start(tmp_path: Path) -> None:
    settings = parse_settings({"enabled": True, "broker": {"usernameFile": str(tmp_path / "none")}})
    with pytest.raises(ArmorAlarmConfigError):
        ArmorAlarmSubscriber(ArmorAlarmGate(settings), client=FakeClient()).start()


def test_a_failed_connection_code_is_not_connected() -> None:
    sub = ArmorAlarmSubscriber(gate(), client=FakeClient())
    sub._on_connect(sub.client, None, {}, 5)  # noqa: SLF001
    assert not sub.connected


# -- through the service -----------------------------------------------------------------------------------------------------------

TOKEN = "tok"


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
def _server(g: ArmorAlarmGate | None, clock: FakeClock):  # noqa: ANN202
    server = SafetyZonesServer(("127.0.0.1", 0), "test", token=TOKEN, clock=clock, armor_gate=g)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _call(base: str, method: str, path: str, body: object = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    request = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def test_service_without_the_gate_is_unchanged() -> None:
    with _server(None, FakeClock()) as (base, server):
        status, answer = _call(base, "POST", "/check", _check_body())
        assert status == 200 and answer["state"] == "ready" and "armorAlarm" not in answer
        assert _call(base, "GET", "/armor-alarm")[0] == 404


def test_service_alarm_inhibits_then_lifts_with_the_hysteresis() -> None:
    clock = FakeClock()
    g = ArmorAlarmGate(ArmorAlarmSettings(enabled=True, hysteresis_s=10.0))
    with _server(g, clock) as (base, server):
        g.ingest(TOPIC, msg("armed", 1000), clock())
        clock.now += 10
        assert _call(base, "POST", "/check", _check_body())[1]["state"] == "ready"
        g.ingest(TOPIC, msg("alarm", 2000), clock())
        status, answer = _call(base, "POST", "/check", _check_body())
        assert status == 200 and answer["state"] == "inhibited" and answer["sdkSafetyState"]["state"] == "INHIBITED"
        assert answer["armorAlarm"]["state"] == "alarm" and answer["armorInhibitOnly"] is True and answer["estopRequests"] == []
        g.ingest(TOPIC, msg("disarmed", 3000), clock())
        assert _call(base, "POST", "/check", _check_body())[1]["state"] == "inhibited"
        clock.now += 10
        g.ingest(TOPIC, msg("disarmed", 4000), clock())
        assert _call(base, "POST", "/check", _check_body())[1]["state"] == "ready"
        status, shown = _call(base, "GET", "/armor-alarm")
        assert status == 200 and shown["state"] == "clear" and shown["audit"]


def test_service_silent_broker_is_unknown_and_shown() -> None:
    clock = FakeClock()
    g = ArmorAlarmGate(ArmorAlarmSettings(enabled=True, hysteresis_s=0.0, max_age_s=30.0))
    with _server(g, clock) as (base, _):
        g.ingest(TOPIC, msg("armed"), clock())
        assert _call(base, "POST", "/check", _check_body())[1]["state"] == "ready"
        clock.now += 31
        answer = _call(base, "POST", "/check", _check_body())[1]
        assert answer["state"] == "inhibited" and answer["armorAlarm"]["state"] == "unknown"


def test_service_a_danger_breach_stays_a_danger_under_an_alarm() -> None:
    clock = FakeClock()
    g = ArmorAlarmGate(ArmorAlarmSettings(enabled=True))
    with _server(g, clock) as (base, _):
        g.ingest(TOPIC, msg("alarm"), clock())
        answer = _call(base, "POST", "/check", _check_body(x=1.0))[1]
        assert answer["state"] == "danger" and answer["breaches"] and "armorInhibitOnly" not in answer


def test_service_safe_stop_only_when_switched_on() -> None:
    clock = FakeClock()
    table = dict(DEFAULT_PHASE_TABLE, alarm="SAFE_STOP")
    off = ArmorAlarmGate(ArmorAlarmSettings(enabled=True, phase_table=table))
    with _server(off, clock) as (base, _):
        off.ingest(TOPIC, msg("alarm"), clock())
        assert _call(base, "POST", "/check", _check_body())[1]["sdkSafetyState"]["state"] == "INHIBITED"
    on = ArmorAlarmGate(ArmorAlarmSettings(enabled=True, phase_table=table, safe_stop=True))
    with _server(on, clock) as (base, _):
        on.ingest(TOPIC, msg("alarm"), clock())
        answer = _call(base, "POST", "/check", _check_body())[1]
        assert answer["sdkSafetyState"]["state"] == "SAFE_STOP" and "armorInhibitOnly" not in answer


def test_the_house_alarm_does_not_silence_the_safe_heartbeat_but_other_inhibitions_do() -> None:
    clock = FakeClock()
    g = ArmorAlarmGate(ArmorAlarmSettings(enabled=True))
    with _server(g, clock) as (base, server):
        g.ingest(TOPIC, msg("alarm"), clock())
        _call(base, "POST", "/check", _check_body())
        assert state_is_safe(server.last_verdict(), 1000)  # inhibited by the house alarm only: a running machine goes on
        body = _check_body()
        body["observation"]["active"] = False  # the observer is down: a real inhibition
        _call(base, "POST", "/check", body)
        assert not state_is_safe(server.last_verdict(), 1000)
    # a verdict that merely claims the flag without being INHIBITED is not safe
    assert not state_is_safe({"sdkSafetyState": {"state": "SAFE_STOP", "timestamp_utc": datetime.now(timezone.utc).isoformat()}, "armorInhibitOnly": True}, 1000)
