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

import hmac
import json
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .breach import check_breaches
from .config import ConfigError, parse_detections, parse_tool_velocity, parse_zone_set, strict_json_loads
from .estop import NullEStopRequester, request_estop_for
from .observation import ObservationError, parse_observation_status
from .safety_state import SafetyEvaluation, SafetyState, evaluate_safety, to_sdk_safety_state
from .zones import ZoneSet, scale_zones_for_velocity

# Connections served at the same time. A client that opens many connections
# and sends nothing must not be able to use up the threads and the memory of
# a service other things wait on to move.
MAX_CONNECTIONS = 32


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


MAX_BODY_BYTES = 1024 * 1024
# How much of an oversized body is drained before responding: rejecting an
# over-limit request without reading any of it leaves the client's own send()
# still in flight when the connection closes, so on a body bigger than the OS
# socket buffer the client saw a raw ConnectionAbortedError instead of the
# clean 400. The drain is read in blocks of DRAIN_BLOCK_BYTES, so it never
# holds more than one block in memory, and gives up at DRAIN_CAP_BYTES.
DRAIN_CAP_BYTES = MAX_BODY_BYTES * 16
DRAIN_BLOCK_BYTES = 64 * 1024


def _drain(handler: BaseHTTPRequestHandler, length: int) -> None:
    remaining = length
    while remaining > 0:
        chunk = handler.rfile.read(min(DRAIN_BLOCK_BYTES, remaining))
        if not chunk:
            return
        remaining -= len(chunk)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", 0))
    except ValueError as error:
        raise ValueError("Content-Length must be an integer") from error
    if length < 0 or length > MAX_BODY_BYTES:
        if 0 <= length <= DRAIN_CAP_BYTES:
            _drain(handler, length)
        raise ValueError(f"request body must contain 0-{MAX_BODY_BYTES} bytes")
    raw = handler.rfile.read(length) if length else b"{}"
    # NaN, Infinity and -Infinity are refused anywhere in the body (Python's
    # json accepts them by default): a NaN where a number belongs switches a
    # check off.
    body = strict_json_loads(raw)
    if not isinstance(body, dict):
        raise ValueError("the body must be a JSON object")
    return body


