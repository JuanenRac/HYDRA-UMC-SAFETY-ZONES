# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_metrics_endpoint.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""GET /metrics of a real server: the text format of Prometheus with the version and the health, the token where the service has one,
and nothing a request carried."""
from __future__ import annotations

import re
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from hydra_umc_safety_zones.api import SafetyZonesServer

TOKEN = "0123456789abcdef"
ADDR = ("127.0.0.1", 0)


@contextmanager
def serving(tmp_path: Path, token: bool = False) -> Iterator[str]:
    server = SafetyZonesServer(ADDR, "test role", **({"token": TOKEN} if token else {}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _raw(url: str, token: str | None = None) -> tuple[int, str, str]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.headers.get("Content-Type", ""), error.read().decode("utf-8")


def test_metrics_are_the_prometheus_text_with_the_version_and_the_health(tmp_path: Path) -> None:
    with serving(tmp_path) as base:
        status, kind, text = _raw(base + "/metrics")
    assert status == 200
    assert kind == "text/plain; version=0.0.4; charset=utf-8"
    assert re.search(r'^hydra_umc_service_info\{service="hydra-umc-safety-zones",version="[^"]+"\} 1$', text, re.MULTILINE)
    assert re.search(r'^hydra_umc_service_health\{service="hydra-umc-safety-zones"\} 1$', text, re.MULTILINE)
    assert text.endswith("\n")


def test_metrics_count_the_responses_by_class(tmp_path: Path) -> None:
    with serving(tmp_path) as base:
        _raw(base + "/no-such-route")
        _, _, text = _raw(base + "/metrics")
    assert re.search(r'responses_total\{service="hydra-umc-safety-zones",class="4xx"\} [1-9]', text)


def test_metrics_carry_nothing_a_request_carried(tmp_path: Path) -> None:
    with serving(tmp_path) as base:
        _raw(base + "/no-such-route?name=very-private-name")
        _, _, text = _raw(base + "/metrics?token=leaked-secret")
    for needle in ("very-private-name", "leaked-secret", "no-such-route", "127.0.0.1"):
        assert needle not in text


def test_metrics_need_the_token_when_the_service_has_one(tmp_path: Path) -> None:
    with serving(tmp_path, token=True) as base:
        refused = _raw(base + "/metrics")
        wrong = _raw(base + "/metrics", "not-the-token")
        allowed = _raw(base + "/metrics", TOKEN)
    assert refused[0] == 401 and wrong[0] == 401
    assert "hydra_umc" not in refused[2]
    assert allowed[0] == 200 and "hydra_umc_service_info" in allowed[2]
