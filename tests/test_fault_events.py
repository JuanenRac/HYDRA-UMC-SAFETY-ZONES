# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_fault_events.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The publisher of fault-event/1: the document, the rules of when it goes out, the settings, the MQTT side with a fake client and the running service."""
from __future__ import annotations

import json
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from hydra_umc_safety_zones.api import SafetyZonesServer
from hydra_umc_safety_zones.fault_events import (
    FaultEventEmitter,
    FaultEventSettings,
    FaultEventsConfigError,
    MqttFaultPublisher,
    build_fault_event,
    parse_fault_event_settings,
)

ON = FaultEventSettings(enabled=True, min_interval_s=0.0)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Sink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], bool]] = []

    def __call__(self, topic: str, payload: bytes, retain: bool) -> None:
        self.sent.append((topic, json.loads(payload), retain))


def verdict(state: str, reason: str = "because") -> dict[str, Any]:
    return {"sdkSafetyState": {"state": state}, "reason": reason}


def test_the_document_is_a_fault_event_the_server_accepts() -> None:
    # the same rules as parseFaultEvent of HYDRA-UMC-SERVER: schema, text source and code, state raised|cleared, severity in the known set
    event = build_fault_event(FaultEventSettings(robot_id="arm-1"), "SAFE_STOP", "x" * 500)
    assert event == {"schema": "fault-event/1", "source": "safety-zones", "code": "safety_state", "severity": "critical", "text": "x" * 240, "state": "raised", "robot_id": "arm-1"}
    assert build_fault_event(ON, "INHIBITED", "no observer")["severity"] == "warning"
    assert build_fault_event(ON, "FAULT", "")["severity"] == "error" and build_fault_event(ON, "FAULT", None)["text"] == "safety state FAULT"
    cleared = build_fault_event(ON, "READY", "all clear")
    assert cleared["state"] == "cleared" and "robot_id" not in cleared


def test_only_a_change_is_published_and_the_first_ready_says_nothing() -> None:
    sink = Sink()
    emitter = FaultEventEmitter(ON, sink, Clock())
    emitter.observe(verdict("READY"))
    assert sink.sent == []
    emitter.observe(verdict("INHIBITED"))
    emitter.observe(verdict("INHIBITED", "another reason"))
    emitter.observe(verdict("SAFE_STOP"))
    emitter.observe(verdict("READY"))
    emitter.observe(verdict("READY"))
    assert [(m["state"], m["severity"]) for _t, m, _r in sink.sent] == [("raised", "warning"), ("raised", "critical"), ("cleared", "info")]
    assert all(topic == "hydra/faults/safety-zones" and retain is True for topic, _m, retain in sink.sent)


def test_a_first_verdict_that_is_not_ready_is_published() -> None:
    sink = Sink()
    FaultEventEmitter(ON, sink, Clock()).observe(verdict("INHIBITED", "no heartbeat"))
    assert sink.sent[0][1]["text"] == "no heartbeat"


def test_a_state_that_flaps_is_held_back_and_the_newest_goes_out_later() -> None:
    clock, sink = Clock(), Sink()
    emitter = FaultEventEmitter(FaultEventSettings(enabled=True, min_interval_s=5.0), sink, clock)
    emitter.observe(verdict("INHIBITED"))
    clock.now += 1
    emitter.observe(verdict("READY"))  # too soon
    assert len(sink.sent) == 1
    clock.now += 5
    emitter.observe(verdict("READY"))  # the interval passed and the state still differs from the last one sent
    assert [m["state"] for _t, m, _r in sink.sent] == ["raised", "cleared"]


def test_off_and_broken_inputs_publish_nothing_and_never_raise() -> None:
    sink = Sink()
    FaultEventEmitter(FaultEventSettings(), sink, Clock()).observe(verdict("INHIBITED"))
    emitter = FaultEventEmitter(ON, sink, Clock())
    for bad in ({}, {"sdkSafetyState": None}, {"sdkSafetyState": {"state": 3}}, {"sdkSafetyState": "x"}):
        emitter.observe(bad)
    assert sink.sent == []


def test_a_failing_broker_is_counted_told_once_and_retried_with_the_next_verdict() -> None:
    told: list[str] = []
    calls = {"n": 0}

    def broken(topic: str, payload: bytes, retain: bool) -> None:
        calls["n"] += 1
        raise OSError("no route to the broker")

    emitter = FaultEventEmitter(ON, broken, Clock(), told.append)
    emitter.observe(verdict("INHIBITED"))
    emitter.observe(verdict("INHIBITED"))
    assert calls["n"] == 2 and emitter.failed == 2 and emitter.sent == 0 and len(told) == 1 and "OSError" in told[0] and "no route" not in told[0]


def test_settings_are_off_by_default_and_validated(tmp_path: Any) -> None:
    assert parse_fault_event_settings(None).enabled is False
    good = parse_fault_event_settings({"enabled": True, "topic": "hydra/faults/cell-1", "source": "cell_1", "robotId": "r1", "retain": False, "minIntervalS": 0,
                                       "broker": {"host": "broker.lan", "internal": True, "port": 8883, "tls": True}})
    assert good.enabled and good.topic == "hydra/faults/cell-1" and good.retain is False and good.broker.port == 8883 and good.broker.client_id == "hydra-umc-safety-zones-faults"
    for bad in ("x", {"surprise": 1}, {"enabled": "yes"}, {"topic": "other/topic"}, {"topic": "hydra/faults/#"}, {"topic": "hydra/faults//x"}, {"source": ""},
                {"code": "a b"}, {"robotId": "a/b"}, {"minIntervalS": -1}, {"minIntervalS": True}, {"broker": []}, {"broker": {"port": 0}}, {"broker": {"host": "broker.lan"}},
                {"broker": {"surprise": 1}}, {"broker": {"tls": "yes"}}):
        with pytest.raises(FaultEventsConfigError):
            parse_fault_event_settings(bad)


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.on_connect = self.on_disconnect = None

    def username_pw_set(self, user: str, password: str | None) -> None:
        self.calls.append(("auth", user, password))

    def reconnect_delay_set(self, **kwargs: Any) -> None:
        pass

    def connect_async(self, host: str, port: int, keepalive: int) -> None:
        self.calls.append(("connect", host, port))

    def loop_start(self) -> None:
        self.calls.append(("loop_start",))

    def loop_stop(self) -> None:
        self.calls.append(("loop_stop",))

    def disconnect(self) -> None:
        self.calls.append(("disconnect",))

    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        self.calls.append(("publish", topic, json.loads(payload), qos, retain))


