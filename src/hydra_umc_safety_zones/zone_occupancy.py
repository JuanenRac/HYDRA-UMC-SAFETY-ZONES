# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/zone_occupancy.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The publisher of the occupied zones: tells the broker which zones have something inside them, for the vehicles that must not drive into them.

The zone interlock of the AMR bridge reads ``{"occupied": ["press-1"], "observed_at": <milliseconds>}`` on ``hydra/safety/zones/occupied``. This module
publishes exactly that, from the breaches of every verdict of ``POST /check``: a zone is occupied while a breach names it (at any level).

- Not retained: an occupancy that stays on the broker after this service has died would be believed for ever. A reader treats a message older than its own maximum
  age as UNKNOWN, so the message is repeated while the service runs: at once when the set changes and otherwise every ``refreshS`` seconds.
- A verdict with no readable list of breaches publishes nothing (an unknown occupancy is not "free"). It only observes: it never changes a verdict, never blocks a
  request and never raises into the service; a failure to publish is counted and printed once.
- Off by default (``zoneOccupancy.enabled`` in the zones file). paho-mqtt is optional and imported only when the switch is on. Credentials come from files.
  Nothing here has run against a real broker with a real vehicle behind it.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from hydra_umc_safety_zones.armor_alarm import BrokerSettings
from hydra_umc_safety_zones.fault_events import FaultEventSettings, FaultEventsConfigError, MqttFaultPublisher, _parse_broker

DEFAULT_TOPIC = "hydra/safety/zones/occupied"
_TOPIC = re.compile(r"[A-Za-z0-9._\-]+(/[A-Za-z0-9._\-]+)*")
_ZONE = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")
_FIELDS = {"enabled", "topic", "refreshS", "broker"}
MAX_ZONES = 500
CLIENT_ID = "hydra-umc-safety-zones-occupancy"


class ZoneOccupancyConfigError(ValueError):
    """The ``zoneOccupancy`` object of the zones file cannot be used."""


@dataclass(frozen=True)
class ZoneOccupancySettings:
    """``enabled`` the switch (off); ``topic`` where the occupancy goes (under ``hydra/safety/``); ``refresh_s`` the longest time between two messages; ``broker`` where to connect."""

    enabled: bool = False
    topic: str = DEFAULT_TOPIC
    refresh_s: float = 1.0
    broker: BrokerSettings = field(default_factory=lambda: BrokerSettings(client_id=CLIENT_ID))


def parse_zone_occupancy_settings(data: object) -> ZoneOccupancySettings:
    """Reads the optional ``zoneOccupancy`` object; absent means the defaults with the publisher off. Anything unknown or malformed is refused."""
    if data is None:
        return ZoneOccupancySettings()
    if not isinstance(data, dict):
        raise ZoneOccupancyConfigError("zoneOccupancy must be a JSON object")
    unknown = sorted(set(data) - _FIELDS)
    if unknown:
        raise ZoneOccupancyConfigError(f"zoneOccupancy has unknown field(s): {unknown}")
    if "enabled" in data and not isinstance(data["enabled"], bool):
        raise ZoneOccupancyConfigError(f"zoneOccupancy.enabled must be true or false, got {data['enabled']!r}")
    topic = data.get("topic", DEFAULT_TOPIC)
    if not isinstance(topic, str) or not _TOPIC.fullmatch(topic) or not topic.startswith("hydra/safety/"):
        raise ZoneOccupancyConfigError("zoneOccupancy.topic must be a topic under hydra/safety/ without wildcards")
    refresh = data.get("refreshS", 1.0)
    if isinstance(refresh, bool) or not isinstance(refresh, (int, float)) or not 0.1 <= refresh <= 3600:
        raise ZoneOccupancyConfigError("zoneOccupancy.refreshS must be a number from 0.1 to 3600")
    broker_data = data.get("broker")
    try:
        broker = _parse_broker(broker_data)
    except FaultEventsConfigError as exc:
        raise ZoneOccupancyConfigError(str(exc).replace("faultEvents.", "zoneOccupancy.")) from exc
    if not (isinstance(broker_data, dict) and "clientId" in broker_data):
        broker = replace(broker, client_id=CLIENT_ID)
    return ZoneOccupancySettings(enabled=data.get("enabled", False), topic=topic, refresh_s=float(refresh), broker=broker)


def occupied_zones(response: dict) -> list[str] | None:
    """The sorted ids of the zones that a verdict says have something inside, or None when the verdict does not say (no usable list of breaches)."""
    breaches = response.get("breaches")
    if not isinstance(breaches, list):
        return None
    found: set[str] = set()
    for breach in breaches:
        zone = breach.get("zone_id") if isinstance(breach, dict) else None
        if not isinstance(zone, str) or not _ZONE.fullmatch(zone):
            return None  # a breach that cannot be read: the occupancy is unknown, not smaller
        found.add(zone)
    return sorted(found)[:MAX_ZONES]


Publish = Callable[[str, bytes, bool], None]


class ZoneOccupancyEmitter:
    """Watches the verdicts and publishes the occupied zones. ``publish(topic, payload, retain)`` sends one message."""

    def __init__(
        self,
        settings: ZoneOccupancySettings,
        publish: Publish,
        clock: Callable[[], float] = time.monotonic,
        wall_clock_ms: Callable[[], float] = lambda: time.time() * 1000.0,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self._publish = publish
        self._clock = clock
        self._wall = wall_clock_ms
        self._log = log or (lambda _message: None)
        self._lock = threading.Lock()
        self._published: list[str] | None = None
        self._last_at: float | None = None
        self._warned = False
        self.sent = 0
        self.failed = 0

    def observe(self, response: dict) -> None:
        """Called with every verdict of ``POST /check``. Never raises and never changes the verdict."""
        try:
            if not self.settings.enabled:
                return
            zones = occupied_zones(response)
            if zones is None:
                return
            now = self._clock()
            with self._lock:
                if zones == self._published and self._last_at is not None and now - self._last_at < self.settings.refresh_s:
                    return
                previous = (self._published, self._last_at)
                self._published, self._last_at = zones, now
            payload = json.dumps({"occupied": zones, "observed_at": int(self._wall())}, sort_keys=True).encode("utf-8")
            try:
                self._publish(self.settings.topic, payload, False)
                self.sent += 1
            except Exception as error:  # noqa: BLE001 - publishing is best effort and must not touch the verdict
                self.failed += 1
                with self._lock:
                    self._published, self._last_at = previous
                    first = not self._warned
                    self._warned = True
                if first:
                    self._log(f"[safety-zones] zone occupancy: could not publish ({type(error).__name__}); the verdicts are not affected")
        except Exception:  # noqa: BLE001 - nothing here may reach the service
            self.failed += 1


def build_publisher(settings: ZoneOccupancySettings, client: Any = None) -> MqttFaultPublisher:
    """The MQTT publisher of the service for these settings (the connection code of the fault events is shared)."""
    return MqttFaultPublisher(FaultEventSettings(enabled=True, broker=settings.broker), client)