class Handler(BaseHTTPRequestHandler):
    server: "SafetyZonesServer"

    # Seconds a client may stay silent on its connection before the server
    # drops it: without it a client that announces a body and sends nothing
    # holds a thread for ever.
    timeout = 10.0

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # quiet by default, same reasoning as this family's other api.py files

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/stats":
            _write_json(self, 200, {"role": self.server.role})
        else:
            _write_error(self, 404, "not found")

    def _authorized(self) -> bool:
        token = self.server.token
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[len("Bearer "):].strip().encode("utf-8"), token.encode("utf-8"))

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/check" and not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Bearer")
            body = b'{"error": "a valid Authorization: Bearer token is required"}'
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            body = _read_json_body(self)
        except (json.JSONDecodeError, ValueError) as e:
            _write_error(self, 400, f"malformed JSON body: {e}")
            return
        if path != "/check":
            _write_error(self, 404, "not found")
            return
        try:
            self._handle_check(body)
        except Exception as e:  # noqa: BLE001 - never a closed socket with no state
            # The caller must get a verdict it cannot misread as "all clear":
            # an explicit FAULT, with the cell treated as not verified.
            evaluation = SafetyEvaluation(SafetyState.INHIBITED, f"internal error: {type(e).__name__}", fault=True)
            try:
                _write_json(
                    self,
                    500,
                    {
                        "error": f"internal error: {type(e).__name__}",
                        "state": evaluation.state.value,
                        "reason": evaluation.reason,
                        "breaches": [],
                        "estopRequests": [],
                        "sdkSafetyState": to_sdk_safety_state(evaluation),
                    },
                )
            except OSError:
                pass

    def _handle_check(self, body: dict) -> None:
        try:
            if self.server.zone_set is not None:
                # Authoritative mode: the zones and calibration are the
                # server's own. A request that brings its own is refused,
                # not silently ignored, so nobody believes they were used.
                if "zones" in body:
                    _write_error(self, 400, "this server holds its own zones (--zones-file): a request must not send zones")
                    return
                zone_set = self.server.zone_set
            else:
                zone_set = parse_zone_set(body["zones"])
            objects = parse_detections(body["detections"])
            # an "observation" key at all is optional in the request
            # body (a caller not yet updated to send it must not get a
            # hard 400 for a field that didn't exist before this fix) -
            # but omitting it always resolves to INHIBITED, never a
            # silent READY, via evaluate_safety()'s own fail-safe default.
            observation_raw = body.get("observation")
            observation = parse_observation_status(observation_raw) if observation_raw is not None else None
            # Optional real head/tool velocity (meters/second) - an
            # additional, opt-in envelope-scaling input alongside
            # `observation` above, with the exact same fail-safe shape: a
            # caller that never sends it (or an older caller not yet
            # updated to) gets today's static zone behavior, unchanged.
            velocity_raw = body.get("toolVelocityMps")
            tool_velocity_mps = parse_tool_velocity(velocity_raw) if velocity_raw is not None else None
        except KeyError as e:
            _write_error(self, 400, f"missing required field: {e}")
            return
        except (ConfigError, ObservationError, TypeError, ValueError) as e:
            _write_error(self, 400, f"invalid safety configuration: {e}")
            return

        limit_error = self._limit_violation(zone_set, observation)
        if limit_error:
            _write_error(self, 400, limit_error)
            return

        today = datetime.now(timezone.utc).date()
        evaluation = evaluate_safety(zone_set, objects, today, observation, tool_velocity_mps=tool_velocity_mps)

        response: dict = {
            "state": evaluation.state.value,
            "reason": evaluation.reason,
            "breaches": [],
            "estopRequests": [],
            # Real HYDRA-UMC-SDK-conformant SafetyState (contracts/
            # json-schema/v1/safety-state.schema.json) alongside this
            # module's own internal `state` above - a real consumer like
            # HYDRA-UMC-VISUAL-SERVOING-API's authorize_correction() reads
            # THIS field, never the internal one (different vocabulary,
            # different member set - see to_sdk_safety_state()'s own
            # header comment for why both must keep existing side by side).
            "sdkSafetyState": to_sdk_safety_state(evaluation),
        }
        if self.server.config_sha256:
            # which configuration this verdict was reached with
            response["configSha256"] = self.server.config_sha256

        if evaluation.state in (SafetyState.INHIBITED, SafetyState.READY):
            _write_json(self, 200, response)
            return

        # Same scaled zones evaluate_safety() itself just breach-checked
        # against internally - recomputing here with the unscaled
        # zone_set.zones would report a DANGER/WARNING state alongside a
        # `breaches` list that doesn't actually explain it.
        breaches = check_breaches(scale_zones_for_velocity(zone_set.zones, tool_velocity_mps), objects)
        response["breaches"] = [asdict(b) for b in breaches]

        if evaluation.state is SafetyState.WARNING:
            _write_json(self, 200, response)
            return

        requester = NullEStopRequester()
        requests = request_estop_for(breaches, requester)
        response["estopRequests"] = [asdict(r) for r in requests]
        _write_json(self, 200, response)


    def _limit_violation(self, zone_set: ZoneSet, observation: object) -> str | None:
        """The server's own ceilings on what a request may claim about how
        long its evidence stays valid, or None when the request is within them."""
        cap_seconds = self.server.max_observation_age_seconds
        max_age = getattr(observation, "max_age_seconds", None)
        if cap_seconds is not None and max_age is not None and max_age > cap_seconds:
            return f"observation.maxAgeSeconds {max_age} exceeds the {cap_seconds} this server accepts"
        cap_days = self.server.max_calibration_age_days
        calibration = zone_set.calibration
        if cap_days is not None and calibration is not None and calibration.max_age_days > cap_days:
            return f"calibration.max_age_days {calibration.max_age_days} exceeds the {cap_days} this server accepts"
        return None


class SafetyZonesServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        role: str,
        *,
        token: str | None = None,
        zone_set: ZoneSet | None = None,
        config_sha256: str | None = None,
        max_observation_age_seconds: float | None = None,
        max_calibration_age_days: int | None = None,
    ) -> None:
        super().__init__(address, Handler)
        self.role = role
        self.token = token or None
        self.zone_set = zone_set
        self.config_sha256 = config_sha256
        self.max_observation_age_seconds = max_observation_age_seconds
        self.max_calibration_age_days = max_calibration_age_days
        self._connections = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address) -> None:  # type: ignore[no-untyped-def]
        if not self._connections.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address) -> None:  # type: ignore[no-untyped-def]
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()
