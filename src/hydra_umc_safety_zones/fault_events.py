# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/fault_events.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The publisher of ``fault-event/1``: tells the broker when the safety state of this service stops being READY, and when it is READY again.

HYDRA-UMC-SERVER reads ``fault-event/1`` from the broker (``hydra/faults/#``), keeps the active faults and the history, and the MTConnect and OPC UA
adapters show them to the plant. Somebody has to publish them: this service knows when a machine is no longer cleared to start, so it does.

- One fault, ``code`` = ``safety_state``, ``source`` = ``safety-zones`` (both configurable). ``READY`` is ``cleared``; ``INHIBITED`` is ``raised`` with
  severity ``warning``; ``FAULT`` (the observer itself failed) is ``raised`` with severity ``error``; ``SAFE_STOP`` is ``raised`` with severity
  ``critical``. A change between two raised states updates the same fault. The text is the reason of the verdict, cut short.
- Only a CHANGE of the state is published, and not more often than ``minIntervalS``: a state that flaps does not flood the broker (the newest state is
  sent as soon as the interval has passed and the next verdict arrives, so the broker never keeps an old one for long). The message is retained by default,
  so a server that starts later reads the present state at once.
- It only observes. It never changes a verdict, never blocks a request (a broker that does not answer costs nothing) and never raises into the service; a
  failure to publish is counted and printed once. It publishes nothing but ``fault-event/1`` and reads nothing from the broker.
- Off by default (``faultEvents.enabled`` in the zones file). paho-mqtt is optional (``pip install "hydra-umc-safety-zones[mqtt]"``) and imported only when
  the switch is on. The credentials are read from files, never from the command line and never printed. Every value is in the ``faultEvents`` object.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hydra_umc_safety_zones.armor_alarm import BrokerSettings

SCHEMA = "fault-event/1"
SEVERITY_BY_SDK_STATE: dict[str, str] = {"INHIBITED": "warning", "FAULT": "error", "SAFE_STOP": "critical"}
_TOPIC = re.compile(r"[A-Za-z0-9._\-]+(/[A-Za-z0-9._\-]+)*")
_NAME = re.compile(r"[A-Za-z0-9._\-]{1,64}")
_FIELDS = {"enabled", "topic", "source", "code", "robotId", "retain", "minIntervalS", "broker"}
_BROKER_FIELDS = {"host", "port", "usernameFile", "passwordFile", "tls", "caFile", "internal", "clientId"}
TEXT_MAX = 240


class FaultEventsConfigError(ValueError):
    """The ``faultEvents`` object of the zones file cannot be used."""


@dataclass(frozen=True)
class FaultEventSettings:
    """Tunable values of the publisher (the ``faultEvents`` object of the zones file).

    - ``enabled`` the switch (off by default: nothing is published and the service is unchanged).
    - ``topic`` where the events go (must be under ``hydra/faults/`` so the server reads them); ``source`` and ``code`` name the fault; ``robot_id`` the robot
      it belongs to (empty: none).
    - ``retain`` keeps the last event on the broker; ``min_interval_s`` the shortest time between two events.
    - ``broker`` where to connect; a host that is not this machine needs ``internal: true``.
    """

    enabled: bool = False
    topic: str = "hydra/faults/safety-zones"
    source: str = "safety-zones"
    code: str = "safety_state"
    robot_id: str = ""
    retain: bool = True
    min_interval_s: float = 1.0
    broker: BrokerSettings = field(default_factory=lambda: BrokerSettings(client_id="hydra-umc-safety-zones-faults"))


def parse_fault_event_settings(data: object) -> FaultEventSettings:
    """Reads the optional ``faultEvents`` object; absent means the defaults with the publisher off. Anything unknown or malformed is refused."""
    if data is None:
        return FaultEventSettings()
    if not isinstance(data, dict):
        raise FaultEventsConfigError("faultEvents must be a JSON object")
    unknown = sorted(set(data) - _FIELDS)
    if unknown:
        raise FaultEventsConfigError(f"faultEvents has unknown field(s): {unknown}")
    for key in ("enabled", "retain"):
        if key in data and not isinstance(data[key], bool):
            raise FaultEventsConfigError(f"faultEvents.{key} must be true or false, got {data[key]!r}")
    topic = data.get("topic", "hydra/faults/safety-zones")
    if not isinstance(topic, str) or not _TOPIC.fullmatch(topic) or not topic.startswith("hydra/faults/"):
        raise FaultEventsConfigError("faultEvents.topic must be a topic under hydra/faults/ without wildcards")
    for key, default in (("source", "safety-zones"), ("code", "safety_state")):
        value = data.get(key, default)
        if not isinstance(value, str) or not _NAME.fullmatch(value):
            raise FaultEventsConfigError(f"faultEvents.{key} must be 1 to 64 letters, digits, dots, dashes or underscores")
    robot_id = data.get("robotId", "")
    if not isinstance(robot_id, str) or (robot_id and not _NAME.fullmatch(robot_id)):
        raise FaultEventsConfigError("faultEvents.robotId must be empty or 1 to 64 letters, digits, dots, dashes or underscores")
    interval = data.get("minIntervalS", 1.0)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 0 <= interval <= 3600:
        raise FaultEventsConfigError("faultEvents.minIntervalS must be a number from 0 to 3600")
    return FaultEventSettings(
        enabled=data.get("enabled", False), topic=topic, source=data.get("source", "safety-zones"), code=data.get("code", "safety_state"),
        robot_id=robot_id, retain=data.get("retain", True), min_interval_s=float(interval), broker=_parse_broker(data.get("broker")),
    )


def _parse_broker(data: object) -> BrokerSettings:
    if data is None:
        return BrokerSettings(client_id="hydra-umc-safety-zones-faults")
    if not isinstance(data, dict):
        raise FaultEventsConfigError("faultEvents.broker must be a JSON object")
    unknown = sorted(set(data) - _BROKER_FIELDS)
    if unknown:
        raise FaultEventsConfigError(f"faultEvents.broker has unknown field(s): {unknown}")
    for key in ("host", "usernameFile", "passwordFile", "caFile", "clientId"):
        if key in data and (not isinstance(data[key], str) or (key in ("host", "clientId") and not data[key])):
            raise FaultEventsConfigError(f"faultEvents.broker.{key} must be a text" + (" that is not empty" if key in ("host", "clientId") else ""))
    for key in ("tls", "internal"):
        if key in data and not isinstance(data[key], bool):
            raise FaultEventsConfigError(f"faultEvents.broker.{key} must be true or false")
    port = data.get("port", 1883)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise FaultEventsConfigError("faultEvents.broker.port must be an integer from 1 to 65535")
    broker = BrokerSettings(
        host=data.get("host", "127.0.0.1"), port=port, username_file=data.get("usernameFile", ""), password_file=data.get("passwordFile", ""),
        tls=data.get("tls", False), ca_file=data.get("caFile", ""), internal=data.get("internal", False),
        client_id=data.get("clientId", "hydra-umc-safety-zones-faults"),
    )
    if not broker.is_local() and not broker.internal:
        raise FaultEventsConfigError("faultEvents.broker.host is not on this machine: declare the broker internal (broker.internal: true) if the network is the installation's own")
    return broker


def build_fault_event(settings: FaultEventSettings, sdk_state: str, reason: object) -> dict[str, Any]:
    """The ``fault-event/1`` document for a safety state of the SDK (``READY``, ``INHIBITED``, ``FAULT`` or ``SAFE_STOP``)."""
    raised = sdk_state in SEVERITY_BY_SDK_STATE
    text = reason if isinstance(reason, str) and reason else f"safety state {sdk_state}"
    event: dict[str, Any] = {
        "schema": SCHEMA, "source": settings.source, "code": settings.code,
        "severity": SEVERITY_BY_SDK_STATE.get(sdk_state, "info"), "text": text[:TEXT_MAX], "state": "raised" if raised else "cleared",
    }
    if settings.robot_id:
        event["robot_id"] = settings.robot_id
    return event


Publish = Callable[[str, bytes, bool], None]


class FaultEventEmitter:
    """Watches the verdicts and publishes a ``fault-event/1`` when the state changes. ``publish(topic, payload, retain)`` sends one message."""

    def __init__(self, settings: FaultEventSettings, publish: Publish, clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] | None = None) -> None:
        self.settings = settings
        self._publish = publish
        self._clock = clock
        self._log = log or (lambda _message: None)
        self._lock = threading.Lock()
        self._published: str | None = None
        self._last_at: float | None = None
        self._warned = False
        self.sent = 0
        self.failed = 0

    def observe(self, response: dict) -> None:
        """Called with every verdict of ``POST /check``. Never raises and never changes the verdict."""
        try:
            sdk = response.get("sdkSafetyState")
            state = sdk.get("state") if isinstance(sdk, dict) else None
            if not isinstance(state, str) or not self.settings.enabled:
                return
            now = self._clock()
            with self._lock:
                if state == self._published:
                    return
                if self._published is None and state == "READY":
                    # the first verdict being READY says nothing was ever wrong: no event to clear
                    self._published, self._last_at = state, now
                    return
                if self._last_at is not None and now - self._last_at < self.settings.min_interval_s:
                    return  # the newest state goes out with the first verdict after the interval
                self._published, self._last_at = state, now
            payload = json.dumps(build_fault_event(self.settings, state, response.get("reason")), sort_keys=True).encode("utf-8")
            try:
                self._publish(self.settings.topic, payload, self.settings.retain)
                self.sent += 1
            except Exception as error:  # noqa: BLE001 - publishing is best effort and must not touch the verdict
                self.failed += 1
                with self._lock:
                    self._published = None  # try again with the next verdict
                    first = not self._warned
                    self._warned = True
                if first:
                    self._log(f"[safety-zones] fault events: could not publish ({type(error).__name__}); the verdicts are not affected")
        except Exception:  # noqa: BLE001 - nothing here may reach the service
            self.failed += 1


