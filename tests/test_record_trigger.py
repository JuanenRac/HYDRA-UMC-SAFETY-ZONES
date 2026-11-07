# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_record_trigger.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The client that asks the camera streamers to keep their frames at a safe stop: the rules of when, the settings, and the running service against a stand-in streamer.

The stand-in streamer answers the way the streamer documents its trigger route (bearer token, a body with a reason and a source, 202 when accepted, 429 inside its minimum
gap, 400 for a bad body). Nothing here has run against the real streamer with a real camera.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from hydra_umc_safety_zones.api import SafetyZonesServer
from hydra_umc_safety_zones.record_trigger import (
    RecordTriggerClient,
    RecordTriggerConfigError,
    RecordTriggerSettings,
    build_client,
    http_sender,
    parse_record_trigger_settings,
    trigger_reason,
)

TARGET = "http://127.0.0.1:9/cam0/record/trigger"
ON = RecordTriggerSettings(enabled=True, targets=(TARGET,), min_interval_s=0.0)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Sink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], str]] = []

    def __call__(self, url: str, body: bytes, token: str) -> None:
        self.sent.append((url, json.loads(body), token))


def verdict(state: str, reason: str = "person in the danger zone") -> dict[str, Any]:
    return {"sdkSafetyState": {"state": state}, "reason": reason}


def client(settings: RecordTriggerSettings = ON, sink: Any = None, clock: Any = None, token: str = "") -> RecordTriggerClient:
    return RecordTriggerClient(settings, sink or Sink(), clock or Clock(), token=token, background=False)


def test_a_safe_stop_asks_every_camera_once_per_stop() -> None:
    sink, clock = Sink(), Clock()
    triggers = client(RecordTriggerSettings(enabled=True, targets=(TARGET, "http://127.0.0.1:9/cam1/record/trigger"), min_interval_s=0.0), sink, clock, token="t0k")
    triggers.observe(verdict("READY"))
    triggers.observe(verdict("SAFE_STOP"))
    triggers.observe(verdict("SAFE_STOP"))  # the same stop is not asked twice
    assert [(url, body["source"], token) for url, body, token in sink.sent] == [(TARGET, "safety-zones", "t0k"), ("http://127.0.0.1:9/cam1/record/trigger", "safety-zones", "t0k")]
    assert sink.sent[0][1]["reason"] == "safe stop: person in the danger zone"
    triggers.observe(verdict("INHIBITED"))  # leaving the stop re-arms it
    triggers.observe(verdict("SAFE_STOP"))
    assert len(sink.sent) == 4


def test_only_a_safe_stop_triggers_and_the_minimum_gap_is_respected() -> None:
    sink, clock = Sink(), Clock()
    triggers = client(RecordTriggerSettings(enabled=True, targets=(TARGET,), min_interval_s=5.0), sink, clock)
    for state in ("READY", "INHIBITED", "FAULT"):
        triggers.observe(verdict(state))
    assert sink.sent == []
    triggers.observe(verdict("SAFE_STOP"))
    triggers.observe(verdict("READY"))
    clock.now += 1.0
    triggers.observe(verdict("SAFE_STOP"))  # a second stop one second later is inside the gap
    assert len(sink.sent) == 1
    triggers.observe(verdict("READY"))
    clock.now += 5.0
    triggers.observe(verdict("SAFE_STOP"))
    assert len(sink.sent) == 2


def test_off_by_default_and_a_verdict_without_a_state_do_nothing() -> None:
    sink = Sink()
    client(RecordTriggerSettings(targets=(TARGET,)), sink).observe(verdict("SAFE_STOP"))
    client(ON, sink).observe({"reason": "x"})
    client(ON, sink).observe({"sdkSafetyState": "SAFE_STOP"})
    assert sink.sent == []


