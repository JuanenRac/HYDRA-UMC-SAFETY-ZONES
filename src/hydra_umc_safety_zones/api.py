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
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .estop import EStopRequest
from .health_route import HealthRouteMixin
from .hold import HoldDenied, HoldError, HoldRegistry, parse_hold_request
from .tool_arms import ToolArmError, ToolArmLimitError, ToolArmRegistry, parse_tool_arm_report
from .server_identity import accepts
from .service_metrics import MetricsHandlerMixin
from .config import ConfigError, parse_detections, parse_robot_body, parse_tool_velocity, parse_zone_set, strict_json_loads
from .armor_alarm import ArmorAlarmGate, combine
from .droid_estop import DroidEstopConfigError, DroidEstopGate, DroidSwitch
from .estop import EStopRequester, NullEStopRequester, request_estop_for
from .signing import Signer
from .observation import ObservationError, parse_observation_status
from .robot_body import RobotBody, RobotBodyError
from .safety_state import SafetyEvaluation, SafetyState, collect_breaches, evaluate_safety, to_sdk_safety_state
from .fault_events import FaultEventEmitter
from .cell_state_file import CellStateWriter
from .record_trigger import RecordTriggerClient
from .zone_occupancy import ZoneOccupancyEmitter
from .stateful import StatefulMonitor, StatefulSettings, format_event
from .zones import ZoneSet

# Connections served at the same time. A client that opens many connections
# and sends nothing must not be able to use up the threads and the memory of
# a service other things wait on to move.
MAX_CONNECTIONS = 32


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    def default(o: object) -> object:
        if hasattr(o, "__dataclass_fields__"):
            return asdict(o)  # type: ignore[call-overload]
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


