# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/armor_alarm.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The alarm of a house (A.R.M.O.R.) reaching the safety state of a cell: it INHIBITS new starts and it is SHOWN, it never stops a machine that is running.

The panel of A.R.M.O.R. publishes its state on ``armor/alarm/<node_id>/state``. This module reads those messages, turns each one into a ``SafetyState`` of
the SDK with a conversion table (the same convention as ``armor_alarm_to_safety_state`` of HYDRA-UMC-SDK; the table is a setting, see
``docs/ARMOR_ALARM.md``) and keeps one answer for the cell: ``clear``, ``alarm``, ``unknown`` or ``disabled``.

Rules, all of them fail-safe:

- everything is OFF by default (``armorAlarm.enabled``); the transport (``armor_alarm_mqtt.py``) is only started on request;
- a sounding alarm is ``INHIBITED``; ``SAFE_STOP`` is possible ONLY when the installation switches ``safeStop`` on (off by default): a table that maps a
  phase to ``SAFE_STOP`` while the switch is off is read as ``INHIBITED`` and the downgrade is audited;
- no message is not "no alarm": a panel that has not been heard from for ``maxAgeS`` seconds (or never) is ``unknown`` and is shown as such; by default
  ``unknown`` inhibits too (``unknownInhibits``);
- an inhibition is lifted ONLY when the panel itself says "no alarm" and ``hysteresisS`` seconds go by without anything else (a new alarm, a corrupt
  message, a contradiction or silence restarts the wait);
- a corrupt or contradictory message inhibits (and holds the inhibition until good messages come back and the hysteresis passes): invalid JSON, a node
  id that is not the one in the topic, another ``kind``, a phase the table does not know, a timestamp that goes backwards (a replay), a siren that sounds
  in a phase the table calls clear;
- a retained message is never applied (it is an old one by definition).

The messages of the panel carry no signature (the contract of ARMOR-COMMON signs only the zone feed that goes TO the panel), so this module cannot prove who
sent one. It does not pretend to: the transport refuses a broker that is not on this machine unless the installation declares the broker internal, and the
allow-list ``nodes`` limits whose messages count. That limit is written as PENDING in the documentation.

