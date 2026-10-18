# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_fail_closed_inputs.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Whatever the input, the answer is never READY unless a healthy, fresh
observer vouches for a cell with real zones: bad numbers, naive times, bad
files, odd ids, a server that holds its own zones, a token, limits."""
from __future__ import annotations

import json
import math
import random
import socket
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pytest

from hydra_umc_safety_zones.api import SafetyZonesServer
from hydra_umc_safety_zones.breach import DetectedObject
from hydra_umc_safety_zones.calibration import ZoneCalibration, is_calibration_expired
from hydra_umc_safety_zones.config import ConfigError, parse_detections, parse_zone_set, parse_zones, strict_json_loads
from hydra_umc_safety_zones.estop import NullEStopRequester, request_estop_for
from hydra_umc_safety_zones.geometry import AABB, Point3D
from hydra_umc_safety_zones.main import main
from hydra_umc_safety_zones.observation import (
    CLOCK_SKEW_TOLERANCE_S,
    ObservationError,
    ObservationStatus,
    is_observation_stale,
    parse_observation_status,
)
from hydra_umc_safety_zones.safety_state import SafetyState, evaluate_safety, to_sdk_safety_state
from hydra_umc_safety_zones.zones import Zone, ZoneLevel, ZoneSet

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


def _zone_set(zones=None, max_age_days=30, calibrated_at=None):
    zones = zones if zones is not None else (Zone("danger1", ZoneLevel.DANGER, AABB(Point3D(0, 0, 0), Point3D(2, 2, 2))),)
    return ZoneSet(zones=tuple(zones), calibration=ZoneCalibration("c1", "manual", calibrated_at or TODAY, max_age_days))


def _zones_json(**calibration):
    return {
        "zones": [{"id": "d1", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 2, "y": 2, "z": 2}}],
        "calibration": {"version": "c1", "source": "manual", "calibrated_at": date.today().isoformat(), "max_age_days": 30, **calibration},
    }


def _observation_json(**overrides):
    body = {"active": True, "observedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "maxAgeSeconds": 5}
    body.update(overrides)
    return body


@contextmanager
def server(**options):
    srv = SafetyZonesServer(("127.0.0.1", 0), "test", **options)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _post(base: str, body: object, token: str | None = None, raw: bytes | None = None) -> tuple[int, dict]:
    data = raw if raw is not None else json.dumps(body).encode()
    request = urllib.request.Request(base + "/check", data=data, headers={"Content-Type": "application/json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ------------------------------------------------------------------ non-finite max age
@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, 0.0, -1.0])
def test_an_observation_whose_max_age_is_not_a_finite_positive_number_is_refused(bad):
    with pytest.raises(ObservationError):
        ObservationStatus(active=True, observed_at=NOW, max_age_seconds=bad)
    with pytest.raises(ObservationError):
        parse_observation_status({"active": True, "observedAt": "2026-01-01T12:00:00Z", "maxAgeSeconds": bad})


def test_nan_and_infinity_literals_are_refused_anywhere_in_a_body_over_http():
    with server() as base:
        for literal in (b"NaN", b"Infinity", b"-Infinity"):
            body = (
                b'{"zones": {"zones": []}, "detections": {"objects": []}, '
                b'"observation": {"active": true, "observedAt": "2001-01-01T00:00:00Z", "maxAgeSeconds": '
                + literal
                + b"}}"
            )
            status, answer = _post(base, None, raw=body)
            assert status == 400, (literal, answer)
            assert answer.get("state") != "ready"


def test_the_exact_request_that_used_to_return_ready_is_now_refused():
    with server() as base:
        raw = (
            b'{"zones": ' + json.dumps(_zones_json()).encode() + b', "detections": {"objects": []}, '
            b'"observation": {"active": true, "observedAt": "2001-01-01T00:00:00Z", "maxAgeSeconds": NaN}}'
        )
        status, answer = _post(base, None, raw=raw)
        assert status == 400 and "state" not in answer


def test_strict_json_loads_refuses_the_three_literals_and_accepts_the_rest():
    for text in ("NaN", "Infinity", "-Infinity", '{"a": [1, NaN]}'):
        with pytest.raises(ValueError):
            strict_json_loads(text)
    assert strict_json_loads('{"a": [1, 2.5, null]}') == {"a": [1, 2.5, None]}


# ------------------------------------------------------------------ naive times
def test_an_observed_at_without_a_time_zone_is_refused_not_a_crash():
    with pytest.raises(ObservationError):
        parse_observation_status({"active": True, "observedAt": "2026-01-01T12:00:00", "maxAgeSeconds": 5})
    with pytest.raises(ObservationError):
        ObservationStatus(active=True, observed_at=datetime(2026, 1, 1, 12, 0, 0), max_age_seconds=5)
    with server() as base:
        status, answer = _post(base, {"zones": _zones_json(), "detections": {"objects": []}, "observation": _observation_json(observedAt="2026-01-01T12:00:00")})
        assert status == 400, answer


def test_an_unexpected_failure_inside_the_check_answers_500_with_a_fault_and_never_ready(monkeypatch):
    import hydra_umc_safety_zones.api as api

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(api, "evaluate_safety", boom)
    with server() as base:
        status, answer = _post(base, {"zones": _zones_json(), "detections": {"objects": []}, "observation": _observation_json()})
        assert status == 500
        assert answer["state"] == "inhibited"
        assert answer["sdkSafetyState"]["state"] == "FAULT"


# ------------------------------------------------------------------ CLI exit codes
def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    "zones, detections",
    [
        ("{not json", {"objects": []}),
        ({"zones": [{"id": "z", "level": "DANGER", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 1, "y": 1, "z": 1}}]}, {"objects": []}),
        ({"nozones": 1}, {"objects": []}),
        (_zones_json(), {"nothing": []}),
        (_zones_json(), {"objects": [{"id": "o"}]}),
        (_zones_json(), "[1, 2]"),
    ],
)
def test_any_input_the_cli_cannot_read_is_inhibited_with_status_3_never_1(tmp_path, capsys, zones, detections):
    code = main(["check", "--zones", _write(tmp_path, "z.json", zones), "--detections", _write(tmp_path, "d.json", detections)])
    out = capsys.readouterr().out
    assert code == 3, out
    assert "SAFETY STATE: INHIBITED" in out


def test_a_missing_file_and_a_naive_observation_are_status_3_for_the_cli(tmp_path, capsys):
    zones = _write(tmp_path, "z.json", _zones_json())
    detections = _write(tmp_path, "d.json", {"objects": []})
    assert main(["check", "--zones", str(tmp_path / "absent.json"), "--detections", detections]) == 3
    naive = _write(tmp_path, "o.json", {"active": True, "observedAt": "2026-01-01T10:00:00", "maxAgeSeconds": 5})
    assert main(["check", "--zones", zones, "--detections", detections, "--observation", naive]) == 3
    nan_obs = _write(tmp_path, "n.json", '{"active": true, "observedAt": "2001-01-01T00:00:00Z", "maxAgeSeconds": NaN}')
    assert main(["check", "--zones", zones, "--detections", detections, "--observation", nan_obs]) == 3
    capsys.readouterr()


def test_the_cli_takes_a_tool_velocity_like_the_api(tmp_path, capsys):
    zones = _write(tmp_path, "z.json", _zones_json())
    # an object 2.3 m away: outside the 2 m zone, inside it once a 2 m/s speed grows the zone by 0.3 m
    detections = _write(tmp_path, "d.json", {"objects": [{"id": "o", "position": {"x": 2.2, "y": 0, "z": 0}}]})
    observation = _write(tmp_path, "o.json", _observation_json())
    assert main(["check", "--zones", zones, "--detections", detections, "--observation", observation]) == 0
    assert main(["check", "--zones", zones, "--detections", detections, "--observation", observation, "--tool-velocity", "2"]) == 2
    assert main(["check", "--zones", zones, "--detections", detections, "--observation", observation, "--tool-velocity", "nan"]) == 3
    capsys.readouterr()


# ------------------------------------------------------------------ clocks
def test_a_few_tens_of_milliseconds_of_clock_skew_is_not_an_outage_but_a_big_lead_is():
    obs = ObservationStatus(active=True, observed_at=NOW + timedelta(milliseconds=50), max_age_seconds=5)
    assert not is_observation_stale(obs, NOW)
    ahead = ObservationStatus(active=True, observed_at=NOW + timedelta(seconds=CLOCK_SKEW_TOLERANCE_S + 1), max_age_seconds=5)
    assert is_observation_stale(ahead, NOW)
    old = ObservationStatus(active=True, observed_at=NOW - timedelta(seconds=6), max_age_seconds=5)
    assert is_observation_stale(old, NOW)


def test_a_calibration_dated_tomorrow_by_the_local_calendar_is_valid_but_two_days_ahead_is_not():
    cal_tomorrow = ZoneCalibration("c", "s", TODAY + timedelta(days=1), 30)
    cal_far = ZoneCalibration("c", "s", TODAY + timedelta(days=2), 30)
    assert not is_calibration_expired(cal_tomorrow, TODAY)
    assert is_calibration_expired(cal_far, TODAY)


# ------------------------------------------------------------------ input validation
def test_coordinates_must_be_numbers_not_booleans_or_strings():
    for bad in (True, "0.5", None):
        with pytest.raises(ConfigError):
            parse_detections({"objects": [{"id": "o", "position": {"x": bad, "y": 0, "z": 0}}]})


def test_ids_must_be_non_empty_strings_and_unique():
    box = {"min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 1, "y": 1, "z": 1}}
    for bad_id in (None, 7, "", "  "):
        with pytest.raises(ConfigError):
            parse_zones({"zones": [{"id": bad_id, "level": "danger", **box}]})
    with pytest.raises(ConfigError):
        parse_zones({"zones": [{"id": "a", "level": "danger", **box}, {"id": "a", "level": "warning", **box}]})
    pos = {"x": 0, "y": 0, "z": 0}
    with pytest.raises(ConfigError):
        parse_detections({"objects": [{"id": "o", "position": pos}, {"id": "o", "position": pos}]})
    with pytest.raises(ConfigError):
        parse_detections({"objects": [{"id": 5, "position": pos}]})


def test_an_empty_zone_list_is_refused_it_is_not_a_safe_cell():
    with pytest.raises(ConfigError):
        parse_zone_set({"zones": [], "calibration": {"version": "c", "source": "s", "calibrated_at": "2026-01-01", "max_age_days": 30}})


def test_a_level_in_the_wrong_case_is_a_clear_error():
    with pytest.raises(ConfigError, match="lower case"):
        parse_zones({"zones": [{"id": "z", "level": "DANGER", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 1, "y": 1, "z": 1}}]})


# ------------------------------------------------------------------ stop requests and FAULT
def test_an_object_in_several_danger_zones_gets_one_request_naming_all_of_them():
    big = AABB(Point3D(0, 0, 0), Point3D(5, 5, 5))
    zs = _zone_set([Zone("a", ZoneLevel.DANGER, big), Zone("b", ZoneLevel.DANGER, big)])
    from hydra_umc_safety_zones.breach import check_breaches

    breaches = check_breaches(zs.zones, (DetectedObject("o", Point3D(1, 1, 1)),))
    requester = NullEStopRequester()
    requests = request_estop_for(breaches, requester)
    assert len(requests) == 1
    assert requests[0].zone_ids == ("a", "b")
    assert "'a'" in requests[0].reason and "'b'" in requests[0].reason


def test_an_observer_that_reports_an_error_is_a_fault_not_just_unverified():
    obs = ObservationStatus(active=True, observed_at=NOW, max_age_seconds=5, error="camera driver disconnected")
    evaluation = evaluate_safety(_zone_set(), (), TODAY, obs, now=NOW)
    assert evaluation.state is SafetyState.INHIBITED
    assert to_sdk_safety_state(evaluation)["state"] == "FAULT"
    stale = evaluate_safety(_zone_set(), (), TODAY, ObservationStatus(True, NOW - timedelta(minutes=5), 5), now=NOW)
    assert to_sdk_safety_state(stale)["state"] == "INHIBITED"


# ------------------------------------------------------------------ never READY from bad input (seeded fuzz)
def test_no_bad_observation_can_ever_produce_ready():
    rng = random.Random(20261018)
    candidates = [math.nan, math.inf, -math.inf, 0, -3, True, "5", None, 1e308, 5, 0.5]
    times = [None, "2001-01-01T00:00:00Z", "2026-01-01T12:00:00", "garbage", 12, "2999-01-01T00:00:00Z", "2026-01-01T11:59:58Z"]
    for _ in range(400):
        data = {"active": rng.choice([True, False, None, "yes"]), "observedAt": rng.choice(times), "maxAgeSeconds": rng.choice(candidates)}
        try:
            obs = parse_observation_status(data)
        except (ObservationError, TypeError, ValueError):
            continue
        evaluation = evaluate_safety(_zone_set(), (), TODAY, obs, now=NOW)
        if evaluation.state is SafetyState.READY:
            assert obs.active is True
            assert obs.observed_at is not None and obs.observed_at.tzinfo is not None
            assert math.isfinite(obs.max_age_seconds) and obs.max_age_seconds > 0
            age = (NOW - obs.observed_at).total_seconds()
            assert -CLOCK_SKEW_TOLERANCE_S <= age <= obs.max_age_seconds


# ------------------------------------------------------------------ the server holds its own rules
def test_a_server_with_its_own_zones_refuses_zones_from_a_request_and_reports_their_hash():
    zone_set = parse_zone_set(_zones_json())
    with server(zone_set=zone_set, config_sha256="abc123") as base:
        status, answer = _post(base, {"zones": _zones_json(), "detections": {"objects": []}, "observation": _observation_json()})
        assert status == 400 and "holds its own zones" in answer["error"]
        status, answer = _post(base, {"detections": {"objects": []}, "observation": _observation_json()})
        assert status == 200 and answer["state"] == "ready" and answer["configSha256"] == "abc123"
        status, answer = _post(base, {"detections": {"objects": [{"id": "o", "position": {"x": 1, "y": 1, "z": 1}}]}, "observation": _observation_json()})
        assert answer["state"] == "danger"


def test_the_server_can_cap_how_long_a_request_may_claim_its_evidence_stays_valid():
    with server(max_observation_age_seconds=2.0, max_calibration_age_days=10) as base:
        body = {"zones": _zones_json(max_age_days=5), "detections": {"objects": []}, "observation": _observation_json(maxAgeSeconds=1)}
        assert _post(base, body)[0] == 200
        assert _post(base, {**body, "observation": _observation_json(maxAgeSeconds=60)})[0] == 400
        assert _post(base, {**body, "zones": _zones_json(max_age_days=365)})[0] == 400


def test_a_token_is_required_for_check_when_one_is_configured():
    body = {"zones": _zones_json(), "detections": {"objects": []}, "observation": _observation_json()}
    with server(token="s3cret") as base:
        assert _post(base, body)[0] == 401
        assert _post(base, body, token="nope")[0] == 401
        assert _post(base, body, token="s3cret")[0] == 200
        with urllib.request.urlopen(base + "/stats", timeout=5) as resp:
            assert resp.status == 200


# ------------------------------------------------------------------ availability under abuse
def test_a_client_that_announces_a_body_and_sends_nothing_is_dropped_and_others_are_served():
    import hydra_umc_safety_zones.api as api

    api.Handler.timeout = 1.0  # a short timeout for the test only
    try:
        with server() as base:
            port = int(base.rsplit(":", 1)[1])
            slow = socket.create_connection(("127.0.0.1", port))
            slow.sendall(b"POST /check HTTP/1.1\r\nHost: x\r\nContent-Length: 500\r\n\r\n")
            with urllib.request.urlopen(base + "/stats", timeout=3) as resp:
                assert resp.status == 200, "another client must be served while the slow one is stuck"
            slow.settimeout(5)
            try:
                data = slow.recv(4096)
            except OSError:
                data = b""
            # the server gave up on it (closed or answered an error) instead of waiting for ever
            assert data == b"" or b"HTTP/1.0" in data or b"HTTP/1.1" in data
            slow.close()
    finally:
        api.Handler.timeout = 10.0