class Handler(HealthRouteMixin, MetricsHandlerMixin, BaseHTTPRequestHandler):
    server: "SafetyZonesServer"
    metrics_service = "hydra-umc-safety-zones"

    # Seconds a client may stay silent on its connection before the server
    # drops it: without it a client that announces a body and sends nothing
    # holds a thread for ever.
    timeout = 10.0

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # quiet by default, same reasoning as this family's other api.py files

    def do_GET(self) -> None:  # noqa: N802
        if self.serve_health(self.server.token):
            return
        if self.serve_metrics(authorized=self._authorized()):
            return
        path = urlparse(self.path).path
        if path == "/stats" and self.server.identity_key is not None and not self._authorized():
            self._write_unauthorized()  # with the identity of the SERVER on, not even the health path is open
            return
        if path == "/stats":
            _write_json(self, 200, {"role": self.server.role})
        elif path == "/state":
            if not self._authorized():
                self._write_unauthorized()
                return
            verdict = self.server.last_verdict()
            if verdict is None:
                # No verdict yet: a consumer must read this as "not confirmed safe", never as READY.
                _write_json(self, 404, {"error": "no safety verdict has been reached yet"})
            else:
                _write_json(self, 200, verdict)
        elif path == "/armor-alarm" and self.server.armor_gate is not None:
            if not self._authorized():
                self._write_unauthorized()
                return
            gate = self.server.armor_gate
            payload = gate.status(self.server.clock()).as_dict()
            payload["audit"] = list(gate.audit_records[-50:])
            _write_json(self, 200, payload)
        elif path == "/settings/droid-estop" and self.server.droid_switch is not None:
            if not self._authorized():
                self._write_unauthorized()
                return
            _write_json(self, 200, self._droid_switch_view())
        elif path == "/droid-estop" and self.server.droid_gate is not None:
            if not self._authorized():
                self._write_unauthorized()
                return
            droid_gate = self.server.droid_gate
            droid_payload = droid_gate.status(self.server.clock()).as_dict()
            droid_payload["audit"] = list(droid_gate.audit_records[-50:])
            _write_json(self, 200, droid_payload)
        elif path == "/holds" and self.server.holds is not None:
            if not self._authorized():
                self._write_unauthorized()
                return
            _write_json(self, 200, {"holds": self.server.holds.listing(), "actuate": self.server.hold_actuate})
        elif path == "/tool-arms" and self.server.tool_arms is not None:
            if not self._authorized():
                self._write_unauthorized()
                return
            _write_json(self, 200, self.server.tool_arms.listing())
        elif path == "/events" and self.server.monitor is not None:
            if not self._authorized():
                self._write_unauthorized()
                return
            self._stream_events()
        else:
            _write_error(self, 404, "not found")

    def _stream_events(self) -> None:
        """Server-Sent Events: the held state first, then every change and every expiry; a comment line when nothing happens."""
        monitor = self.server.monitor
        assert monitor is not None
        subscriber = monitor.subscribe()
        if subscriber is None:
            _write_error(self, 503, "too many open streams")
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(format_event(monitor.current_event(self.server.clock())))
            self.wfile.flush()
            while not self.server.stopping.is_set():
                try:
                    payload = subscriber.events.get(timeout=monitor.settings.keepalive_s)
                    self.wfile.write(format_event(payload))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except OSError:
            pass  # the consumer went away
        finally:
            monitor.unsubscribe(subscriber)

    def _authorized(self) -> bool:
        token = self.server.token
        if self.server.identity_key is not None:
            return accepts(self, token or "", self.server.identity_key)  # the identity of HYDRA-UMC-SERVER is on: nothing is open
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[len("Bearer "):].strip().encode("utf-8"), token.encode("utf-8"))

    def _observer_authorized(self) -> bool:
        """Who may send what the service judges (``/check``, ``/heartbeat``) once an observer token is set: that token, or an administrator of the server. The token of the service no longer may."""
        observer = self.server.observer_token
        assert observer
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and hmac.compare_digest(header[len("Bearer "):].strip().encode("utf-8"), observer.encode("utf-8")):
            return True
        if self.server.identity_key is None or not accepts(self, "", self.server.identity_key):
            return False
        identity = getattr(self, "identity", None)
        return identity is not None and identity.role == "admin"

    def _write_unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", "Bearer")
        body = b'{"error": "a valid Authorization: Bearer token is required"}'
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if self.server.observer_token and path in ("/check", "/heartbeat"):
            if not self._observer_authorized():
                self._write_unauthorized()
                return
        elif (self.server.identity_key is not None or path in ("/check", "/hold", "/tool-arms", "/settings/droid-estop") or (path == "/heartbeat" and self.server.monitor is not None)) and not self._authorized():
            self._write_unauthorized()
            return
        try:
            body = _read_json_body(self)
        except (json.JSONDecodeError, ValueError) as e:
            if path == "/check":
                self._remember_refusal(f"a check was refused: malformed JSON body: {e}")
            _write_error(self, 400, f"malformed JSON body: {e}")
            return
        if path == "/settings/droid-estop" and self.server.droid_switch is not None:
            self._handle_droid_switch(body)
            return
        if path == "/heartbeat" and self.server.monitor is not None:
            self._handle_heartbeat(body)
            return
        if path == "/hold" and self.server.holds is not None:
            self._handle_hold(body)
            return
        if path == "/tool-arms" and self.server.tool_arms is not None:
            self._handle_tool_arm(body)
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
            self._remember_refusal(evaluation.reason, fault=True)
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

    def _droid_switch_view(self) -> dict:
        switch = self.server.droid_switch
        assert switch is not None
        return {"enabled": switch.enabled, "default": False, "running": switch.running}

    def _handle_droid_switch(self, body: dict) -> None:
        """Switch the reading of the droid emergency stop on or off. An administrator only (a caller with the token of the service itself counts as one), with a confirmation, and it is audited."""
        switch = self.server.droid_switch
        assert switch is not None
        if self.server.identity_key is None and not self.server.token:
            _write_error(self, 403, "changing a setting needs a credential: start the service with the identity of the server or a token")
            return
        identity = getattr(self, "identity", None)
        if identity is not None and identity.role != "admin":
            _write_error(self, 403, "only an administrator may change this setting")
            return
        if not isinstance(body, dict) or set(body) != {"enabled", "confirm"} or not isinstance(body["enabled"], bool):
            _write_error(self, 400, "the body must be an object with enabled (true or false) and confirm: true")
            return
        if body["confirm"] is not True:
            _write_json(self, 400, {"error": "changing this setting needs confirm: true", "code": "CONFIRMATION_REQUIRED"})
            return
        by = str(getattr(identity, "username", None) or "token")[:64]
        try:
            changed = switch.set_enabled(body["enabled"])
        except (DroidEstopConfigError, OSError) as exc:
            _write_error(self, 502, f"the setting could not be changed ({type(exc).__name__}); it stays as it was")
            return
        if changed and self.server.droid_gate is not None:
            self.server.droid_gate._audit("switch-by", f"changed by {by}")  # noqa: SLF001
        _write_json(self, 200, {**self._droid_switch_view(), "changed": changed})

    def _handle_hold(self, body: dict) -> None:
        """Hold (or release) one robot. Never without a credential; a stop request leaves only when the operator turned that on."""
        if self.server.identity_key is None and not self.server.token:
            _write_error(self, 403, "a hold needs a credential: start the service with the identity of the server or a token")
            return
        try:
            robot, reason, release = parse_hold_request(body)
        except HoldError as e:
            _write_error(self, 400, str(e))
            return
        holds = self.server.holds
        assert holds is not None
        identity = getattr(self, "identity", None)
        by = str(getattr(identity, "username", None) or "token")[:64]
        # the token of the service counts as an administrator, as it does for the settings
        privileged = identity is None or identity.role == "admin"
        if release:
            try:
                released = holds.release(robot, by=by, privileged=privileged)
            except HoldDenied as e:
                _write_error(self, 403, str(e))
                return
            _write_json(self, 200, {"robot": robot, "held": False, "released": released, "actuated": False})
            return
        try:
            record = holds.hold(robot, reason, by, service=identity is None, privileged=privileged)
        except HoldError as e:
            _write_error(self, 429, str(e))
            return
        actuated = False
        requester = self.server.estop_requester
        if self.server.hold_actuate and requester is not None:
            try:
                requester.send(EStopRequest(object_id=robot, zone_id="hold", reason=f"hold: {reason}", zone_ids=("hold",)))
                actuated = True
            except Exception:  # noqa: BLE001 - the hold stays recorded; the caller is told the stop was not requested
                actuated = False
        _write_json(self, 200, {"robot": robot, "held": True, "seq": record.seq, "actuated": actuated})

    def _handle_tool_arm(self, body: dict) -> None:
        """Record the arm state of one tool of one robot. Never without a credential; it only records, it requests no stop."""
        if self.server.identity_key is None and not self.server.token:
            _write_error(self, 403, "a tool arm report needs a credential: start the service with the identity of the server or a token")
            return
        try:
            robot, tool_id, armed = parse_tool_arm_report(body)
        except ToolArmError as e:
            _write_error(self, 400, str(e))
            return
        registry = self.server.tool_arms
        assert registry is not None
        identity = getattr(self, "identity", None)
        by = getattr(identity, "username", None) or "token"
        try:
            record = registry.report(robot, tool_id, armed, str(by)[:64])
        except ToolArmLimitError as e:
            _write_error(self, 429, str(e))
            return
        _write_json(self, 200, {"robot": robot, "toolId": tool_id, "armed": armed, "seq": record.seq})

    def _handle_heartbeat(self, body: dict) -> None:
        """The observer's proof of life. An invalid one renews nothing: the previous heartbeat just keeps ageing."""
        monitor = self.server.monitor
        assert monitor is not None
        try:
            observation = parse_observation_status(body)
        except (ObservationError, TypeError, ValueError) as e:
            _write_error(self, 400, f"invalid heartbeat: {e}")
            return
        monitor.record_heartbeat(observation, self.server.clock())
        _write_json(self, 200, {"accepted": True, "validForSeconds": monitor.settings.heartbeat_ttl_s})

    def _remember_refusal(self, reason: str, *, fault: bool = False) -> None:
        """A check that reached no verdict leaves INHIBITED behind: GET /state must not keep serving the earlier READY."""
        evaluation = SafetyEvaluation(SafetyState.INHIBITED, reason[:300], fault=fault)
        self.server.log_decision(
            {"state": evaluation.state.value, "reason": evaluation.reason, "breaches": [], "estopRequests": [], "sdkSafetyState": to_sdk_safety_state(evaluation)}
        )

    def _refuse_check(self, message: str) -> None:
        self._remember_refusal(f"a check was refused: {message}")
        _write_error(self, 400, message)

    def _handle_check(self, body: dict) -> None:
        monitor = self.server.monitor
        try:
            if self.server.zone_set is not None:
                # Authoritative mode: the zones and calibration are the
                # server's own. A request that brings its own is refused,
                # not silently ignored, so nobody believes they were used.
                if "zones" in body:
                    self._refuse_check("this server holds its own zones (--zones-file): a request must not send zones")
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
            if monitor is not None and observation_raw is not None:
                # The observer must not vouch for its own freshness: in the stateful mode the evidence is its heartbeats.
                self._refuse_check("this server is stateful: the observation comes from POST /heartbeat, a request must not send one")
                return
            observation = parse_observation_status(observation_raw) if observation_raw is not None else None
            if monitor is not None:
                observation = monitor.current_observation(self.server.clock())
            # Optional real head/tool velocity (meters/second) - an
            # additional, opt-in envelope-scaling input alongside
            # `observation` above, with the exact same fail-safe shape: a
            # caller that never sends it (or an older caller not yet
            # updated to) gets today's static zone behavior, unchanged.
            velocity_raw = body.get("toolVelocityMps")
            tool_velocity_mps = parse_tool_velocity(velocity_raw) if velocity_raw is not None else None
        except KeyError as e:
            self._refuse_check(f"missing required field: {e}")
            return
        except (ConfigError, ObservationError, TypeError, ValueError) as e:
            self._refuse_check(f"invalid safety configuration: {e}")
            return

        robot_body: RobotBody | None = None
        body_raw = body.get("robotBody")
        if body_raw is not None:
            try:
                robot_body = parse_robot_body(body_raw, zone_set.robot_body)
            except RobotBodyError as e:
                # Never ignored and never read as a smaller body: the request is refused and the answer says the cell is not verified.
                evaluation = SafetyEvaluation(SafetyState.INHIBITED, f"robot body refused: {e}")
                self._remember_refusal(evaluation.reason)
                _write_json(
                    self,
                    400,
                    {
                        "error": f"invalid robot body: {e}",
                        "state": evaluation.state.value,
                        "reason": evaluation.reason,
                        "breaches": [],
                        "estopRequests": [],
                        "sdkSafetyState": to_sdk_safety_state(evaluation),
                    },
                )
                return

        limit_error = self._limit_violation(zone_set, observation)
        if limit_error:
            self._refuse_check(limit_error)
            return

        today = datetime.now(timezone.utc).date()
        if monitor is not None and observation is None:
            evaluation = monitor.missing_heartbeat_evaluation(self.server.clock())
        else:
            evaluation = evaluate_safety(zone_set, objects, today, observation, tool_velocity_mps=tool_velocity_mps, body=robot_body)

        # The alarm of A.R.M.O.R. (off unless armorAlarm.enabled): a sounding alarm, a silent panel or a corrupt message INHIBITS a verdict that would be READY;
        # it never turns a running machine off, except when the installation switched the safe stop on.
        armor = None
        armor_only = False
        armor_stop = False
        if self.server.armor_gate is not None:
            armor = self.server.armor_gate.status(self.server.clock())
            before = evaluation.state
            new_state, new_reason = combine(before.value, evaluation.reason, armor)
            if new_state != before.value:
                evaluation = SafetyEvaluation(SafetyState(new_state), new_reason, evaluation.fault, evaluation.separation)
                armor_only = before is SafetyState.READY and evaluation.state is SafetyState.INHIBITED
                armor_stop = evaluation.state is SafetyState.DANGER and before is not SafetyState.DANGER
            elif armor.inhibits and before is SafetyState.WARNING:
                evaluation = SafetyEvaluation(before, f"{evaluation.reason}; {armor.reason}", evaluation.fault, evaluation.separation)

        # The emergency stop of a droid (off unless droidEstop.enabled): a cut robot, a lost link or a silent bridge INHIBITS a verdict that would be READY, like the alarm;
        # it stops nothing that runs, except when the installation switched the safe stop on.
        droid = None
        droid_only = False
        droid_stop = False
        if self.server.droid_gate is not None:
            droid = self.server.droid_gate.status(self.server.clock())
            before_droid = evaluation.state
            droid_state, droid_reason = combine(before_droid.value, evaluation.reason, droid)
            if droid_state != before_droid.value:
                evaluation = SafetyEvaluation(SafetyState(droid_state), droid_reason, evaluation.fault, evaluation.separation)
                droid_only = before_droid is SafetyState.READY and evaluation.state is SafetyState.INHIBITED
                droid_stop = evaluation.state is SafetyState.DANGER and before_droid is not SafetyState.DANGER
            elif droid.inhibits and before_droid is SafetyState.WARNING:
                evaluation = SafetyEvaluation(before_droid, f"{evaluation.reason}; {droid.reason}", evaluation.fault, evaluation.separation)

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
        if evaluation.separation is not None:
            # optional and additive: evaluated_safe / evaluated_unsafe / not_evaluated, with the zones and the reason of each
            response["separation"] = evaluation.separation
        if armor is not None:
            response["armorAlarm"] = armor.as_dict()
            if armor_only:
                response["armorInhibitOnly"] = True
        if droid is not None:
            response["droidEstop"] = droid.as_dict()
            if droid_only:
                response["droidInhibitOnly"] = True
        if robot_body is not None:
            response["robotBody"] = {"segments": len(robot_body.segments)}
        if self.server.config_sha256:
            # which configuration this verdict was reached with
            response["configSha256"] = self.server.config_sha256

        if armor_stop or droid_stop:
            self.server.request_gate_stops(response, armor, droid)

        if armor_stop or droid_stop or evaluation.state in (SafetyState.INHIBITED, SafetyState.READY):
            self.server.log_decision(response)
            _write_json(self, 200, response)
            return

        # Same scaled zones evaluate_safety() itself just breach-checked
        # against internally - recomputing here with the unscaled
        # zone_set.zones would report a DANGER/WARNING state alongside a
        # `breaches` list that doesn't actually explain it.
        breaches = collect_breaches(zone_set, objects, tool_velocity_mps, robot_body)
        response["breaches"] = [asdict(b) for b in breaches]

        if evaluation.state is SafetyState.WARNING:
            self.server.log_decision(response)
            _write_json(self, 200, response)
            return

        # the requester is the CAN one when the service was started with a CAN configuration, and the recording one otherwise
        requester = self.server.estop_requester or NullEStopRequester()
        try:
            requests = request_estop_for(breaches, requester)
        except Exception as exc:  # noqa: BLE001 - a transport that fails must not hide the verdict: the state stays a stop
            requests = ()
            response["estopTransportError"] = f"{type(exc).__name__}: {exc}"
        response["estopRequests"] = [asdict(r) for r in requests]
        self.server.log_decision(response)
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