def test_the_mqtt_publisher_reads_credentials_from_files_and_publishes_at_qos_1(tmp_path: Any) -> None:
    (tmp_path / "user").write_text("faults\n", encoding="utf-8")
    (tmp_path / "pass").write_text("hunter2\n", encoding="utf-8")
    settings = parse_fault_event_settings({"enabled": True, "broker": {"usernameFile": str(tmp_path / "user"), "passwordFile": str(tmp_path / "pass")}})
    client = FakeClient()
    publisher = MqttFaultPublisher(settings, client)
    with pytest.raises(RuntimeError):
        MqttFaultPublisher(settings).publish("t", b"{}", True)
    publisher.start()
    FaultEventEmitter(settings, publisher.publish, Clock()).observe(verdict("SAFE_STOP", "person in the danger zone"))
    publisher.stop()
    assert client.calls[0] == ("auth", "faults", "hunter2") and ("connect", "127.0.0.1", 1883) in client.calls
    published = [c for c in client.calls if c[0] == "publish"][0]
    assert published[1] == "hydra/faults/safety-zones" and published[2]["severity"] == "critical" and published[3:] == (1, True)
    assert client.calls[-2:] == [("loop_stop",), ("disconnect",)]


def test_an_unreadable_credential_file_stops_the_start(tmp_path: Any) -> None:
    settings = parse_fault_event_settings({"enabled": True, "broker": {"usernameFile": str(tmp_path / "missing")}})
    with pytest.raises(FaultEventsConfigError, match="usernameFile"):
        MqttFaultPublisher(settings, FakeClient()).start()


def _zones() -> dict:
    return {"zones": [{"id": "d", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 2, "y": 2, "z": 2}}],
            "calibration": {"version": "c", "source": "manual", "calibrated_at": datetime.now(timezone.utc).date().isoformat(), "max_age_days": 30}}


def _check(base: str, x: float, observed: bool) -> dict:
    body: dict[str, Any] = {"zones": _zones(), "detections": {"objects": [{"id": "p", "position": {"x": x, "y": 0, "z": 0}}]}}
    if observed:
        body["observation"] = {"active": True, "observedAt": datetime.now(timezone.utc).isoformat(), "maxAgeSeconds": 3600, "error": None}
    request = urllib.request.Request(base + "/check", data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - loopback test server
        return json.loads(response.read())


@contextmanager
def _running(emitter: FaultEventEmitter | None) -> Iterator[str]:
    server = SafetyZonesServer(("127.0.0.1", 0), "test")
    server.fault_emitter = emitter
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_running_service_publishes_the_changes_of_its_verdicts() -> None:
    sink = Sink()
    with _running(FaultEventEmitter(ON, sink, Clock())) as base:
        assert _check(base, 9.0, observed=False)["sdkSafetyState"]["state"] == "INHIBITED"  # no observer: not verified
        assert _check(base, 9.0, observed=True)["sdkSafetyState"]["state"] == "READY"
        assert _check(base, 1.0, observed=True)["sdkSafetyState"]["state"] == "SAFE_STOP"  # inside the danger zone
    assert [(m["state"], m["severity"]) for _t, m, _r in sink.sent] == [("raised", "warning"), ("cleared", "info"), ("raised", "critical")]


def test_a_publisher_that_breaks_never_changes_the_verdict() -> None:
    def broken(topic: str, payload: bytes, retain: bool) -> None:
        raise RuntimeError("boom")

    with_emitter, without = None, None
    with _running(FaultEventEmitter(ON, broken, Clock())) as base:
        with_emitter = _check(base, 1.0, observed=True)
    with _running(None) as base:
        without = _check(base, 1.0, observed=True)
    for key in ("state", "reason", "breaches"):
        assert with_emitter[key] == without[key]
    assert with_emitter["sdkSafetyState"]["state"] == without["sdkSafetyState"]["state"] == "SAFE_STOP"


def test_the_zones_file_switches_the_publisher_on_and_the_rest_of_the_file_still_loads(tmp_path: Any) -> None:
    from hydra_umc_safety_zones.config import load_zone_set
    from hydra_umc_safety_zones.main import _start_fault_events

    path = tmp_path / "zones.json"
    path.write_text(json.dumps({**_zones(), "faultEvents": {"enabled": True, "robotId": "arm-1"}}), encoding="utf-8")
    assert load_zone_set(str(path)) is not None
    started = _start_fault_events(str(path))
    assert started is not None and started[0].settings.robot_id == "arm-1" and started[1].client is None  # built, not yet started
    path.write_text(json.dumps(_zones()), encoding="utf-8")
    assert _start_fault_events(str(path)) is None and _start_fault_events("") is None
    path.write_text(json.dumps({**_zones(), "faultEvents": {"enabled": True, "topic": "elsewhere"}}), encoding="utf-8")
    with pytest.raises(FaultEventsConfigError):
        _start_fault_events(str(path))
