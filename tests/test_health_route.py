# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_health_route.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""GET /health of a real server: closed by default, open only when the operator opens it, loopback or the token, and a body without secrets."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from hydra_umc_safety_zones import health_route
from hydra_umc_safety_zones.health_route import health_open_requested

from hydra_umc_safety_zones.api import SafetyZonesServer

TOKEN = '0123456789abcdef'


def make_server(token: str = ""):
    return SafetyZonesServer(('127.0.0.1', 0), 'test role', token=token)



def _get(base: str, token: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(base + "/health")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.loads(error.read())


def _running(health_open: bool):
    server = make_server()
    server.health_open = health_open
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def _stop(server, thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_health_is_closed_by_default() -> None:
    server, thread, base = _running(False)
    try:
        status, body = _get(base)
    finally:
        _stop(server, thread)
    assert status == 404
    assert "status" not in body


def test_health_answers_on_loopback_once_opened_and_carries_no_secret() -> None:
    server, thread, base = _running(True)
    try:
        status, body = _get(base)
    finally:
        _stop(server, thread)
    assert status == 200
    assert body["status"] == "ok"
    assert set(body) == {"status", "service", "version"}
    assert body["service"].startswith("hydra-umc-")


def test_health_from_outside_loopback_needs_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_route, "_is_loopback", lambda address: False)
    server, thread, base = _running(True)
    try:
        status_without, _ = _get(base)
        status_wrong, _ = _get(base, "not-the-token")
    finally:
        _stop(server, thread)
    assert status_without == 401
    assert status_wrong == 401


def test_health_open_comes_from_the_flag_or_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(health_route.ENV_HEALTH_OPEN, raising=False)
    assert health_open_requested(False) is False
    assert health_open_requested(True) is True
    monkeypatch.setenv(health_route.ENV_HEALTH_OPEN, "1")
    assert health_open_requested(False) is True
    monkeypatch.setenv(health_route.ENV_HEALTH_OPEN, "no")
    assert health_open_requested(False) is False


def test_health_from_outside_loopback_is_answered_for_the_right_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_route, "_is_loopback", lambda address: False)
    server = make_server(TOKEN)
    server.health_open = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status_ok, body = _get(base, TOKEN)
        status_no, _ = _get(base)
    finally:
        _stop(server, thread)
    assert (status_ok, status_no) == (200, 401)
    assert TOKEN not in json.dumps(body)
