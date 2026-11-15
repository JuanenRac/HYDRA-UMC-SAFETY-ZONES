# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/droid_estop.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The emergency stop of a droid reaching the safety state of a cell: a cut robot INHIBITS new starts and it is SHOWN.

The droid bridge (HYDRA-UMC-BRIDGE-DROIDS) cuts the Spot session when an operator asks for it over ``hydra/bridges/droids/cmd/estop``, when the global stop is announced or when the
link is lost, and says so on its retained state topic ``hydra/bridges/droids/state`` (``session_ready``, ``cut_reason``, ``link_lost``, ``observed_at``) and on
``hydra/bridges/droids/availability`` (``online`` / ``offline``). This module reads those messages and keeps one answer for the cell: ``clear``, ``cut``, ``holding``,
``unknown`` or ``disabled``.

Rules, all of them fail-safe:

- everything is OFF by default (``droidEstop.enabled``); the transport (``armor_alarm_mqtt.ArmorAlarmSubscriber``, reused) is only started on request;
- a cut droid is ``INHIBITED``; ``SAFE_STOP`` is possible ONLY when the installation switches ``safeStop`` on (off by default), because a stop of one droid must not stop a
  different machine of the cell unless the installation decided so;
- the bridge publishes its state retained and not on a timer, so a retained message IS applied here (unlike the alarm): a retained cut inhibits at once; a clear counts
  like any other "clear" and is subject to the hysteresis;
- no message is not "no cut": a bridge that was never heard from, that says ``offline``, or (when ``maxAgeS`` is above 0) that has been silent longer than that is ``unknown``
  and, by default, ``unknown`` inhibits too (``unknownInhibits``);
- an inhibition is lifted ONLY when the bridge itself says the session is ready with no cut reason and no lost link, and ``hysteresisS`` seconds go by without anything else;
- a message that is not a JSON object of the expected fields inhibits (and holds the inhibition until good messages come back and the hysteresis passes);
- the broker must be on this machine unless the installation declares it internal (``broker.internal``): the messages of the bridge are not signed.

