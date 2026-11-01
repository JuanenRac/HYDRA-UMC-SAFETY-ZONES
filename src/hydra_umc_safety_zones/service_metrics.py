# =============================================================================
# HYDRA-UMC-SDK - tools/canonical/service_metrics.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The `GET /metrics` of a service that answers with the standard library HTTP server, written once.

This file has a single canonical copy (`tools/canonical/service_metrics.py` of HYDRA-UMC-SDK) that is vendored, byte for byte, into the Python services;
`tools/sync_repo_tools.py` keeps the copies identical. A service adopts it in three lines:

    class Handler(MetricsHandlerMixin, BaseHTTPRequestHandler):
        metrics_service = "hydra-umc-example"
        metrics_version = __version__     # optional: without it the installed version of the distribution is published

        def do_GET(self):
            if self.serve_metrics(authorized=self._authorized()):   # the answer to /metrics is the first thing a GET does
                return

It publishes the text format of Prometheus (version 0.0.4 of the exposition format) and only numbers about the service: its version, how long it has been up,
how many responses it gave in each class of status (2xx, 3xx, 4xx, 5xx) and its health, the aggregate of the checks the service declares (`metrics_checks`, a
dictionary of check name to whether it passes; no checks means the process answers, so healthy). Nothing a request carried is ever a label or a value: no path, no
address, no token, no body. `authorized` says whether the caller may read the numbers; a service with a token gives the same answer it gives to its most
sensitive read, and a service with none publishes them like its `/stats`.
"""
from __future__ import annotations

import re
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler
from importlib import metadata

STATUS_CLASSES = ("2xx", "3xx", "4xx", "5xx")
_CHECK_NAME = re.compile(r"[^a-z0-9_]+")
_STARTED = time.monotonic()
_CREATE = threading.Lock()


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def installed_version(distribution: str) -> str:
    """The version the package manager knows for a distribution, or "unknown" when it is run from a source tree."""
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return "unknown"


def check_name(name: str) -> str:
    """A check name as a label value: lower case letters, digits and underscores, so a check can never carry free text."""
    return _CHECK_NAME.sub("_", name.lower()).strip("_") or "check"


class ServiceMetrics:
    """The counters of one service; safe to use from the threads of the HTTP server."""

    def __init__(self, service: str, version: str) -> None:
        self.service = service
        self.version = version
        self._lock = threading.Lock()
        self._responses: Counter[str] = Counter()

    def count(self, status: int) -> None:
        klass = f"{status // 100}xx"
        with self._lock:
            self._responses[klass if klass in STATUS_CLASSES else "5xx"] += 1

    def render(self, checks: dict[str, bool] | None = None) -> str:
        with self._lock:
            responses = dict(self._responses)
        service = _label(self.service)
        results = {check_name(name): bool(ok) for name, ok in (checks or {}).items()}
        lines = [
            "# HELP hydra_umc_service_info The version of the running service.",
            "# TYPE hydra_umc_service_info gauge",
            f'hydra_umc_service_info{{service="{service}",version="{_label(self.version)}"}} 1',
            "# HELP hydra_umc_service_uptime_seconds Seconds since the process started.",
            "# TYPE hydra_umc_service_uptime_seconds gauge",
            f'hydra_umc_service_uptime_seconds{{service="{service}"}} {int(time.monotonic() - _STARTED)}',
            "# HELP hydra_umc_service_http_responses_total Responses given, by the class of the status.",
            "# TYPE hydra_umc_service_http_responses_total counter",
        ]
        lines += [f'hydra_umc_service_http_responses_total{{service="{service}",class="{klass}"}} {responses.get(klass, 0)}' for klass in STATUS_CLASSES]
        lines += [
            "# HELP hydra_umc_service_health 1 when every check of the service passes (or it declares none), 0 when one fails.",
            "# TYPE hydra_umc_service_health gauge",
            f'hydra_umc_service_health{{service="{service}"}} {int(all(results.values()))}',
        ]
        if results:
            lines += [
                "# HELP hydra_umc_service_health_check 1 when one check of the service passes.",
                "# TYPE hydra_umc_service_health_check gauge",
            ]
            lines += [f'hydra_umc_service_health_check{{service="{service}",check="{name}"}} {int(ok)}' for name, ok in sorted(results.items())]
        return "\n".join(lines) + "\n"


class MetricsHandlerMixin(BaseHTTPRequestHandler):
    """Counts every response and answers `GET /metrics`; put it before `BaseHTTPRequestHandler` in the bases of the handler."""

    metrics_service = "hydra-umc-service"
    metrics_version = ""  # the version to publish; empty means the one installed under the name of the service

    def metrics_checks(self) -> dict[str, bool]:
        """The checks that make up the health of the service; a service with checks of its own overrides this."""
        return {}

    def _service_metrics(self) -> ServiceMetrics:
        existing = getattr(self.server, "service_metrics", None)
        if isinstance(existing, ServiceMetrics):
            return existing
        with _CREATE:
            existing = getattr(self.server, "service_metrics", None)
            if not isinstance(existing, ServiceMetrics):
                existing = ServiceMetrics(self.metrics_service, self.metrics_version or installed_version(self.metrics_service))
                self.server.service_metrics = existing  # type: ignore[attr-defined]
            return existing

    def send_response(self, code: int, message: str | None = None) -> None:
        self._service_metrics().count(code)
        super().send_response(code, message)

    def serve_metrics(self, authorized: bool = True) -> bool:
        """Answer `GET /metrics` and return True; return False for any other path so the handler carries on."""
        if self.path.split("?", 1)[0] != "/metrics":
            return False
        if not authorized:
            body = b'{"error": "unauthorized"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("WWW-Authenticate", "Bearer")
        else:
            try:
                checks = self.metrics_checks()
            except Exception:  # a check that cannot be made is a failed check, never a failed answer
                checks = {"checks": False}
            body = self._service_metrics().render(checks).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True
