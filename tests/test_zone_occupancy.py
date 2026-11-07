# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_zone_occupancy.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The publisher of the occupied zones: what counts as occupied, when a message goes out, the settings, the MQTT side with a fake client and the running service."""
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
from hydra_umc_safety_zones.zone_occupancy import (
    DEFAULT_TOPIC,
    ZoneOccupancyConfigError,
    ZoneOccupancyEmitter,
    ZoneOccupancySettings,
    build_publisher,
    occupied_zones,
    parse_zone_occupancy_settings,
)

ON = ZoneOccupancySettings(enabled=True, refresh_s=1.0)


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


def verdict(*zones: str) -> dict[str, Any]:
    return {"breaches": [{"zone_id": zone, "level": "danger", "object_id": "p"} for zone in zones]}


def test_the_occupied_zones_are_the_zones_the_breaches_name() -> None:
    assert occupied_zones(verdict()) == []
    assert occupied_zones(verdict("press-2", "press-1", "press-2")) == ["press-1", "press-2"]


def test_a_verdict_that_does_not_say_publishes_nothing_instead_of_free() -> None:
    for bad in ({}, {"breaches": "x"}, {"breaches": [3]}, {"breaches": [{"zone_id": 5}]}, {"breaches": [{"zone_id": "bad zone"}]}, {"breaches": [{"zone_id": "ok"}, {}]}):
        assert occupied_zones(bad) is None
    sink = Sink()
    ZoneOccupancyEmitter(ON, sink).observe({"breaches": [{"zone_id": 5}]})
    assert sink.sent == []


def test_the_message_is_the_one_the_zone_interlock_reads_and_is_not_retained() -> None:
    sink = Sink()
    ZoneOccupancyEmitter(ON, sink, Clock(), lambda: 1234567.9).observe(verdict("press-1"))
    assert sink.sent == [(DEFAULT_TOPIC, {"occupied": ["press-1"], "observed_at": 1234567}, False)]


def test_a_change_goes_out_at_once_and_the_same_set_is_repeated_after_the_refresh_time() -> None:
    sink, clock = Sink(), Clock()
    emitter = ZoneOccupancyEmitter(ON, sink, clock)
    emitter.observe(verdict())
    emitter.observe(verdict())
    assert len(sink.sent) == 1
    clock.now += 0.2
    emitter.observe(verdict("a"))  # a change is not held back by the refresh time
    emitter.observe(verdict("a"))
    assert [m[1]["occupied"] for m in sink.sent] == [[], ["a"]]
    clock.now += 1.0
    emitter.observe(verdict("a"))  # the reader ages a message out: it is repeated while the service runs
    assert [m[1]["occupied"] for m in sink.sent] == [[], ["a"], ["a"]]
    emitter.observe(verdict())
    assert sink.sent[-1][1]["occupied"] == [] and emitter.sent == 4


def test_off_by_default_publishes_nothing() -> None:
    sink = Sink()
    ZoneOccupancyEmitter(ZoneOccupancySettings(), sink).observe(verdict("a"))
    assert sink.sent == [] and ZoneOccupancySettings().enabled is False


def test_a_broker_that_fails_never_reaches_the_service_and_is_tried_again() -> None:
    calls: list[int] = []
    messages: list[str] = []

    def broken(topic: str, payload: bytes, retain: bool) -> None:
        calls.append(1)
        raise ConnectionError("down")

    emitter = ZoneOccupancyEmitter(ON, broken, Clock(), log=messages.append)
    emitter.observe(verdict("a"))
    emitter.observe(verdict("a"))
    assert len(calls) == 2 and emitter.failed == 2 and emitter.sent == 0 and len(messages) == 1


def test_the_settings_are_checked() -> None:
    assert parse_zone_occupancy_settings(None) == ZoneOccupancySettings()
    on = parse_zone_occupancy_settings({"enabled": True, "topic": "hydra/safety/zones/hall", "refreshS": 2})
    assert on.enabled and on.topic == "hydra/safety/zones/hall" and on.refresh_s == 2.0
    assert on.broker.client_id == "hydra-umc-safety-zones-occupancy"
    for bad in (
        [], {"enabled": "yes"}, {"topic": "elsewhere"}, {"topic": "hydra/safety/#"}, {"refreshS": 0}, {"refreshS": True}, {"refreshS": 99999},
        {"unknown": 1}, {"broker": {"host": "10.0.0.5"}}, {"broker": {"port": 0}},
    ):
        with pytest.raises(ZoneOccupancyConfigError):
            parse_zone_occupancy_settings(bad)
    assert parse_zone_occupancy_settings({"broker": {"host": "10.0.0.5", "internal": True, "clientId": "mine"}}).broker.client_id == "mine"


class FakeClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, int, bool]] = []

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        self.published.append((topic, payload, qos, retain))

    def connect(self, host: str, port: int, keepalive: int) -> None:
        pass

    def loop_start(self) -> None:
        pass

    def reconnect_delay_set(self, **kwargs: Any) -> None:
        pass


def test_the_mqtt_side_publishes_with_a_fake_client() -> None:
    client = FakeClient()
    publisher = build_publisher(ON, client)
    publisher.start()
    ZoneOccupancyEmitter(ON, publisher.publish, Clock()).observe(verdict("a"))
    topic, payload, qos, retain = client.published[0]
    assert topic == DEFAULT_TOPIC and json.loads(payload)["occupied"] == ["a"] and retain is False and qos == 1


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
def _running(emitter: ZoneOccupancyEmitter | None) -> Iterator[str]:
    server = SafetyZonesServer(("127.0.0.1", 0), "test")
    server.occupancy_emitter = emitter
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_running_service_publishes_the_zone_a_person_is_in_and_the_verdict_is_unchanged() -> None:
    sink = Sink()
    with _running(ZoneOccupancyEmitter(ON, sink)) as base:
        inside, outside = _check(base, 1.0), _check(base, 50.0)
    with _running(None) as base:
        plain_inside, plain_outside = _check(base, 1.0), _check(base, 50.0)
    assert [m[1]["occupied"] for m in sink.sent] == [["d"], []]
    for with_emitter, without in ((inside, plain_inside), (outside, plain_outside)):
        for key in ("state", "reason", "breaches"):
            assert with_emitter[key] == without[key]


def test_the_zones_file_switches_the_publisher_on_and_the_rest_of_the_file_still_loads(tmp_path: Any) -> None:
    from hydra_umc_safety_zones.config import load_zone_set
    from hydra_umc_safety_zones.main import _start_zone_occupancy

    path = tmp_path / "zones.json"
    path.write_text(json.dumps({**_zones(), "zoneOccupancy": {"enabled": True}}), encoding="utf-8")
    assert load_zone_set(str(path)) is not None
    started = _start_zone_occupancy(str(path))
    assert started is not None and started[0].settings.topic == DEFAULT_TOPIC and started[1].client is None  # built, not yet started
    path.write_text(json.dumps(_zones()), encoding="utf-8")
    assert _start_zone_occupancy(str(path)) is None and _start_zone_occupancy("") is None
    path.write_text(json.dumps({**_zones(), "zoneOccupancy": {"enabled": True, "topic": "elsewhere"}}), encoding="utf-8")
    with pytest.raises(ZoneOccupancyConfigError):
        _start_zone_occupancy(str(path))
