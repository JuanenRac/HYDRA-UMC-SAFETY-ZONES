# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_tool_arms.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""POST /tool-arms and GET /tool-arms: off by default, never without a credential, recorded only, old reports shown as stale."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from hydra_umc_safety_zones.api import SafetyZonesServer
from hydra_umc_safety_zones.estop import NullEStopRequester
from hydra_umc_safety_zones.tool_arms import ToolArmError, ToolArmLimitError, ToolArmRegistry, parse_tool_arm_report

TOKEN = "own-token-of-the-service-0123"


@contextmanager
def _serve(*, token=None, enabled=True, requester=None) -> Iterator[tuple[str, SafetyZonesServer]]:
    server = SafetyZonesServer(("127.0.0.1", 0), "test role", token=token, estop_requester=requester)
    if enabled:
        server.tool_arms = ToolArmRegistry(server.clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _call(url: str, path: str, body: dict | None = None, bearer: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url + path, data=data, method="POST" if body is not None else "GET")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if bearer:
        request.add_header("Authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_the_report_is_checked():
    assert parse_tool_arm_report({"robot": "arm-3", "toolId": 0, "armed": True}) == ("arm-3", 0, True)
    assert parse_tool_arm_report({"robot": "arm-3", "toolId": 24, "armed": False}) == ("arm-3", 24, False)
    for bad in ({}, {"robot": "../x", "toolId": 1, "armed": True}, {"robot": "a b", "toolId": 1, "armed": True},
                {"robot": "a", "toolId": 25, "armed": True}, {"robot": "a", "toolId": -1, "armed": True},
                {"robot": "a", "toolId": True, "armed": True}, {"robot": "a", "toolId": 1.5, "armed": True},
                {"robot": "a", "toolId": "1", "armed": True}, {"robot": "a", "toolId": 1, "armed": "yes"},
                {"robot": "a", "toolId": 1}, {"robot": 3, "toolId": 1, "armed": True}):
        with pytest.raises(ToolArmError):
            parse_tool_arm_report(bad)


def test_the_registry_keeps_the_latest_report_of_each_pair_and_has_a_limit():
    clock = _Clock()
    registry = ToolArmRegistry(clock, limit=2)
    registry.report("a", 0, True, "x")
    second = registry.report("a", 0, False, "x")
    registry.report("b", 3, True, "x")
    assert second.seq == 2
    listing = registry.listing()
    assert [(i["robot"], i["toolId"], i["armed"]) for i in listing["arms"]] == [("a", 0, False), ("b", 3, True)]
    assert listing["armedCount"] == 1 and listing["staleCount"] == 0 and listing["schema_version"] == "1.0"
    with pytest.raises(ToolArmLimitError):
        registry.report("c", 1, True, "x")
    registry.report("a", 0, True, "x")  # a known pair can always be updated


def test_an_old_report_is_stale_and_is_not_counted_as_armed():
    clock = _Clock()
    registry = ToolArmRegistry(clock, ttl_seconds=30)
    registry.report("a", 5, True, "x")
    clock.now += 30
    listing = registry.listing()
    assert listing["arms"][0]["stale"] is False and listing["armedCount"] == 1
    clock.now += 0.5
    listing = registry.listing()
    assert listing["arms"][0]["stale"] is True and listing["armedCount"] == 0 and listing["staleCount"] == 1
    assert listing["arms"][0]["armed"] is True  # the old value is shown, but marked
    with pytest.raises(ValueError):
        ToolArmRegistry(clock, ttl_seconds=0)


def test_without_the_switch_the_routes_do_not_exist():
    with _serve(token=TOKEN, enabled=False) as (url, _):
        assert _call(url, "/tool-arms", {"robot": "a", "toolId": 1, "armed": True}, TOKEN)[0] == 404
        assert _call(url, "/tool-arms", None, TOKEN)[0] == 404


def test_a_report_never_works_with_nothing_to_authenticate_it():
    with _serve() as (url, server):
        status, body = _call(url, "/tool-arms", {"robot": "a", "toolId": 1, "armed": True})
        assert status == 403 and "credential" in body["error"]
        assert server.tool_arms.listing()["arms"] == []


def test_with_the_service_token_a_report_is_recorded_and_listed_but_nothing_is_requested():
    requester = NullEStopRequester()
    with _serve(token=TOKEN, requester=requester) as (url, _):
        assert _call(url, "/tool-arms", {"robot": "arm-3", "toolId": 4, "armed": True})[0] == 401
        assert _call(url, "/tool-arms", None)[0] == 401
        status, body = _call(url, "/tool-arms", {"robot": "arm-3", "toolId": 4, "armed": True}, TOKEN)
        assert status == 200 and body == {"robot": "arm-3", "toolId": 4, "armed": True, "seq": 1}
        assert requester.sent == []
        status, listing = _call(url, "/tool-arms", None, TOKEN)
        assert status == 200 and listing["armedCount"] == 1 and listing["arms"][0]["toolId"] == 4
        assert _call(url, "/tool-arms", {"robot": "arm-3", "toolId": 99, "armed": True}, TOKEN)[0] == 400
        assert _call(url, "/tool-arms", {"robot": "arm-3", "toolId": 4, "armed": False}, TOKEN)[0] == 200
        assert _call(url, "/tool-arms", None, TOKEN)[1]["armedCount"] == 0
