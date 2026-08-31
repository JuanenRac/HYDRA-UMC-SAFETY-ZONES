# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/api.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Plain JSON/HTTP surface (stdlib http.server) - same convention as this
family's other api.py files. POST /check reaches the exact same
`evaluate_safety()`/`check_breaches()`/`request_estop_for()` functions the
CLI's own `check` subcommand already runs - `zones`/`detections` travel
directly in the JSON body (config.py's own `parse_zone_set()`/
`parse_detections()`, split out for exactly this reason) rather than a
server-side file path, which only ever made sense for a CLI running on
the same machine as the file.

This never asserts an E-STOP itself, only ever requests one
(`NullEStopRequester`, same as the CLI) - a real transport still has to
be wired to actually assert it (see estop.py's own module docstring).
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .breach import check_breaches
from .config import ConfigError, parse_detections, parse_zone_set
from .estop import NullEStopRequester, request_estop_for
from .safety_state import SafetyState, evaluate_safety


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    def default(o: object) -> object:
        if hasattr(o, "__dataclass_fields__"):
            return asdict(o)
        if hasattr(o, "value"):  # enum
            return o.value
        return str(o)
    body = json.dumps(payload, default=default).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _write_error(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    _write_json(handler, status, {"error": message})


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    return json.loads(raw)


class Handler(BaseHTTPRequestHandler):
    server: "SafetyZonesServer"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # quiet by default, same reasoning as this family's other api.py files

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/stats":
            _write_json(self, 200, {"role": self.server.role})
        else:
            _write_error(self, 404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = _read_json_body(self)
        except json.JSONDecodeError as e:
            _write_error(self, 400, f"malformed JSON body: {e}")
            return
        if path == "/check":
            self._handle_check(body)
        else:
            _write_error(self, 404, "not found")

    def _handle_check(self, body: dict) -> None:
        try:
            zone_set = parse_zone_set(body["zones"])
            objects = parse_detections(body["detections"])
        except KeyError as e:
            _write_error(self, 400, f"missing required field: {e}")
            return
        except (ConfigError, KeyError, TypeError, ValueError) as e:
            _write_error(self, 400, f"invalid safety configuration: {e}")
            return

        today = datetime.now(timezone.utc).date()
        evaluation = evaluate_safety(zone_set, objects, today)

        response: dict = {"state": evaluation.state.value, "reason": evaluation.reason, "breaches": [], "estopRequests": []}

        if evaluation.state in (SafetyState.INHIBITED, SafetyState.READY):
            _write_json(self, 200, response)
            return

        breaches = check_breaches(zone_set.zones, objects)
        response["breaches"] = [asdict(b) for b in breaches]

        if evaluation.state is SafetyState.WARNING:
            _write_json(self, 200, response)
            return

        requester = NullEStopRequester()
        requests = request_estop_for(breaches, requester)
        response["estopRequests"] = [asdict(r) for r in requests]
        _write_json(self, 200, response)


class SafetyZonesServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], role: str) -> None:
        super().__init__(address, Handler)
        self.role = role