def _read_secret(path: str, what: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise FaultEventsConfigError(f"faultEvents.broker.{what} cannot be read: {exc.strerror or type(exc).__name__}") from exc


class MqttFaultPublisher:
    """Connects to the broker and publishes. ``client`` is a paho-like object (a test hands in a fake, so no broker is needed)."""

    def __init__(self, settings: FaultEventSettings, client: Any = None) -> None:
        self.settings = settings
        self.client = client
        self.connected = False

    def start(self) -> None:
        broker = self.settings.broker
        if self.client is None:
            try:
                import paho.mqtt.client as mqtt  # noqa: PLC0415
            except ImportError as exc:
                raise FaultEventsConfigError('the fault events need paho-mqtt (pip install "hydra-umc-safety-zones[mqtt]")') from exc
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=broker.client_id)
        client = self.client
        if broker.username_file:
            password = _read_secret(broker.password_file, "passwordFile") if broker.password_file else None
            client.username_pw_set(_read_secret(broker.username_file, "usernameFile"), password)
        if broker.tls:
            client.tls_set(ca_certs=broker.ca_file or None)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect_async(broker.host, broker.port, keepalive=30) if hasattr(client, "connect_async") else client.connect(broker.host, broker.port, 30)
        client.loop_start()

    def stop(self) -> None:
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()
        self.connected = False

    def publish(self, topic: str, payload: bytes, retain: bool) -> None:
        if self.client is None:
            raise RuntimeError("the publisher is not started")
        self.client.publish(topic, payload, qos=1, retain=retain)

    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
        failure = getattr(reason_code, "is_failure", None)
        self.connected = not (failure if isinstance(failure, bool) else reason_code not in (0, None))

    def _on_disconnect(self, client: Any, userdata: Any, *args: Any) -> None:
        self.connected = False
