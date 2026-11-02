# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/armor_alarm_mqtt.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The MQTT side of the reading of the ARMOR alarm: subscribes to ``armor/alarm/+/state`` and hands every message to the ``ArmorAlarmGate``.

paho-mqtt is optional (``pip install "hydra-umc-safety-zones[mqtt]"``) and imported only when the reading is switched on. The credentials are read from files
(never from the command line, never printed). The transport only listens: it publishes nothing to the broker. A client that is given (a test hands in a
fake) replaces paho, so no test needs a broker.

When the link to the broker goes away the gate is not told "no alarm": it is told nothing, and the silence turns the panels ``unknown`` after ``maxAgeS``.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hydra_umc_safety_zones.armor_alarm import ArmorAlarmConfigError, ArmorAlarmGate


def _read_secret(path: str, what: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ArmorAlarmConfigError(f"armorAlarm.broker.{what} cannot be read: {exc.strerror or type(exc).__name__}") from exc


class ArmorAlarmSubscriber:
    """Connects to the broker of the settings and feeds the gate. ``client`` is a paho-like object (``connect``, ``subscribe``, ``loop_start``, ``loop_stop``...)."""

    def __init__(self, gate: ArmorAlarmGate, clock: Callable[[], float] = time.monotonic, client: Any = None) -> None:
        self.gate = gate
        self.clock = clock
        self.client = client
        self.connected = False

    def start(self) -> None:
        settings = self.gate.settings
        broker = settings.broker
        if self.client is None:
            try:
                import paho.mqtt.client as mqtt  # noqa: PLC0415
            except ImportError as exc:
                raise ArmorAlarmConfigError('the reading of the ARMOR alarm needs paho-mqtt (pip install "hydra-umc-safety-zones[mqtt]")') from exc
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=broker.client_id)
        client = self.client
        if broker.username_file:
            password = _read_secret(broker.password_file, "passwordFile") if broker.password_file else None
            client.username_pw_set(_read_secret(broker.username_file, "usernameFile"), password)
        if broker.tls:
            client.tls_set(ca_certs=broker.ca_file or None)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect_async(broker.host, broker.port, keepalive=30) if hasattr(client, "connect_async") else client.connect(broker.host, broker.port, 30)
        client.loop_start()

    def stop(self) -> None:
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()
        self.connected = False

    # paho API v2 callbacks (extra arguments are tolerated so a fake can call them simply)
    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
        ok = getattr(reason_code, "is_failure", None)
        self.connected = not (ok if isinstance(ok, bool) else reason_code not in (0, None))
        if self.connected:
            client.subscribe(self.gate.settings.topic, qos=1)

    def _on_disconnect(self, client: Any, userdata: Any, *args: Any) -> None:
        self.connected = False

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        self.gate.ingest(message.topic, message.payload, self.clock(), retained=bool(getattr(message, "retain", False)))
