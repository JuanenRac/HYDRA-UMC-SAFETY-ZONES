# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_service_metrics_edges.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The edges of the shared metrics text: labels that must never carry free text, the escaping of a service name, the health of the service when a check fails
or cannot be made, and the answer of `/metrics` to a request without the token."""
from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from hydra_umc_safety_zones.service_metrics import MetricsHandlerMixin, ServiceMetrics, check_name, installed_version


def test_check_names_can_never_carry_free_text_into_a_label():
    assert check_name("Data Lake/OK") == "data_lake_ok"
    assert check_name('"}\n injected{') == "injected"
    assert check_name("___") == "check"
    assert check_name("") == "check"


def test_the_installed_version_of_something_unknown_is_unknown():
    assert installed_version("a-distribution-that-does-not-exist-hyt2") == "unknown"
    assert installed_version("pytest") not in ("", "unknown")


def test_the_metrics_text_escapes_the_labels_and_says_unhealthy_when_a_check_fails():
    text = ServiceMetrics('svc"x\\y\nz', "1.0").render({"datalake": True, "broker": False})
    assert 'svc\\"x\\\\y z' in text
    assert 'hydra_umc_service_health{service="svc\\"x\\\\y z"} 0' in text
    assert text.count("check=") == 2 and 'check="broker"} 0' in text and 'check="datalake"} 1' in text
    assert ServiceMetrics("svc", "1.0").render({}).count("hydra_umc_service_health_check") == 0
    assert 'hydra_umc_service_health{service="svc"} 1' in ServiceMetrics("svc", "1.0").render({})


class _Service(MetricsHandlerMixin):
    metrics_service = "hyt2-service"
    metrics_version = "9.9.9"
    checks: object = {"disk": True}
    allowed = True

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def metrics_checks(self) -> dict[str, bool]:
        if isinstance(self.checks, Exception):
            raise self.checks
        return dict(self.checks)  # type: ignore[call-overload]

    def do_GET(self) -> None:  # noqa: N802
        if self.serve_metrics(authorized=type(self).allowed):
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _get(base: str, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(base + path, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


@pytest.fixture
def service():
    handler = type("Handler", (_Service,), {"checks": {"disk": True}, "allowed": True})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", handler
    finally:
        server.shutdown()
        server.server_close()


def test_metrics_counts_every_answer_and_other_paths_carry_on(service):
    base, _ = service
    assert _get(base, "/other")[0] == 404
    status, text = _get(base, "/metrics?x=1")
    assert status == 200 and 'version="9.9.9"' in text and 'check="disk"} 1' in text
    assert 'class="4xx"} 1' in text and 'class="2xx"}' in text


def test_a_status_that_is_not_in_a_known_class_is_counted_as_a_server_error():
    metrics = ServiceMetrics("svc", "1")
    for status in (200, 204, 404, 503, 999, 99):
        metrics.count(status)
    text = metrics.render()
    assert 'class="2xx"} 2' in text and 'class="4xx"} 1' in text and 'class="5xx"} 3' in text


def test_a_request_without_the_token_gets_401_and_no_numbers(service):
    base, handler = service
    handler.allowed = False
    status, body = _get(base, "/metrics")
    assert status == 401 and "unauthorized" in body and "hydra_umc_service_health" not in body


def test_a_check_that_cannot_be_made_is_a_failed_check_and_not_a_failed_answer(service):
    base, handler = service
    handler.checks = RuntimeError("the disk is gone")
    status, text = _get(base, "/metrics")
    assert status == 200 and 'check="checks"} 0' in text and "} 0" in text