Everything takes its clock as an argument (seconds on a monotonic clock), so the tests drive it with simulated time. Nothing here was tried with a real droid.
"""
from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from hydra_umc_safety_zones.armor_alarm import ArmorAlarmConfigError, BrokerSettings, _parse_broker

DEFAULT_TOPIC = "hydra/bridges/droids/state"
AVAILABILITY_SUFFIX = "availability"
MAX_MESSAGE_BYTES = 16 * 1024
_FIELDS = {"enabled", "topic", "maxAgeS", "hysteresisS", "unknownInhibits", "safeStop", "auditLog", "broker"}


class DroidEstopConfigError(ArmorAlarmConfigError):
    """The droidEstop settings cannot be used."""


@dataclass(frozen=True)
class DroidEstopSettings:
    """Tunable values of the reading of the droid emergency stop (the ``droidEstop`` object of the zones file).

    - ``enabled`` the switch (off by default: nothing is read and the verdict is unchanged).
    - ``topic`` the retained state topic of the droid bridge; the availability topic is its sibling.
    - ``maxAgeS`` seconds of silence after which the bridge is ``unknown`` (0: not checked, which is the default because the bridge publishes on events, not on a timer).
    - ``hysteresisS`` seconds a "no cut" must last before the inhibition is lifted. ``unknownInhibits`` an unknown bridge inhibits (default true).
    - ``safeStop`` lets a cut droid reach SAFE_STOP (default false). ``auditLog`` file for the audit lines (``-``: standard error; empty: only memory).
    """

    enabled: bool = False
    topic: str = DEFAULT_TOPIC
    max_age_s: float = 0.0
    hysteresis_s: float = 10.0
    unknown_inhibits: bool = True
    safe_stop: bool = False
    audit_log: str = ""
    broker: BrokerSettings = field(default_factory=lambda: BrokerSettings(client_id="hydra-umc-safety-zones-droids"))


def _number(value: object, what: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise DroidEstopConfigError(f"droidEstop.{what} must be a number from {low} to {high}, got {value!r}")
    return float(value)


def parse_settings(data: object) -> DroidEstopSettings:
    """Reads the optional ``droidEstop`` object; absent means the defaults with the reading off. Anything unknown or malformed is refused."""
    if data is None:
        return DroidEstopSettings()
    if not isinstance(data, dict):
        raise DroidEstopConfigError("droidEstop must be a JSON object")
    unknown = sorted(set(data) - _FIELDS)
    if unknown:
        raise DroidEstopConfigError(f"droidEstop has unknown field(s): {unknown}")
    for key in ("enabled", "unknownInhibits", "safeStop"):
        if key in data and not isinstance(data[key], bool):
            raise DroidEstopConfigError(f"droidEstop.{key} must be true or false, got {data[key]!r}")
    topic = data.get("topic", DEFAULT_TOPIC)
    if not isinstance(topic, str) or not topic.startswith("hydra/bridges/") or not topic.endswith("/state") or "+" in topic or "#" in topic:
        raise DroidEstopConfigError("droidEstop.topic must be hydra/bridges/<name>/state, without wildcards")
    audit = data.get("auditLog", "")
    if not isinstance(audit, str):
        raise DroidEstopConfigError("droidEstop.auditLog must be a text")
    broker_data = data.get("broker")
    if isinstance(broker_data, dict) and "clientId" not in broker_data:
        broker_data = {**broker_data, "clientId": "hydra-umc-safety-zones-droids"}
    try:
        # the messages of the bridge are not signed, so a broker outside this machine must be declared internal
        broker = _parse_broker(broker_data, False)
    except ArmorAlarmConfigError as exc:
        raise DroidEstopConfigError(str(exc).replace("armorAlarm.", "droidEstop.")) from exc
    return DroidEstopSettings(
        enabled=data.get("enabled", False),
        topic=topic,
        max_age_s=_number(data.get("maxAgeS", 0.0), "maxAgeS", 0.0, 86400.0),
        hysteresis_s=_number(data.get("hysteresisS", 10.0), "hysteresisS", 0.0, 3600.0),
        unknown_inhibits=data.get("unknownInhibits", True),
        safe_stop=data.get("safeStop", False),
        audit_log=audit,
        broker=broker if broker_data is not None else DroidEstopSettings().broker,
    )


@dataclass(frozen=True)
class DroidEstopStatus:
    """The answer for the cell. ``state``: disabled, clear, cut, holding (clear but still inside the hysteresis) or unknown."""

    state: str
    inhibits: bool
    safe_stop: bool
    reason: str
    detail: str = ""
    last_message_age_s: float | None = None

    def as_dict(self) -> dict:
        return {
            "state": self.state, "inhibits": self.inhibits, "safeStop": self.safe_stop, "reason": self.reason,
            "detail": self.detail, "lastMessageAgeS": self.last_message_age_s,
        }


DISABLED_STATUS = DroidEstopStatus("disabled", False, False, "the reading of the droid emergency stop is switched off (droidEstop.enabled)")


class DroidEstopGate:
    """Keeps what the droid bridge says and answers ``status(now)``. Thread-safe; ``ingest`` never raises."""

    def __init__(self, settings: DroidEstopSettings, audit_sink: Callable[[dict], None] | None = None) -> None:
        self.settings = settings
        self._audit_sink = audit_sink
        self._lock = threading.Lock()
        self.audit_records: list[dict] = []
        self._received_at: float | None = None
        self._cut_reason = ""
        self._link_lost = False
        self._session_ready = False
        self._fault: str | None = None
        self._offline = False
        self._clear_since: float | None = None
        self._last_status: tuple[str, bool] | None = None

    def topics(self) -> tuple[str, str]:
        """The state topic and its availability sibling."""
        return self.settings.topic, self.settings.topic.rsplit("/", 1)[0] + "/" + AVAILABILITY_SUFFIX

    def ingest(self, topic: str, payload: bytes | str | None, mono_now: float, retained: bool = False) -> None:  # noqa: ARG002 - a retained message counts here
        if not self.settings.enabled:
            return
        state_topic, availability_topic = self.topics()
        with self._lock:
            if topic == availability_topic:
                self._ingest_availability(payload, mono_now)
            elif topic == state_topic:
                self._ingest_state(payload, mono_now)
            else:
                self._audit("ignored", f"topic {topic!r} is not a topic of the droid bridge")

    def _ingest_availability(self, payload: bytes | str | None, mono_now: float) -> None:
        text = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else (payload or "")
        text = text.strip().lower()
        if text == "offline":
            self._offline = True
            self._clear_since = None
            self._received_at = mono_now
            self._audit("offline", "the droid bridge says it is offline")
        elif text == "online":
            self._offline = False
            self._received_at = mono_now
        else:
            self._fault = f"the availability {text[:20]!r} is neither online nor offline"
            self._clear_since = None
            self._received_at = mono_now
            self._audit("corrupt", self._fault)

    def _ingest_state(self, payload: bytes | str | None, mono_now: float) -> None:
        try:
            message = self._decode(payload)
        except ValueError as exc:
            self._fault = str(exc)
            self._clear_since = None
            self._received_at = mono_now
            self._audit("corrupt", str(exc))
            return
        self._fault = None
        self._received_at = mono_now
        self._offline = False
        self._session_ready = message["session_ready"]
        self._cut_reason = message["cut_reason"]
        self._link_lost = message["link_lost"]
        if self._cut_reason or self._link_lost:
            self._clear_since = None
            self._audit("cut", self._cut_reason or "the link to the robot is lost")
        elif self._clear_since is None:
            self._clear_since = mono_now

    @staticmethod
    def _decode(payload: bytes | str | None) -> dict:
        if payload is None:
            raise ValueError("the message is empty")
        raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
        if not raw or len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError("the message is empty or larger than the limit")

        def reject(token: str) -> object:
            raise ValueError(f"the literal {token} is not valid JSON")

        try:
            data = json.loads(raw.decode("utf-8"), parse_constant=reject)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError(f"the message is not valid JSON ({type(exc).__name__})") from exc
        if not isinstance(data, dict):
            raise ValueError("the message is not a JSON object")
        if not isinstance(data.get("session_ready"), bool) or not isinstance(data.get("cut_reason"), str):
            raise ValueError("session_ready must be true or false and cut_reason a text")
        if not isinstance(data.get("link_lost", False), bool):
            raise ValueError("link_lost must be true or false")
        return {"session_ready": data["session_ready"], "cut_reason": data["cut_reason"].strip()[:200], "link_lost": data.get("link_lost", False)}

    def status(self, mono_now: float) -> DroidEstopStatus:
        """The answer now. Calling it also audits the changes."""
        if not self.settings.enabled:
            return DISABLED_STATUS
        s = self.settings
        with self._lock:
            age = None if self._received_at is None else max(0.0, mono_now - self._received_at)
            if self._received_at is None:
                result = DroidEstopStatus("unknown", s.unknown_inhibits, False, "no message of the droid bridge has arrived yet: no news is not 'no cut'")
            elif mono_now < self._received_at or (s.max_age_s > 0 and age is not None and age > s.max_age_s):
                result = DroidEstopStatus("unknown", s.unknown_inhibits, False, f"the droid bridge has been silent for more than {s.max_age_s:g}s", "", age)
            elif self._fault is not None:
                result = DroidEstopStatus("cut", True, False, f"droid emergency stop: corrupt message: {self._fault}", self._fault, age)
            elif self._offline:
                result = DroidEstopStatus("unknown", s.unknown_inhibits, False, "the droid bridge says it is offline", "offline", age)
            elif self._cut_reason or self._link_lost:
                detail = self._cut_reason or "the link to the robot is lost"
                result = DroidEstopStatus("cut", True, s.safe_stop, f"droid emergency stop: {detail}", detail, age)
            else:
                waited = 0.0 if self._clear_since is None else mono_now - self._clear_since
                if waited < s.hysteresis_s:
                    result = DroidEstopStatus("holding", True, False, f"droid session clear, waiting {s.hysteresis_s:g}s before lifting ({waited:.1f}s)", "", age)
                else:
                    result = DroidEstopStatus("clear", False, False, "the droid bridge reports no emergency stop", "", age)
            key = (result.state, result.inhibits)
            if key != self._last_status:
                self._audit("status", f"{result.state} (inhibits={result.inhibits}): {result.reason}")
                self._last_status = key
            return result

    def _audit(self, event: str, detail: str) -> None:
        record = {"at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "source": "droid-estop", "event": event, "detail": detail}
        self.audit_records.append(record)
        del self.audit_records[:-200]
        if self._audit_sink is not None:
            try:
                self._audit_sink(record)
            except Exception:  # noqa: BLE001 - an audit that cannot be written never changes the answer
                pass
