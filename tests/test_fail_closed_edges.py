# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_fail_closed_edges.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The refusals and failure paths that must never read as "all clear":
a request the server cannot parse, a crash inside the evaluation, a flood
of idle connections, and every malformed shape of the configuration files.
Real sockets and a real server, no mocked HTTP layer."""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from enum import Enum

import pytest

from hydra_umc_safety_zones import api
from hydra_umc_safety_zones.api import MAX_BODY_BYTES, MAX_CONNECTIONS, SafetyZonesServer, _write_json
from hydra_umc_safety_zones.calibration import CalibrationError, ZoneCalibration
from hydra_umc_safety_zones.config import (
    ConfigError,
    load_observation_status,
    parse_detections,
    parse_tool_velocity,
    parse_zone_set,
    parse_zones,
)
from hydra_umc_safety_zones.geometry import AABB, Point3D

from datetime import date, datetime, timezone


@contextmanager
def running(**kwargs):
    server = SafetyZonesServer(("127.0.0.1", 0), "test role", **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _raw_request(port: int, head: bytes, body: bytes = b"") -> tuple[int, bytes]:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(head + body)
        data = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data += chunk
    status = int(data.split(b" ", 2)[1])
    return status, data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""


def _post(port: int, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/check", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _valid_request() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "zones": {
            "zones": [{"id": "z", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 1, "y": 1, "z": 1}}],
            "calibration": {"version": "c", "source": "manual", "calibrated_at": now.date().isoformat(), "max_age_days": 30},
        },
        "detections": {"objects": [{"id": "o", "position": {"x": 50, "y": 50, "z": 50}}]},
        "observation": {"active": True, "observedAt": now.isoformat(), "maxAgeSeconds": 3600, "error": None},
    }


# ---- the HTTP layer ----------------------------------------------------------------------------------------------------

def test_a_body_over_the_limit_is_refused_and_the_server_keeps_answering() -> None:
    with running() as (_, port):
        oversize = b"{" + b" " * (MAX_BODY_BYTES + 10) + b"}"
        head = (
            b"POST /check HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nConnection: close\r\n"
            + f"Content-Length: {len(oversize)}\r\n\r\n".encode()
        )
        status, body = _raw_request(port, head, oversize)
        assert status == 400
        assert b"request body must contain" in body
        # the refusal did not wedge the server: a normal request is judged afterwards
        assert _post(port, _valid_request())[0] == 200


@pytest.mark.parametrize("length", [b"abc", b"-5"])
def test_a_content_length_that_is_not_a_size_is_a_400_not_a_crash(length: bytes) -> None:
    with running() as (_, port):
        head = b"POST /check HTTP/1.1\r\nHost: x\r\nConnection: close\r\nContent-Length: " + length + b"\r\n\r\n"
        status, body = _raw_request(port, head)
        assert status == 400
        assert b"error" in body


def test_a_post_to_another_path_is_a_404_and_a_get_of_it_too() -> None:
    with running() as (_, port):
        status, _ = _raw_request(port, b"POST /other HTTP/1.1\r\nHost: x\r\nConnection: close\r\nContent-Length: 2\r\n\r\n", b"{}")
        assert status == 404
        status, _ = _raw_request(port, b"GET /other HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        assert status == 404


def test_an_internal_error_answers_inhibited_with_a_fault_never_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("simulated crash in the evaluation")

    monkeypatch.setattr(api, "evaluate_safety", explode)
    with running() as (_, port):
        status, body = _post(port, _valid_request())
    assert status == 500
    assert body["state"] == "inhibited"
    assert body["breaches"] == [] and body["estopRequests"] == []
    assert body["error"] == "internal error: RuntimeError"
    # the SDK-shaped verdict a consumer reads must not authorise motion
    assert body["sdkSafetyState"]["state"] != "READY"


def test_a_server_that_holds_its_own_zones_refuses_a_request_that_brings_zones() -> None:
    zone_set = parse_zone_set(_valid_request()["zones"])
    with running(zone_set=zone_set) as (_, port):
        status, body = _post(port, _valid_request())
        assert status == 400
        assert "must not send zones" in body["error"]


def test_the_cap_on_observation_age_and_calibration_age_is_enforced() -> None:
    with running(max_observation_age_seconds=60) as (_, port):
        status, body = _post(port, _valid_request())  # claims 3600 s
        assert status == 400 and "maxAgeSeconds" in body["error"]
    with running(max_calibration_age_days=7) as (_, port):
        status, body = _post(port, _valid_request())  # claims 30 days
        assert status == 400 and "max_age_days" in body["error"]


def test_idle_connections_cannot_use_up_the_server() -> None:
    with running() as (_, port):
        idle = []
        try:
            for _ in range(MAX_CONNECTIONS):
                idle.append(socket.create_connection(("127.0.0.1", port), timeout=5))
            # every slot is held by a client that sends nothing: the next one is turned away at once
            extra = socket.create_connection(("127.0.0.1", port), timeout=5)
            try:
                extra.settimeout(5)
                reply = extra.recv(200)
            finally:
                extra.close()
            assert reply.startswith(b"HTTP/1.1 503")
        finally:
            for connection in idle:
                connection.close()
        # once the idle ones are gone the slots come back
        for _ in range(50):
            status, _ = _raw_request(port, b"GET /stats HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            if status == 200:
                break
        assert status == 200


class _Handler:
    def __init__(self) -> None:
        self.status = None
        self.headers: list[tuple[str, str]] = []
        self.written = b""

        class _Out:
            def write(inner, data: bytes) -> None:  # noqa: N805
                self.written += data

        self.wfile = _Out()

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.headers.append((name, value))

    def end_headers(self) -> None:
        pass


class _Colour(Enum):
    RED = "red"


def test_the_json_writer_turns_enums_dataclasses_and_unknown_objects_into_text_not_a_crash() -> None:
    handler = _Handler()
    _write_json(handler, 200, {"e": _Colour.RED, "a": AABB(Point3D(0, 0, 0), Point3D(1, 1, 1)), "o": object})
    payload = json.loads(handler.written)
    assert payload["e"] == "red"
    assert payload["a"]["min_corner"] == {"x": 0.0, "y": 0.0, "z": 0.0}
    assert isinstance(payload["o"], str)
    assert ("Content-Length", str(len(handler.written))) in handler.headers


# ---- calibration, geometry and the configuration files ----------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"version": ""}, "version"),
        ({"source": ""}, "source"),
        ({"max_age_days": 0}, "max_age_days"),
        ({"calibrated_by": "   "}, "calibrated_by"),
    ],
)
def test_a_calibration_without_a_version_a_source_an_age_or_a_name_is_refused(kwargs: dict, message: str) -> None:
    fields = {"version": "v", "source": "s", "calibrated_at": date(2026, 1, 1), "max_age_days": 30}
    fields.update(kwargs)
    with pytest.raises(CalibrationError, match=message):
        ZoneCalibration(**fields)


@pytest.mark.parametrize("radius", [-0.1, float("nan"), float("inf")])
def test_a_sphere_test_with_a_bad_radius_is_refused_not_answered(radius: float) -> None:
    box = AABB(Point3D(0, 0, 0), Point3D(1, 1, 1))
    with pytest.raises(ValueError, match="radius"):
        box.intersects_sphere(Point3D(5, 5, 5), radius)


def _zone(**overrides) -> dict:
    zone = {"id": "z", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 1, "y": 1, "z": 1}}
    zone.update(overrides)
    return zone


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"zones": "nope"}, "zones must be a list"),
        ({"zones": []}, "must not be empty"),
        ({"zones": ["x"]}, "each zone must be an object"),
        ({"zones": [_zone(), _zone()]}, "used more than once"),
        ({"zones": [_zone(level="DANGER")]}, "level must be one of"),
        ({"zones": [{"id": "z", "level": "danger", "max": {"x": 1, "y": 1, "z": 1}}]}, "missing"),
        ({"zones": [_zone(min="here")]}, "point must be an object"),
        ({"zones": [_zone(min={"x": True, "y": 0, "z": 0})]}, "must be a number"),
        ({"zones": [_zone(min={"x": "0", "y": 0, "z": 0})]}, "must be a number"),
        ({"zones": [_zone(min={"x": float("inf"), "y": 0, "z": 0})]}, "finite"),
        ({"zones": [_zone(min={"x": 10**400, "y": 0, "z": 0})]}, "numeric"),
    ],
)
def test_every_malformed_zone_file_is_refused_with_a_reason(raw: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_zones(raw)


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"objects": {}}, "objects must be a list"),
        ({"objects": [3]}, "each object must be an object"),
        ({"objects": [{"id": "a", "position": {"x": 0, "y": 0, "z": 0}}, {"id": "a", "position": {"x": 0, "y": 0, "z": 0}}]}, "used more than once"),
        ({"objects": [{"id": "a"}]}, "missing"),
        ({"objects": [{"id": "a", "position": {"x": 0, "y": 0, "z": 0}, "radiusM": -1}]}, "a"),
    ],
)
def test_every_malformed_detection_file_is_refused_with_a_reason(raw: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_detections(raw)


@pytest.mark.parametrize("value", ["fast", None, float("nan"), float("inf"), -0.5])
def test_a_tool_velocity_that_is_not_a_finite_non_negative_number_is_refused(value: object) -> None:
    with pytest.raises(ConfigError):
        parse_tool_velocity(value)


def test_an_observation_file_that_is_not_an_object_is_refused(tmp_path) -> None:
    path = tmp_path / "observation.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON object"):
        load_observation_status(path)