# The most breaches GET /state lists (the count is always given), so the answer stays small when many objects are inside.
_STATE_BREACHES_MAX = 20

# Seconds GET /state serves a verdict that no check renewed (the stateful mode uses its own state_ttl_s).
DEFAULT_STATE_MAX_AGE_S = 5.0


class SafetyZonesServer(ThreadingHTTPServer):

    health_open: bool = False  # GET /health answers only when the operator opens it (health_route.py)
    state_max_age_s: float = DEFAULT_STATE_MAX_AGE_S  # how long GET /state serves a verdict that nothing renews; 0 keeps it for ever
    observer_token: str | None = None  # when set, only it (or an administrator of the server) may use POST /check and /heartbeat
    holds: HoldRegistry | None = None  # POST /hold and GET /holds exist only when the operator started the service with --hold (hold.py)
    tool_arms: ToolArmRegistry | None = None  # POST /tool-arms and GET /tool-arms exist only when the operator started the service with --tool-arms (tool_arms.py)
    hold_actuate: bool = False  # a hold also hands a stop request to the requester only with --hold-actuate
    identity_key: bytes | None = None  # the signing key of HYDRA-UMC-SERVER, when the operator turned its identity on (server_identity.py)
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
        decision_log: str | None = None,
        stateful: StatefulSettings | None = None,
        clock: Callable[[], float] | None = None,
        signer: Signer | None = None,
        estop_requester: EStopRequester | None = None,
        armor_gate: ArmorAlarmGate | None = None,
        droid_gate: DroidEstopGate | None = None,
        droid_switch: DroidSwitch | None = None,
    ) -> None:
        # Set before the socket is bound: a failed bind calls server_close().
        self.stopping = threading.Event()
        super().__init__(address, Handler)
        # Seconds on a clock that only moves forward; a test hands in a simulated one.
        self.clock: Callable[[], float] = clock or time.monotonic
        self.monitor: StatefulMonitor | None = (
            StatefulMonitor(stateful, config_sha256) if stateful is not None and stateful.enabled else None
        )
        self._watcher: threading.Thread | None = None
        self.role = role
        self.token = token or None
        self.zone_set = zone_set
        self.config_sha256 = config_sha256
        self.max_observation_age_seconds = max_observation_age_seconds
        self.max_calibration_age_days = max_calibration_age_days
        self._connections = threading.BoundedSemaphore(MAX_CONNECTIONS)
        # Off unless a destination is given: "-" is standard error, anything else a file that lines are appended to.
        self._decision_log = decision_log or None
        self._decision_lock = threading.Lock()
        self._last_verdict: dict | None = None
        self._last_verdict_at: float | None = None
        self.gate_stop_error: str | None = None
        self._verdict_lock = threading.Lock()
        # Off unless a key was given: then every verdict carries a signature (see signing.py).
        self.signer = signer
        self.estop_requester = estop_requester
        # Off unless the zones file switches armorAlarm.enabled on (see armor_alarm.py).
        self.armor_gate = armor_gate
        # Off unless the zones file switches droidEstop.enabled on (see droid_estop.py).
        self.droid_gate = droid_gate if droid_gate is not None or droid_switch is None else droid_switch.gate
        # Off unless the zones file names droidEstop.stateFile: the administrator can switch the reading on or off while the service runs.
        self.droid_switch = droid_switch
        # Off unless the zones file switches faultEvents.enabled on: publishes fault-event/1 when the state changes (see fault_events.py).
        self.fault_emitter: FaultEventEmitter | None = None
        # Off unless the zones file switches zoneOccupancy.enabled on: publishes the occupied zones for the vehicles (see zone_occupancy.py).
        self.occupancy_emitter: ZoneOccupancyEmitter | None = None
        # Off unless the zones file switches recordTrigger.enabled on: asks the camera streamers to keep their frames when a safe stop begins (see record_trigger.py).
        self.record_trigger: RecordTriggerClient | None = None
        # Off unless the zones file switches cellState.enabled on: writes the file the bridges read as the state of the cell (see cell_state_file.py).
        self.cell_state_writer: CellStateWriter | None = None

    def _gates_that_request(self) -> list[tuple[str, str, ArmorAlarmGate | DroidEstopGate]]:
        gates: list[tuple[str, str, ArmorAlarmGate | DroidEstopGate]] = []
        if self.armor_gate is not None and self.armor_gate.settings.request_stop:
            gates.append(("armor-alarm", "armor alarm", self.armor_gate))
        if self.droid_gate is not None and self.droid_gate.settings.request_stop:
            gates.append(("droid-estop", "droid emergency stop", self.droid_gate))
        return gates

    def _send_gate_stop(self, source: str, what: str, detail: str) -> EStopRequest:
        request = EStopRequest(object_id=source, zone_id=source, reason=f"{what} safe stop: {detail}"[:300], zone_ids=(source,))
        (self.estop_requester or NullEStopRequester()).send(request)
        return request

    def request_gate_stops(self, response: dict, armor: object, droid: object) -> None:
        """The safe stop of the alarm or of a droid, with `requestStop` on, asks for the stop frame with a reason of its own."""
        statuses = {"armor-alarm": armor, "droid-estop": droid}
        for source, what, _gate in self._gates_that_request():
            status = statuses[source]
            if status is None or not getattr(status, "safe_stop", False):
                continue
            try:
                request = self._send_gate_stop(source, what, str(getattr(status, "reason", "")))
            except Exception as exc:  # noqa: BLE001 - a transport that fails must not hide the verdict
                response["estopTransportError"] = f"{type(exc).__name__}: {exc}"
            else:
                response["estopRequests"] = [*response["estopRequests"], asdict(request)]

    def watch_once(self) -> None:
        """One turn of the watcher: ask again for the stop of a gate that holds a safe stop (even if nobody calls /check), and hold an expired verdict."""
        now = self.clock()
        for source, what, gate in self._gates_that_request():
            status = gate.status(now)
            if status.safe_stop:
                try:
                    self._send_gate_stop(source, what, status.reason)
                    self.gate_stop_error = None
                except Exception as exc:  # noqa: BLE001 - tried again on the next turn
                    if self.gate_stop_error is None:
                        print(f"[safety-zones] the stop of the {what} could not be requested: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                    self.gate_stop_error = type(exc).__name__
        if self.monitor is not None:
            expired = self.monitor.tick(now)
            if expired is not None:
                response = {**expired, "breaches": [], "estopRequests": []}
                self._sign(response)
                self._remember_verdict(response)

    def start_watcher(self, interval_s: float = 0.25) -> None:
        """Starts the thread of watch_once(): needed in stateful mode and when a gate requests its stop on the bus."""
        if self._watcher is not None or (self.monitor is None and not self._gates_that_request()):
            return

        def run() -> None:
            while not self.stopping.wait(interval_s):
                try:
                    self.watch_once()
                except Exception as exc:  # noqa: BLE001 - the watcher must outlive one bad turn
                    print(f"[safety-zones] the watcher failed once: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

        self._watcher = threading.Thread(target=run, name="safety-zones-expiry", daemon=True)
        self._watcher.start()

    def server_close(self) -> None:
        self.stopping.set()
        super().server_close()

    def last_verdict(self) -> dict | None:
        """The most recent verdict of /check as the SDK SafetyState with the time it was reached, or None before the first one.

        A verdict that nothing renewed for longer than its time to live is answered as INHIBITED (and signed as such when the service signs).
        """
        with self._verdict_lock:
            record = None if self._last_verdict is None else dict(self._last_verdict)
            reached = self._last_verdict_at
        if record is None:
            return None
        limit = self.monitor.settings.state_ttl_s if self.monitor is not None else self.state_max_age_s
        if not limit or self.state_max_age_s == 0 or reached is None:
            return record
        age = self.clock() - reached
        if 0 <= age <= limit:
            return record
        evaluation = SafetyEvaluation(SafetyState.INHIBITED, f"the last verdict is more than {limit:g}s old and nothing renewed it - the state is not confirmed")
        expired: dict = {"sdkSafetyState": to_sdk_safety_state(evaluation), "reason": evaluation.reason}
        self._sign(expired)
        if record.get("configSha256"):
            expired["configSha256"] = record["configSha256"]
        return expired

    def _sign(self, response: dict) -> None:
        """Adds `sdkSafetyStateSignature` to a verdict when the service signs; a signing that fails leaves the verdict unsigned, which a consumer reads as INHIBITED."""
        sdk = response.get("sdkSafetyState")
        if self.signer is None or not isinstance(sdk, dict):
            return
        try:
            response["sdkSafetyStateSignature"] = self.signer.sign(sdk)
        except Exception:  # noqa: BLE001
            response.pop("sdkSafetyStateSignature", None)

    def _remember_verdict(self, response: dict) -> None:
        sdk = response.get("sdkSafetyState")
        if not isinstance(sdk, dict):
            return
        record = {"sdkSafetyState": dict(sdk), "reason": response.get("reason")}
        if isinstance(response.get("sdkSafetyStateSignature"), dict):
            record["sdkSafetyStateSignature"] = dict(response["sdkSafetyStateSignature"])
        if response.get("armorInhibitOnly") is True:
            # the only cause of the INHIBITED is the house alarm: it must not silence the safe heartbeat of a machine that is running
            record["armorInhibitOnly"] = True
        if response.get("droidInhibitOnly") is True:
            # the only cause of the INHIBITED is a cut droid: it must not silence the safe heartbeat of a machine that is running
            record["droidInhibitOnly"] = True
        if response.get("configSha256"):
            record["configSha256"] = response["configSha256"]
        if isinstance(response.get("separation"), dict):
            record["separation"] = response["separation"]
        # What the panels show next to the verdict: which zone each object is in, at which level, and whether a stop was asked.
        # Short and without positions; the signed state above is not touched.
        breaches = response.get("breaches")
        if isinstance(breaches, list):
            record["breaches"] = [
                {"zone_id": str(b.get("zone_id", "")), "level": str(getattr(b.get("level"), "value", b.get("level", ""))), "object_id": str(b.get("object_id", ""))}
                for b in breaches[:_STATE_BREACHES_MAX]
                if isinstance(b, dict)
            ]
            record["breachCount"] = len(breaches)
        estops = response.get("estopRequests")
        if isinstance(estops, list):
            record["estopRequestCount"] = len(estops)
        with self._verdict_lock:
            self._last_verdict = record
            self._last_verdict_at = self.clock()

    def log_decision(self, response: dict) -> None:
        """Remember the verdict for GET /state and, when a destination is set, append one JSON line (state, reason, configuration hash, counts); never raises."""
        self._sign(response)
        self._remember_verdict(response)
        if self.fault_emitter is not None:
            self.fault_emitter.observe(response)
        if self.occupancy_emitter is not None:
            self.occupancy_emitter.observe(response)
        if self.record_trigger is not None:
            self.record_trigger.observe(response)
        if self.cell_state_writer is not None:
            self.cell_state_writer.observe(response)
        if self.monitor is not None:
            self.monitor.record_verdict(response, self.clock())
        if self._decision_log is None:
            return
        line = json.dumps(
            {
                "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "state": response.get("state"),
                "sdkSafetyState": response.get("sdkSafetyState"),
                "reason": response.get("reason"),
                "configSha256": response.get("configSha256"),
                "breaches": len(response.get("breaches", [])),
                "estopRequests": len(response.get("estopRequests", [])),
            },
            sort_keys=True,
        )
        try:
            with self._decision_lock:
                if self._decision_log == "-":
                    print(line, file=sys.stderr, flush=True)
                else:
                    with open(self._decision_log, "a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
        except OSError:
            pass  # a log that cannot be written must never change the verdict

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
