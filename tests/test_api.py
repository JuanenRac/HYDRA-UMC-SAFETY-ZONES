# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_api.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Real end-to-end HTTP tests: a real SafetyZonesServer (ThreadingHTTPServer)
hit with real urllib requests - same convention and fixture shapes as this
repo's own tests/test_cli.py."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from hydra_umc_safety_zones.api import SafetyZonesServer


def _today_str():
    return datetime.now(timezone.utc).date().isoformat()


def _zones(calibration="valid"):
    payload = {
        "zones": [
            {"id": "warn1", "level": "warning", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 10, "y": 10, "z": 10}},
            {"id": "danger1", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 2, "y": 2, "z": 2}},
        ]
    }
    if calibration == "valid":
        payload["calibration"] = {"version": "cal-1", "source": "manual", "calibrated_at": _today_str(), "max_age_days": 30}
    elif calibration == "expired":
        payload["calibration"] = {"version": "cal-0", "source": "manual", "calibrated_at": "2020-01-01", "max_age_days": 30}
    elif calibration != "missing":
        raise ValueError(f"unknown calibration fixture kind: {calibration!r}")
    return payload


def _detections(x, y, z):
    return {"objects": [{"id": "op1", "position": {"x": x, "y": y, "z": z}}]}


def _post(url: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@contextmanager
def running_server() -> Iterator[str]:
    server = SafetyZonesServer(("127.0.0.1", 0), "test role")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_check_no_breach_is_ready(tmp_path) -> None:
    with running_server() as base:
        status, body = _post(f"{base}/check", {"zones": _zones(), "detections": _detections(50, 50, 50)})
        assert status == 200
        assert body["state"] == "ready"
        assert body["breaches"] == []


def test_check_warning_breach(tmp_path) -> None:
    with running_server() as base:
        status, body = _post(f"{base}/check", {"zones": _zones(), "detections": _detections(5, 5, 5)})
        assert status == 200
        assert body["state"] == "warning"
        assert len(body["breaches"]) == 1
        assert body["breaches"][0]["zone_id"] == "warn1"
        assert body["estopRequests"] == []


def test_check_danger_breach_requests_estop(tmp_path) -> None:
    with running_server() as base:
        status, body = _post(f"{base}/check", {"zones": _zones(), "detections": _detections(1, 1, 1)})
        assert status == 200
        assert body["state"] == "danger"
        assert len(body["breaches"]) >= 1
        assert len(body["estopRequests"]) >= 1


def test_check_missing_calibration_inhibits(tmp_path) -> None:
    with running_server() as base:
        status, body = _post(f"{base}/check", {"zones": _zones(calibration="missing"), "detections": _detections(500, 500, 500)})
        assert status == 200
        assert body["state"] == "inhibited"
        assert body["breaches"] == []


def test_check_expired_calibration_inhibits_even_inside_danger_zone(tmp_path) -> None:
    with running_server() as base:
        status, body = _post(f"{base}/check", {"zones": _zones(calibration="expired"), "detections": _detections(1, 1, 1)})
        assert status == 200
        assert body["state"] == "inhibited"
        assert body["breaches"] == []


def test_check_invalid_coordinate_returns_400(tmp_path) -> None:
    with running_server() as base:
        status, body = _post(f"{base}/check", {"zones": _zones(), "detections": _detections("NaN", 1, 1)})
        assert status == 400


def test_check_missing_field(tmp_path) -> None:
    with running_server() as base:
        status, body = _post(f"{base}/check", {"zones": _zones()})
        assert status == 400


def test_stats() -> None:
    with running_server() as base:
        status, body = _get(f"{base}/stats")
        assert status == 200
        assert body == {"role": "test role"}


def test_not_found() -> None:
    with running_server() as base:
        status, body = _get(f"{base}/nope")
        assert status == 404
