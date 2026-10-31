# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_stateful.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The stateful mode (`serve --stateful`): heartbeats stamped by the service's own
clock, a verdict that expires, and the state pushed on `GET /events`.

The clock is simulated: the tests move time by hand, nothing sleeps for a TTL."""
from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from hydra_umc_safety_zones.api import SafetyZonesServer
from hydra_umc_safety_zones.observation import ObservationStatus
from hydra_umc_safety_zones.stateful import StatefulMonitor, StatefulSettings, parse_stateful_settings

TOKEN = "s3cret"


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


def _beat(*, active: bool = True, max_age: float = 3600, error: str | None = None, observed_at: str | None = None) -> dict:
    return {"active": active, "observedAt": observed_at or datetime.now(timezone.utc).isoformat(), "maxAgeSeconds": max_age, "error": error}


def _check_body(x: float = 9.0) -> dict:
    return {"zones": _zones(), "detections": {"objects": [{"id": "p", "position": {"x": x, "y": 0, "z": 0}}]}}


@contextmanager
def _server(stateful: StatefulSettings | None, clock: FakeClock | None = None, token: str | None = None) -> Iterator[tuple[str, SafetyZonesServer]]:
    server = SafetyZonesServer(("127.0.0.1", 0), "test", token=token, stateful=stateful, clock=clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _call(base: str, method: str, path: str, body: object = None, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    request = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


ON = StatefulSettings(enabled=True, heartbeat_ttl_s=2.0, state_ttl_s=2.0, keepalive_s=0.2)


# -- without the switch nothing changes ------------------------------------------------------------------------------------------


def test_without_the_switch_the_new_routes_do_not_exist() -> None:
    with _server(None, token=TOKEN) as (base, server):
        assert server.monitor is None
        assert _call(base, "POST", "/heartbeat", _beat())[0] == 404  # not 401: the route is simply not there
        assert _call(base, "GET", "/events")[0] == 404
        assert _call(base, "GET", "/events", token=TOKEN)[0] == 404


def test_a_settings_object_with_the_mode_off_is_the_same_as_no_settings() -> None:
    with _server(StatefulSettings(enabled=False)) as (base, server):
        assert server.monitor is None
        assert _call(base, "POST", "/heartbeat", _beat())[0] == 404


def test_without_the_switch_the_request_still_brings_its_own_observation() -> None:
    with _server(None) as (base, _):
        status, answer = _call(base, "POST", "/check", {**_check_body(), "observation": _beat()})
        assert status == 200 and answer["state"] == "ready"
        status, answer = _call(base, "POST", "/check", _check_body())
        assert status == 200 and answer["state"] == "inhibited"


# -- heartbeats ------------------------------------------------------------------------------------------------------------------


def test_a_check_with_no_heartbeat_is_inhibited() -> None:
    with _server(ON, FakeClock()) as (base, _):
        status, answer = _call(base, "POST", "/check", _check_body())
        assert status == 200
        assert answer["state"] == "inhibited" and answer["sdkSafetyState"]["state"] == "INHIBITED"
        assert "no heartbeat" in answer["reason"]


def test_a_heartbeat_makes_the_cell_ready_and_it_lapses_on_the_services_clock() -> None:
    clock = FakeClock()
    with _server(ON, clock) as (base, _):
        assert _call(base, "POST", "/heartbeat", _beat())[1] == {"accepted": True, "validForSeconds": 2.0}
        assert _call(base, "POST", "/check", _check_body())[1]["state"] == "ready"
        clock.now += 1.9
        assert _call(base, "POST", "/check", _check_body())[1]["state"] == "ready"
        clock.now += 0.2  # 2.1 s since the heartbeat arrived
        answer = _call(base, "POST", "/check", _check_body())[1]
        assert answer["state"] == "inhibited" and "heartbeat" in answer["reason"]
        _call(base, "POST", "/heartbeat", _beat())
        assert _call(base, "POST", "/check", _check_body())[1]["state"] == "ready"


def test_a_breach_is_still_a_breach_with_a_live_heartbeat() -> None:
    with _server(ON, FakeClock()) as (base, _):
        _call(base, "POST", "/heartbeat", _beat())
        answer = _call(base, "POST", "/check", _check_body(x=1.0))[1]
        assert answer["state"] == "danger" and answer["estopRequests"]


def test_the_observer_cannot_make_its_evidence_last_longer_than_the_ttl() -> None:
    """It claims an hour of validity; the service allows two seconds of it."""
    clock = FakeClock()
    with _server(ON, clock) as (base, server):
        stale = _beat(max_age=3600, observed_at="2020-01-01T00:00:00+00:00")
        _call(base, "POST", "/heartbeat", stale)
        assert _call(base, "POST", "/check", _check_body())[1]["state"] == "inhibited"  # its own timestamp is old
        assert server.monitor is not None
        current = server.monitor.current_observation(clock())
        assert current is not None and current.max_age_seconds == 2.0


def test_a_heartbeat_that_reports_an_error_or_an_inactive_observer_is_not_ready() -> None:
    with _server(ON, FakeClock()) as (base, _):
        _call(base, "POST", "/heartbeat", _beat(error="camera lost"))
        answer = _call(base, "POST", "/check", _check_body())[1]
        assert answer["state"] == "inhibited" and answer["sdkSafetyState"]["state"] == "FAULT"
        _call(base, "POST", "/heartbeat", _beat(active=False))
        assert _call(base, "POST", "/check", _check_body())[1]["state"] == "inhibited"


def test_a_request_may_not_bring_its_own_observation_in_the_stateful_mode() -> None:
    with _server(ON, FakeClock()) as (base, _):
        status, answer = _call(base, "POST", "/check", {**_check_body(), "observation": _beat()})
        assert status == 400 and "heartbeat" in answer["error"]


@pytest.mark.parametrize("bad", [{}, {"active": "yes"}, {"active": True}, {"active": True, "maxAgeSeconds": float("inf")}, []])
def test_an_invalid_heartbeat_renews_nothing(bad: object) -> None:
    clock = FakeClock()
    with _server(ON, clock) as (base, _):
        _call(base, "POST", "/heartbeat", _beat())
        clock.now += 1.5
        payload = json.dumps(bad, allow_nan=True).encode()
        request = urllib.request.Request(base + "/heartbeat", data=payload, method="POST", headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=5)
            raise AssertionError("an invalid heartbeat was accepted")
        except urllib.error.HTTPError as error:
            assert error.code == 400
        clock.now += 1.0  # 2.5 s since the only good heartbeat
        assert _call(base, "POST", "/check", _check_body())[1]["state"] == "inhibited"


def test_heartbeat_and_events_demand_the_token_when_one_is_set() -> None:
    with _server(ON, FakeClock(), token=TOKEN) as (base, _):
        assert _call(base, "POST", "/heartbeat", _beat())[0] == 401
        assert _call(base, "POST", "/heartbeat", _beat(), token="wrong")[0] == 401
        assert _call(base, "POST", "/heartbeat", _beat(), token=TOKEN)[0] == 200
        conn = http.client.HTTPConnection(base.removeprefix("http://"), timeout=5)
        conn.request("GET", "/events")
        assert conn.getresponse().status == 401
        conn.close()


# -- the pure monitor, with simulated time ---------------------------------------------------------------------------------------


def _verdict(state: str, sdk: str) -> dict:
    return {"state": state, "reason": "r", "sdkSafetyState": {"schema_version": "1.0", "state": sdk, "source": "t", "timestamp_utc": "x"}}


def test_the_monitor_publishes_on_a_change_and_republishes_at_half_the_validity() -> None:
    monitor = StatefulMonitor(StatefulSettings(enabled=True, state_ttl_s=2.0), "abc")
    subscriber = monitor.subscribe()
    assert subscriber is not None
    monitor.record_verdict(_verdict("ready", "READY"), 0.0)
    monitor.record_verdict(_verdict("ready", "READY"), 0.4)  # same state, too soon: nothing new
    monitor.record_verdict(_verdict("ready", "READY"), 1.1)  # half of the validity has gone by: renewed
    monitor.record_verdict(_verdict("danger", "SAFE_STOP"), 1.2)  # a change: at once
    received = [subscriber.events.get_nowait() for _ in range(subscriber.events.qsize())]
    assert [e["sdkSafetyState"]["state"] for e in received] == ["READY", "READY", "SAFE_STOP"]
    assert [e["sequence"] for e in received] == [1, 2, 3]
    assert all(e["validForSeconds"] == 2.0 and e["configSha256"] == "abc" for e in received)


def test_the_monitor_itself_publishes_inhibited_when_a_verdict_expires() -> None:
    monitor = StatefulMonitor(StatefulSettings(enabled=True, state_ttl_s=2.0, heartbeat_ttl_s=60.0))
    subscriber = monitor.subscribe()
    assert subscriber is not None
    monitor.record_verdict(_verdict("ready", "READY"), 0.0)
    subscriber.events.get_nowait()
    monitor.tick(1.9)
    assert subscriber.events.empty()
    monitor.tick(2.1)
    expired = subscriber.events.get_nowait()
    assert expired["sdkSafetyState"]["state"] == "INHIBITED" and expired["validForSeconds"] == 0.0
    assert "expired" not in expired["reason"] and "nothing renewed" in expired["reason"]
    monitor.tick(5.0)  # once only
    assert subscriber.events.empty()
    monitor.record_verdict(_verdict("ready", "READY"), 6.0)  # a new verdict after the expiry is published again
    assert subscriber.events.get_nowait()["sdkSafetyState"]["state"] == "READY"
    monitor.tick(6.5)
    assert subscriber.events.empty()


def test_a_lost_heartbeat_publishes_inhibited_before_the_verdict_expires() -> None:
    monitor = StatefulMonitor(StatefulSettings(enabled=True, state_ttl_s=10.0, heartbeat_ttl_s=1.0))
    observation = ObservationStatus(active=True, observed_at=datetime.now(timezone.utc), max_age_seconds=5)
    monitor.record_heartbeat(observation, 0.0)
    subscriber = monitor.subscribe()
    assert subscriber is not None
    monitor.record_verdict(_verdict("ready", "READY"), 0.0)
    subscriber.events.get_nowait()
    monitor.tick(0.9)
    assert subscriber.events.empty()
    monitor.tick(1.2)
    event = subscriber.events.get_nowait()
    assert event["sdkSafetyState"]["state"] == "INHIBITED" and "heartbeat" in event["reason"]


def test_a_new_subscriber_is_told_inhibited_until_there_is_a_valid_verdict() -> None:
    monitor = StatefulMonitor(StatefulSettings(enabled=True, state_ttl_s=2.0))
    first = monitor.current_event(0.0)
    assert first["sdkSafetyState"]["state"] == "INHIBITED" and first["validForSeconds"] == 0.0
    monitor.record_verdict(_verdict("ready", "READY"), 1.0)
    held = monitor.current_event(1.5)
    assert held["sdkSafetyState"]["state"] == "READY" and held["validForSeconds"] == pytest.approx(1.5)
    assert monitor.current_event(3.5)["sdkSafetyState"]["state"] == "INHIBITED"
    assert monitor.current_event(0.5)["sdkSafetyState"]["state"] == "INHIBITED"  # a clock that went backwards is not trusted


def test_the_subscriber_limit_and_a_slow_reader() -> None:
    monitor = StatefulMonitor(StatefulSettings(enabled=True, max_subscribers=2))
    a, b = monitor.subscribe(), monitor.subscribe()
    assert a is not None and b is not None and monitor.subscribe() is None
    monitor.unsubscribe(a)
    assert monitor.subscribe() is not None
    for i in range(40):  # nobody reads: the queue keeps the newest sixteen
        monitor.record_verdict(_verdict("ready" if i % 2 else "danger", "READY" if i % 2 else "SAFE_STOP"), float(i))
    kept = [b.events.get_nowait() for _ in range(b.events.qsize())]
    assert len(kept) == 16 and kept[-1]["sequence"] == 40


def test_the_settings_are_validated() -> None:
    assert parse_stateful_settings(None) == StatefulSettings(enabled=False)
    assert parse_stateful_settings({"enabled": True, "heartbeat_ttl_s": 1.5}).heartbeat_ttl_s == 1.5
    for bad in (
        [], {"unknown": 1}, {"heartbeat_ttl_s": 0}, {"heartbeat_ttl_s": float("nan")}, {"state_ttl_s": -1}, {"state_ttl_s": True},
        {"keepalive_s": "5"}, {"max_subscribers": 0}, {"max_subscribers": 17}, {"max_subscribers": 2.5}, {"enabled": "yes"},
    ):
        with pytest.raises(ValueError):
            parse_stateful_settings(bad)


# -- the stream over HTTP --------------------------------------------------------------------------------------------------------


def _read_event(response: http.client.HTTPResponse) -> dict:
    """Reads lines until one `data:` line, skipping comments, and returns its JSON."""
    while True:
        line = response.fp.readline().decode()  # type: ignore[union-attr]
        if not line:
            raise AssertionError("the stream ended")
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])


def test_the_stream_pushes_the_state_and_the_expiry() -> None:
    clock = FakeClock()
    with _server(ON, clock) as (base, server):
        server.start_watcher(interval_s=0.02)
        conn = http.client.HTTPConnection(base.removeprefix("http://"), timeout=10)
        conn.request("GET", "/events")
        response = conn.getresponse()
        assert response.status == 200 and response.getheader("Content-Type") == "text/event-stream"
        first = _read_event(response)
        assert first["sdkSafetyState"]["state"] == "INHIBITED" and first["validForSeconds"] == 0.0

        _call(base, "POST", "/heartbeat", _beat())
        _call(base, "POST", "/check", _check_body())
        assert _read_event(response)["sdkSafetyState"]["state"] == "READY"
        _call(base, "POST", "/check", _check_body(x=1.0))
        assert _read_event(response)["sdkSafetyState"]["state"] == "SAFE_STOP"

        clock.now += 5.0  # nothing renews the verdict: the service publishes INHIBITED by itself
        expired = _read_event(response)
        assert expired["sdkSafetyState"]["state"] == "INHIBITED" and expired["validForSeconds"] == 0.0
        conn.close()


def test_the_stream_sends_keepalives_and_frees_its_slot_when_the_consumer_leaves() -> None:
    with _server(ON, FakeClock()) as (base, server):
        conn = http.client.HTTPConnection(base.removeprefix("http://"), timeout=10)
        conn.request("GET", "/events")
        response = conn.getresponse()
        _read_event(response)
        seen_comment = False
        for _ in range(6):
            line = response.fp.readline().decode()  # type: ignore[union-attr]
            if line.startswith(": keepalive"):
                seen_comment = True
                break
        assert seen_comment
        assert server.monitor is not None and server.monitor.subscriber_count == 1
        response.close()  # the response holds its own reference to the socket
        conn.close()
        for _ in range(100):  # the handler notices the closed connection at its next write
            if server.monitor.subscriber_count == 0:
                break
            threading.Event().wait(0.05)
        assert server.monitor.subscriber_count == 0


def test_a_stream_over_the_limit_is_refused_with_503() -> None:
    settings = StatefulSettings(enabled=True, max_subscribers=1, keepalive_s=0.2)
    with _server(settings, FakeClock()) as (base, _):
        first = http.client.HTTPConnection(base.removeprefix("http://"), timeout=10)
        first.request("GET", "/events")
        _read_event(first.getresponse())
        second = http.client.HTTPConnection(base.removeprefix("http://"), timeout=10)
        second.request("GET", "/events")
        assert second.getresponse().status == 503
        first.close()
        second.close()