def test_the_reason_is_made_printable_ascii_and_cut_to_the_limit_of_the_streamer() -> None:
    assert trigger_reason("persona en la zona ñ") == "safe stop: persona en la zona ?"
    assert trigger_reason(None) == "safe stop" and trigger_reason("") == "safe stop"
    assert len(trigger_reason("x" * 500)) == 120
    assert re.fullmatch(r"[\x20-\x7e]{1,120}", trigger_reason("質問\x00\x1f text"))


def test_a_camera_that_fails_never_reaches_the_service_and_the_others_are_still_asked() -> None:
    calls: list[str] = []
    messages: list[str] = []

    def flaky(url: str, body: bytes, token: str) -> None:
        calls.append(url)
        if "cam0" in url:
            raise ConnectionError("down")

    triggers = RecordTriggerClient(
        RecordTriggerSettings(enabled=True, targets=(TARGET, "http://127.0.0.1:9/cam1/record/trigger"), min_interval_s=0.0), flaky, Clock(), messages.append, background=False
    )
    triggers.observe(verdict("SAFE_STOP"))
    assert len(calls) == 2 and triggers.failed == 1 and triggers.sent == 1 and len(messages) == 1


def test_the_settings_are_checked() -> None:
    assert parse_record_trigger_settings(None) == RecordTriggerSettings()
    on = parse_record_trigger_settings({"enabled": True, "targets": [TARGET], "timeoutS": 1, "minIntervalS": 2})
    assert on.enabled and on.targets == (TARGET,) and on.timeout_s == 1.0 and on.min_interval_s == 2.0
    for bad in (
        [], {"unknown": 1}, {"enabled": "yes"}, {"enabled": True}, {"targets": "x"}, {"targets": ["http://127.0.0.1/cam/other"]}, {"targets": ["file:///etc/passwd/record/trigger"]},
        {"targets": ["http://10.0.0.5:8080/cam0/record/trigger"]}, {"targets": [TARGET] * 17}, {"targets": [TARGET], "timeoutS": 0}, {"targets": [TARGET], "timeoutS": True},
        {"targets": [TARGET], "minIntervalS": -1}, {"targets": [TARGET], "tokenFile": 5},
    ):
        with pytest.raises(RecordTriggerConfigError):
            parse_record_trigger_settings(bad)
    remote = parse_record_trigger_settings({"internal": True, "targets": ["http://10.0.0.5:8080/cam0/record/trigger"]})
    assert remote.internal and remote.targets


def test_the_token_comes_from_a_file_and_an_unreadable_file_stops_the_start(tmp_path: Any) -> None:
    token = tmp_path / "token"
    token.write_text("secret-token\n", encoding="utf-8")
    assert build_client(RecordTriggerSettings(enabled=True, targets=(TARGET,), token_file=str(token)))._token == "secret-token"
    with pytest.raises(RecordTriggerConfigError):
        build_client(RecordTriggerSettings(enabled=True, targets=(TARGET,), token_file=str(tmp_path / "missing")))


class _Streamer(BaseHTTPRequestHandler):
    """The trigger route of the camera streamer as documented: bearer token, {reason, source}, 202 / 429 / 400 / 401 / 404."""

    received: list[dict[str, Any]] = []
    token = "streamer-token"
    busy = False

    def do_POST(self) -> None:  # noqa: N802
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self.send_response(401)
            self.end_headers()
            return
        if not re.fullmatch(r"/[A-Za-z0-9._~-]{1,64}/record/trigger", self.path):
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            assert isinstance(body, dict) and set(body) <= {"reason", "source"}
            assert re.fullmatch(r"[\x20-\x7e]{1,120}", body["reason"])
        except Exception:  # noqa: BLE001
            self.send_response(400)
            self.end_headers()
            return
        type(self).received.append({"camera": self.path.split("/")[1], **body})
        self.send_response(429 if type(self).busy else 202)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: Any) -> None:
        pass