Everything takes its clock as an argument (seconds on a monotonic clock), so the tests drive it with simulated time. Nothing here was tried with a real alarm.
"""
from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

SAFETY_STATES = ("READY", "INHIBITED", "FAULT", "SAFE_STOP")
#: What an alarm phase means unless the installation chooses another table. Same table as ``DEFAULT_ALARM_TO_SAFETY`` of the SDK; SAFE_STOP is never in it.
DEFAULT_PHASE_TABLE: dict[str, str] = {
    "disarmed": "READY",
    "exit_delay": "READY",
    "armed": "READY",
    "entry_delay": "READY",
    "alarm": "INHIBITED",
}
DEFAULT_TOPIC_FILTER = "armor/alarm/+/state"
MAX_MESSAGE_BYTES = 65536
_NODE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_FIELDS = {
    "enabled", "topic", "nodes", "phaseTable", "maxAgeS", "hysteresisS", "unknownInhibits", "safeStop", "auditLog", "broker",
}
_BROKER_FIELDS = {"host", "port", "usernameFile", "passwordFile", "tls", "caFile", "internal", "clientId"}


class ArmorAlarmConfigError(ValueError):
    """The armorAlarm settings cannot be used."""


def _number(value: object, what: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ArmorAlarmConfigError(f"armorAlarm.{what} must be a number from {low} to {high}, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class BrokerSettings:
    """Where the transport connects. A host that is not this machine needs ``internal: true`` (the installation says the network is its own)."""

    host: str = "127.0.0.1"
    port: int = 1883
    username_file: str = ""
    password_file: str = ""
    tls: bool = False
    ca_file: str = ""
    internal: bool = False
    client_id: str = "hydra-umc-safety-zones-armor"

    def is_local(self) -> bool:
        return self.host in ("127.0.0.1", "::1", "localhost")


@dataclass(frozen=True)
class ArmorAlarmSettings:
    """Tunable values of the reading of the ARMOR alarm (the ``armorAlarm`` object of the zones file).

    - ``enabled`` the switch (off by default: nothing is read and the verdict is unchanged).
    - ``topic`` the MQTT filter of the states; ``nodes`` the panels that count (empty: any node id the topic carries).
    - ``phaseTable`` phase of the panel -> SDK state; ``maxAgeS`` seconds of silence after which a panel is ``unknown``; ``hysteresisS`` seconds a
      "no alarm" must last before the inhibition is lifted.
    - ``unknownInhibits`` an unknown panel inhibits (default true). ``safeStop`` lets a table reach SAFE_STOP (default false).
    - ``auditLog`` file for the audit lines (``-``: standard error; empty: the decision log of the service is not touched and only memory keeps them).
    """

    enabled: bool = False
    topic: str = DEFAULT_TOPIC_FILTER
    nodes: tuple[str, ...] = ()
    phase_table: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_PHASE_TABLE))
    max_age_s: float = 60.0
    hysteresis_s: float = 10.0
    unknown_inhibits: bool = True
    safe_stop: bool = False
    audit_log: str = ""
    broker: BrokerSettings = field(default_factory=BrokerSettings)


def parse_settings(data: object) -> ArmorAlarmSettings:
    """Reads the optional ``armorAlarm`` object; absent means the defaults with the reading off. Anything unknown or malformed is refused."""
    if data is None:
        return ArmorAlarmSettings()
    if not isinstance(data, dict):
        raise ArmorAlarmConfigError("armorAlarm must be a JSON object")
    unknown = sorted(set(data) - _FIELDS)
    if unknown:
        raise ArmorAlarmConfigError(f"armorAlarm has unknown field(s): {unknown}")
    for key in ("enabled", "unknownInhibits", "safeStop"):
        if key in data and not isinstance(data[key], bool):
            raise ArmorAlarmConfigError(f"armorAlarm.{key} must be true or false, got {data[key]!r}")
    topic = data.get("topic", DEFAULT_TOPIC_FILTER)
    if not isinstance(topic, str) or not re.fullmatch(r"armor/alarm/(\+|[a-z0-9][a-z0-9_-]{0,63})/state", topic):
        raise ArmorAlarmConfigError("armorAlarm.topic must be armor/alarm/<node_id or +>/state")
    nodes_raw = data.get("nodes", [])
    if not isinstance(nodes_raw, list) or not all(isinstance(n, str) and _NODE_ID.fullmatch(n) for n in nodes_raw):
        raise ArmorAlarmConfigError("armorAlarm.nodes must be a list of node ids (lower-case letters, digits, dashes, underscores)")
    table_raw = data.get("phaseTable", dict(DEFAULT_PHASE_TABLE))
    if not isinstance(table_raw, dict) or not table_raw:
        raise ArmorAlarmConfigError("armorAlarm.phaseTable must be a non-empty object")
    for phase, state in table_raw.items():
        if not isinstance(phase, str) or not phase or state not in SAFETY_STATES:
            raise ArmorAlarmConfigError(f"armorAlarm.phaseTable maps {phase!r} to {state!r}, which is not one of {list(SAFETY_STATES)}")
    audit = data.get("auditLog", "")
    if not isinstance(audit, str):
        raise ArmorAlarmConfigError("armorAlarm.auditLog must be a text")
    broker = _parse_broker(data.get("broker"))
    return ArmorAlarmSettings(
        enabled=data.get("enabled", False),
        topic=topic,
        nodes=tuple(nodes_raw),
        phase_table=dict(table_raw),
        max_age_s=_number(data.get("maxAgeS", 60.0), "maxAgeS", 1.0, 86400.0),
        hysteresis_s=_number(data.get("hysteresisS", 10.0), "hysteresisS", 0.0, 3600.0),
        unknown_inhibits=data.get("unknownInhibits", True),
        safe_stop=data.get("safeStop", False),
        audit_log=audit,
        broker=broker,
    )


def _parse_broker(data: object) -> BrokerSettings:
    if data is None:
        return BrokerSettings()
    if not isinstance(data, dict):
        raise ArmorAlarmConfigError("armorAlarm.broker must be a JSON object")
    unknown = sorted(set(data) - _BROKER_FIELDS)
    if unknown:
        raise ArmorAlarmConfigError(f"armorAlarm.broker has unknown field(s): {unknown}")
    for key in ("host", "usernameFile", "passwordFile", "caFile", "clientId"):
        if key in data and (not isinstance(data[key], str) or (key == "host" and not data[key])):
            raise ArmorAlarmConfigError(f"armorAlarm.broker.{key} must be a text" + (" that is not empty" if key == "host" else ""))
    for key in ("tls", "internal"):
        if key in data and not isinstance(data[key], bool):
            raise ArmorAlarmConfigError(f"armorAlarm.broker.{key} must be true or false")
    port = data.get("port", 1883)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ArmorAlarmConfigError("armorAlarm.broker.port must be an integer from 1 to 65535")
    broker = BrokerSettings(
        host=data.get("host", "127.0.0.1"),
        port=port,
        username_file=data.get("usernameFile", ""),
        password_file=data.get("passwordFile", ""),
        tls=data.get("tls", False),
        ca_file=data.get("caFile", ""),
        internal=data.get("internal", False),
        client_id=data.get("clientId", "hydra-umc-safety-zones-armor"),
    )
    if not broker.is_local() and not broker.internal:
        # The panel's messages are not signed: a broker outside this machine is accepted only when the installation says it is its own network.
        raise ArmorAlarmConfigError(
            "armorAlarm.broker.host is not on this machine: the messages of the panel are not signed, so the broker must be declared internal (broker.internal: true)"
        )
    return broker


@dataclass(frozen=True)
class NodeStatus:
    node_id: str
    state: str  # clear | alarm | holding | unknown | fault
    inhibits: bool
    detail: str
    phase: str | None = None
    sdk_state: str | None = None
    last_message_age_s: float | None = None


@dataclass(frozen=True)
class ArmorAlarmStatus:
    """The answer for the cell. ``state``: disabled, clear, alarm, unknown (nothing heard or silent) or holding (clear but still inside the hysteresis)."""

    state: str
    inhibits: bool
    safe_stop: bool
    reason: str
    nodes: tuple[NodeStatus, ...] = ()

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "inhibits": self.inhibits,
            "safeStop": self.safe_stop,
            "reason": self.reason,
            "nodes": [
                {
                    "nodeId": n.node_id, "state": n.state, "inhibits": n.inhibits, "detail": n.detail, "phase": n.phase,
                    "sdkState": n.sdk_state, "lastMessageAgeS": n.last_message_age_s,
                }
                for n in self.nodes
            ],
        }


DISABLED_STATUS = ArmorAlarmStatus("disabled", False, False, "the reading of the ARMOR alarm is switched off (armorAlarm.enabled)")


class _Node:
    def __init__(self) -> None:
        self.phase: str | None = None
        self.sdk_state: str | None = None  # after the safeStop switch
        self.timestamp_ms: int | None = None
        self.received_at: float | None = None
        self.clear_since: float | None = None
        self.fault: str | None = None  # a corrupt/contradictory last message


def armor_alarm_to_sdk_state(message: Mapping[str, object], table: Mapping[str, str], topic_node: str | None = None) -> dict:
    """The SafetyState of an alarm message by ``table``: the same shape and rules as ``armor_alarm_to_safety_state`` of HYDRA-UMC-SDK.

    Raises ``ValueError`` for anything that is not a well-formed alarm message (the caller then inhibits).
    """
    if not isinstance(message, Mapping):
        raise ValueError("the message is not a JSON object")
    kind, node_id, stamp = message.get("kind"), message.get("node_id"), message.get("timestamp_ms")
    if kind != "alarm":
        raise ValueError(f"kind is {kind!r}, only a message of kind alarm has a phase")
    if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id):
        raise ValueError("node_id must be lower-case letters, digits, dashes or underscores")
    if topic_node is not None and node_id != topic_node:
        raise ValueError("the node_id of the message is not the one in its topic")
    if type(stamp) is not int or stamp < 0:
        raise ValueError("timestamp_ms must be an integer >= 0")
    phase = message.get("phase")
    if not isinstance(phase, str) or phase not in table:
        raise ValueError("phase is not one the table knows")
    try:
        when = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("timestamp_ms is out of range") from exc
    return {
        "schema_version": "1.0",
        "state": table[phase],
        "source": f"armor:{node_id}",
        "timestamp_utc": when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp % 1000:03d}Z",
        "armor_phase": phase,
    }


class ArmorAlarmGate:
    """One answer for the cell from the messages of the panels. Thread-safe; the clock (monotonic seconds) is an argument of every call."""

    def __init__(self, settings: ArmorAlarmSettings, audit: Callable[[dict], None] | None = None) -> None:
        self.settings = settings
        self._audit_sink = audit
        self._lock = threading.Lock()
        self._nodes: dict[str, _Node] = {n: _Node() for n in settings.nodes}
        self._last_status: tuple[str, bool] | None = None
        self.audit_records: list[dict] = []  # the newest 200, for the status route and the tests

    # -- input ----------------------------------------------------------------------------------------------------------------------

    def ingest(self, topic: str, payload: bytes | str | None, mono_now: float, retained: bool = False) -> None:
        """One MQTT message. It never raises: whatever is wrong with it is a fault of that panel (an inhibition), audited."""
        if not self.settings.enabled:
            return
        node_id = self._topic_node(topic)
        if node_id is None:
            self._audit("ignored", None, f"topic {topic!r} is not a state topic of the ARMOR alarm")
            return
        with self._lock:
            if self.settings.nodes and node_id not in self._nodes:
                self._audit("ignored", node_id, "node id is not in armorAlarm.nodes")
                return
            node = self._nodes.setdefault(node_id, _Node())
            if retained:
                self._audit("ignored", node_id, "a retained message is an old one and is never applied")
                return
            try:
                message = self._decode(payload)
                sdk = armor_alarm_to_sdk_state(message, self.settings.phase_table, node_id)
                stamp = message["timestamp_ms"]
                if node.timestamp_ms is not None and stamp < node.timestamp_ms:
                    raise ValueError(f"timestamp_ms {stamp} goes backwards from {node.timestamp_ms} (a replay or a clock that was reset)")
                if message.get("siren") is True and sdk["state"] == "READY":
                    raise ValueError("the siren sounds in a phase the table calls clear")
            except ValueError as exc:
                node.fault = str(exc)
                node.clear_since = None
                # a message that cannot be trusted still proves the panel talks, but it never counts as a "no alarm"
                node.received_at = mono_now
                self._audit("corrupt", node_id, str(exc))
                return
            node.fault = None
            node.timestamp_ms = stamp
            node.received_at = mono_now
            node.phase = sdk["armor_phase"]
            state = sdk["state"]
            if state == "SAFE_STOP" and not self.settings.safe_stop:
                self._audit("downgraded", node_id, "the table says SAFE_STOP but armorAlarm.safeStop is off: read as INHIBITED")
                state = "INHIBITED"
            node.sdk_state = state
            if state == "READY":
                if node.clear_since is None:
                    node.clear_since = mono_now
            else:
                node.clear_since = None

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
        return data

    def _topic_node(self, topic: str) -> str | None:
        parts = topic.split("/") if isinstance(topic, str) else []
        if len(parts) == 4 and parts[0] == "armor" and parts[1] == "alarm" and parts[3] == "state" and _NODE_ID.fullmatch(parts[2]):
            filter_node = self.settings.topic.split("/")[2]
            if filter_node in ("+", parts[2]):
                return parts[2]
        return None

    # -- output ---------------------------------------------------------------------------------------------------------------------

    def status(self, mono_now: float) -> ArmorAlarmStatus:
        """The answer now. Calling it also audits the changes (a lifted inhibition, a panel gone silent)."""
        if not self.settings.enabled:
            return DISABLED_STATUS
        s = self.settings
        with self._lock:
            nodes = [self._node_status(nid, node, mono_now) for nid, node in sorted(self._nodes.items())]
            if not nodes:
                result = ArmorAlarmStatus(
                    "unknown", s.unknown_inhibits, False,
                    "no message of the ARMOR alarm has arrived yet: no news is not 'no alarm'",
                )
            else:
                alarm = [n for n in nodes if n.state in ("alarm", "fault")]
                unknown = [n for n in nodes if n.state == "unknown"]
                holding = [n for n in nodes if n.state == "holding"]
                if alarm:
                    stop = s.safe_stop and any(n.sdk_state in ("SAFE_STOP",) for n in alarm)
                    names = ", ".join(f"{n.node_id}: {n.detail}" for n in alarm)
                    result = ArmorAlarmStatus("alarm", True, stop, f"ARMOR alarm: {names}", tuple(nodes))
                elif unknown:
                    names = ", ".join(f"{n.node_id}: {n.detail}" for n in unknown)
                    result = ArmorAlarmStatus("unknown", s.unknown_inhibits, False, f"ARMOR alarm state unknown: {names}", tuple(nodes))
                elif holding:
                    names = ", ".join(f"{n.node_id}: {n.detail}" for n in holding)
                    result = ArmorAlarmStatus("holding", True, False, f"ARMOR alarm cleared, waiting {s.hysteresis_s}s before lifting: {names}", tuple(nodes))
                else:
                    result = ArmorAlarmStatus("clear", False, False, "ARMOR reports no alarm", tuple(nodes))
            key = (result.state, result.inhibits)
            if key != self._last_status:
                self._audit("status", None, f"{result.state} (inhibits={result.inhibits}): {result.reason}")
                self._last_status = key
            return result

    def _node_status(self, node_id: str, node: _Node, now: float) -> NodeStatus:
        s = self.settings
        age = None if node.received_at is None else max(0.0, now - node.received_at)
        if node.received_at is None:
            return NodeStatus(node_id, "unknown", s.unknown_inhibits, "never heard from", None, None, None)
        if now < node.received_at or (age is not None and age > s.max_age_s):
            return NodeStatus(node_id, "unknown", s.unknown_inhibits, f"silent for more than {s.max_age_s:g}s (no news is not 'no alarm')", node.phase, node.sdk_state, age)
        if node.fault is not None:
            return NodeStatus(node_id, "fault", True, f"corrupt or contradictory message: {node.fault}", node.phase, node.sdk_state, age)
        if node.sdk_state != "READY":
            return NodeStatus(node_id, "alarm", True, f"phase {node.phase} -> {node.sdk_state}", node.phase, node.sdk_state, age)
        waited = 0.0 if node.clear_since is None else now - node.clear_since
        if waited < s.hysteresis_s:
            return NodeStatus(node_id, "holding", True, f"phase {node.phase}, clear for {waited:.1f}s of {s.hysteresis_s:g}s", node.phase, node.sdk_state, age)
        return NodeStatus(node_id, "clear", False, f"phase {node.phase}", node.phase, node.sdk_state, age)

    # -- audit ----------------------------------------------------------------------------------------------------------------------

    def _audit(self, event: str, node_id: str | None, detail: str) -> None:
        record = {"at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "source": "armor-alarm", "event": event, "node": node_id, "detail": detail}
        self.audit_records.append(record)
        del self.audit_records[:-200]
        if self._audit_sink is not None:
            try:
                self._audit_sink(record)
            except Exception:  # noqa: BLE001 - an audit that cannot be written never changes the answer
                pass


def combine(base_state: str, base_reason: str, status: ArmorAlarmStatus) -> tuple[str, str]:
    """The state of the verdict once the alarm is taken into account: ``(state, reason)`` with the states of ``SafetyState`` (ready/warning/danger/inhibited).

    A DANGER verdict stays as it is, and so does an INHIBITED one unless the safe stop is on. A READY verdict becomes INHIBITED when the alarm inhibits, and DANGER only when the safe stop is on and
    the table asked for it. WARNING stays WARNING (the SDK already reads it as INHIBITED).
    """
    if not status.inhibits or base_state == "danger":
        return base_state, base_reason
    if status.safe_stop:
        return "danger", status.reason
    if base_state == "inhibited":
        return base_state, base_reason
    if base_state == "ready":
        return "inhibited", status.reason
    return base_state, f"{base_reason}; {status.reason}"
