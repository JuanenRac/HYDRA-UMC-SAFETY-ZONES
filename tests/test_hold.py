# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_hold.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""POST /hold and GET /holds: off by default, never without a credential, recorded only unless the operator also asks for a stop request."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from hydra_umc_safety_zones import server_identity
from hydra_umc_safety_zones.api import SafetyZonesServer
from hydra_umc_safety_zones.estop import NullEStopRequester
from hydra_umc_safety_zones.hold import HoldError, HoldRegistry, parse_hold_request

KEY = b"a-signing-key-of-the-server-0123456789"
TOKEN = "own-token-of-the-service-0123"


@contextmanager
def _serve(*, token=None, key=None, holds=True, actuate=False, requester=None) -> Iterator[tuple[str, SafetyZonesServer]]:
    server = SafetyZonesServer(("127.0.0.1", 0), "test role", token=token, estop_requester=requester)
    if key is not None:
        server.identity_key = key
    if holds:
        server.holds = HoldRegistry()
        server.hold_actuate = actuate
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


def test_the_request_is_checked():
    assert parse_hold_request({"robot": "arm-3", "reason": "node lost"}) == ("arm-3", "node lost", False)
    assert parse_hold_request({"robot": "arm-3", "release": True}) == ("arm-3", "", True)
    for bad in ({}, {"robot": "../x", "reason": "r"}, {"robot": "a b", "reason": "r"}, {"robot": "a", "reason": ""},
                {"robot": "a", "reason": "x" * 201}, {"robot": "a", "reason": "r", "release": "yes"}, {"robot": 3, "reason": "r"}):
        with pytest.raises(HoldError):
            parse_hold_request(bad)


def test_the_registry_keeps_the_latest_hold_of_each_robot_and_has_a_limit():
    registry = HoldRegistry(limit=2)
    registry.hold("a", "one", "x")
    registry.hold("a", "two", "x")
    registry.hold("b", "three", "x")
    assert [h["reason"] for h in registry.listing()] == ["two", "three"]
    with pytest.raises(HoldError):
        registry.hold("c", "four", "x")
    assert registry.release("a") is True
    assert registry.release("a") is False


def test_without_the_switch_the_routes_do_not_exist():
    with _serve(token=TOKEN, holds=False) as (url, _):
        assert _call(url, "/hold", {"robot": "a", "reason": "r"}, TOKEN)[0] == 404
        assert _call(url, "/holds", None, TOKEN)[0] == 404


def test_a_hold_never_works_with_nothing_to_authenticate_it():
    with _serve() as (url, server):
        status, body = _call(url, "/hold", {"robot": "a", "reason": "r"})
        assert status == 403 and "credential" in body["error"]
        assert server.holds.listing() == []


def test_with_the_service_token_a_hold_is_recorded_and_listed_but_nothing_is_requested():
    requester = NullEStopRequester()
    with _serve(token=TOKEN, requester=requester) as (url, _):
        assert _call(url, "/hold", {"robot": "arm-3", "reason": "node lost"})[0] == 401
        status, body = _call(url, "/hold", {"robot": "arm-3", "reason": "node lost"}, TOKEN)
        assert status == 200 and body["held"] is True and body["actuated"] is False
        assert requester.sent == []
        status, listing = _call(url, "/holds", None, TOKEN)
        assert status == 200 and [h["robot"] for h in listing["holds"]] == ["arm-3"] and listing["actuate"] is False
        assert _call(url, "/hold", {"robot": "arm-3", "release": True}, TOKEN)[1]["released"] is True
        assert _call(url, "/holds", None, TOKEN)[1]["holds"] == []


def test_with_the_identity_of_the_server_a_viewer_cannot_hold_and_an_operator_can():
    viewer = server_identity.sign_token(KEY, "reader", "viewer")
    operator = server_identity.sign_token(KEY, "node-healing", "operator")
    with _serve(key=KEY) as (url, _):
        assert _call(url, "/hold", {"robot": "a", "reason": "r"})[0] == 401
        assert _call(url, "/hold", {"robot": "a", "reason": "r"}, viewer)[0] == 401
        status, body = _call(url, "/hold", {"robot": "a", "reason": "r"}, operator)
        assert status == 200 and body["held"] is True
        listing = _call(url, "/holds", None, viewer)[1]["holds"]
        assert listing[0]["by"] == "node-healing"


def test_an_invalid_body_is_a_400_and_records_nothing():
    with _serve(token=TOKEN) as (url, server):
        assert _call(url, "/hold", {"robot": "a"}, TOKEN)[0] == 400
        assert server.holds.listing() == []


def test_only_with_the_second_switch_does_a_hold_hand_a_stop_request_to_the_requester():
    requester = NullEStopRequester()
    with _serve(token=TOKEN, actuate=True, requester=requester) as (url, _):
        status, body = _call(url, "/hold", {"robot": "arm-3", "reason": "node lost"}, TOKEN)
        assert status == 200 and body["actuated"] is True
        assert [r.object_id for r in requester.sent] == ["arm-3"]
    other = NullEStopRequester()
    with _serve(token=TOKEN, actuate=True, requester=None) as (url, _):
        assert _call(url, "/hold", {"robot": "arm-3", "reason": "r"}, TOKEN)[1]["actuated"] is False
    assert other.sent == []