@contextmanager
def _streamer() -> Iterator[int]:
    _Streamer.received, _Streamer.busy = [], False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Streamer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_http_sender_reaches_the_stand_in_streamer_with_its_token_and_treats_the_minimum_gap_as_no_failure() -> None:
    with _streamer() as port:
        body = json.dumps({"reason": "safe stop: x", "source": "safety-zones"}).encode()
        url = f"http://127.0.0.1:{port}/cam0/record/trigger"
        send = http_sender(2.0)
        send(url, body, "streamer-token")
        assert _Streamer.received == [{"camera": "cam0", "reason": "safe stop: x", "source": "safety-zones"}]
        _Streamer.busy = True
        send(url, body, "streamer-token")  # 429: the streamer already has a recording for this stop
        with pytest.raises(Exception):  # noqa: B017 - urllib.error.HTTPError
            send(url, body, "wrong")


def _zones() -> dict:
    return {"zones": [{"id": "d", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 2, "y": 2, "z": 2}}],
            "calibration": {"version": "c", "source": "manual", "calibrated_at": datetime.now(timezone.utc).date().isoformat(), "max_age_days": 30}}


def _check(base: str, x: float) -> dict:
    body: dict[str, Any] = {"zones": _zones(), "detections": {"objects": [{"id": "p", "position": {"x": x, "y": 0, "z": 0}}]}}
    body["observation"] = {"active": True, "observedAt": datetime.now(timezone.utc).isoformat(), "maxAgeSeconds": 3600, "error": None}
    request = urllib.request.Request(base + "/check", data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - loopback test server
        return json.loads(response.read())


@contextmanager
def _running(triggers: RecordTriggerClient | None) -> Iterator[str]:
    server = SafetyZonesServer(("127.0.0.1", 0), "test")
    server.record_trigger = triggers
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_person_in_a_danger_zone_makes_the_running_service_ask_the_stand_in_streamer_and_the_verdict_is_unchanged() -> None:
    with _streamer() as port:
        settings = RecordTriggerSettings(enabled=True, targets=(f"http://127.0.0.1:{port}/cam0/record/trigger",), min_interval_s=0.0)
        triggers = RecordTriggerClient(settings, http_sender(2.0), token="streamer-token")
        with _running(triggers) as base:
            outside, inside = _check(base, 50.0), _check(base, 1.0)
        triggers.join()
        with _running(None) as base:
            plain_outside, plain_inside = _check(base, 50.0), _check(base, 1.0)
    assert inside["sdkSafetyState"]["state"] == plain_inside["sdkSafetyState"]["state"] == "SAFE_STOP"
    assert len(_Streamer.received) == 1 and _Streamer.received[0]["camera"] == "cam0" and _Streamer.received[0]["source"] == "safety-zones"
    for with_client, without in ((outside, plain_outside), (inside, plain_inside)):
        for key in ("state", "reason", "breaches"):
            assert with_client[key] == without[key]


def test_the_zones_file_switches_the_client_on_and_the_rest_of_the_file_still_loads(tmp_path: Any) -> None:
    from hydra_umc_safety_zones.config import load_zone_set
    from hydra_umc_safety_zones.main import _start_record_trigger

    path = tmp_path / "zones.json"
    path.write_text(json.dumps({**_zones(), "recordTrigger": {"enabled": True, "targets": [TARGET]}}), encoding="utf-8")
    assert load_zone_set(str(path)) is not None
    started = _start_record_trigger(str(path))
    assert started is not None and started.settings.targets == (TARGET,)
    path.write_text(json.dumps(_zones()), encoding="utf-8")
    assert _start_record_trigger(str(path)) is None and _start_record_trigger("") is None
    path.write_text(json.dumps({**_zones(), "recordTrigger": {"enabled": True, "targets": ["http://example.org/cam/record/trigger"]}}), encoding="utf-8")
    with pytest.raises(RecordTriggerConfigError):
        _start_record_trigger(str(path))
