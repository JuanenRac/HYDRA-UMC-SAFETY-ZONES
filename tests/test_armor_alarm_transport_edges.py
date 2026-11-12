# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_armor_alarm_transport_edges.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The MQTT side of the reading of the ARMOR alarm, in the ways the first tests do not reach: the library missing, the client the library makes, a user without a
password, TLS, the asynchronous connect, the shapes of a connection result and a stop that has nothing to stop. The broker is a stand-in; nothing is connected."""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from hydra_umc_safety_zones.armor_alarm import ArmorAlarmConfigError, ArmorAlarmGate, parse_settings
from hydra_umc_safety_zones.armor_alarm_mqtt import ArmorAlarmSubscriber


def gate(**broker) -> ArmorAlarmGate:
    return ArmorAlarmGate(parse_settings({"enabled": True, "requireSignature": False, "broker": broker}))


class Client:
    """A client with the calls the subscriber makes; `asynchronous` decides whether it has `connect_async`."""

    def __init__(self, asynchronous: bool = False) -> None:
        self.calls: list[tuple] = []
        if asynchronous:
            self.connect_async = self._connect_async  # type: ignore[assignment]

    def username_pw_set(self, user, password):
        self.calls.append(("credentials", user, password))

    def tls_set(self, ca_certs=None):
        self.calls.append(("tls", ca_certs))

    def reconnect_delay_set(self, **kwargs):
        self.calls.append(("delay", kwargs))

    def connect(self, host, port, keepalive):
        self.calls.append(("connect", host, port, keepalive))

    def _connect_async(self, host, port, keepalive=60):
        self.calls.append(("connect_async", host, port, keepalive))

    def loop_start(self):
        self.calls.append(("loop_start",))

    def loop_stop(self):
        self.calls.append(("loop_stop",))

    def disconnect(self):
        self.calls.append(("disconnect",))

    def subscribe(self, topic, qos=0):
        self.calls.append(("subscribe", topic, qos))


def test_without_the_library_the_start_says_what_to_install(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "paho", None)
    monkeypatch.setitem(sys.modules, "paho.mqtt", None)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", None)
    with pytest.raises(ArmorAlarmConfigError, match="needs paho-mqtt"):
        ArmorAlarmSubscriber(gate()).start()


def test_the_client_of_the_library_is_made_with_the_version_2_callbacks_and_the_client_id(monkeypatch) -> None:
    made: list[tuple] = []
    library = types.ModuleType("paho.mqtt.client")
    library.CallbackAPIVersion = SimpleNamespace(VERSION2="v2")  # type: ignore[attr-defined]

    def factory(version, client_id=None):
        made.append((version, client_id))
        return Client()

    library.Client = factory  # type: ignore[attr-defined]
    package, middle = types.ModuleType("paho"), types.ModuleType("paho.mqtt")
    package.mqtt, middle.client = middle, library  # type: ignore[attr-defined]
    for name, module in (("paho", package), ("paho.mqtt", middle), ("paho.mqtt.client", library)):
        monkeypatch.setitem(sys.modules, name, module)
    subscriber = ArmorAlarmSubscriber(gate(clientId="safety-cell-7"))
    subscriber.start()
    assert made == [("v2", "safety-cell-7")]
    assert isinstance(subscriber.client, Client)


def test_a_user_without_a_password_is_given_to_the_client_as_it_is(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.write_text("  safety\n", encoding="utf-8")
    client = Client()
    ArmorAlarmSubscriber(gate(usernameFile=str(user)), client=client).start()
    assert ("credentials", "safety", None) in client.calls


def test_a_missing_password_file_stops_the_start_before_anything_is_connected(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.write_text("safety", encoding="utf-8")
    client = Client()
    with pytest.raises(ArmorAlarmConfigError, match="passwordFile"):
        ArmorAlarmSubscriber(gate(usernameFile=str(user), passwordFile=str(tmp_path / "absent")), client=client).start()
    assert not any(call[0] in ("connect", "connect_async", "loop_start") for call in client.calls)


def test_no_user_means_no_credentials_and_no_tls_means_no_tls() -> None:
    client = Client()
    ArmorAlarmSubscriber(gate(), client=client).start()
    assert not any(call[0] in ("credentials", "tls") for call in client.calls)


def test_tls_is_set_with_the_authority_of_the_settings_or_with_none() -> None:
    plain = Client()
    ArmorAlarmSubscriber(gate(host="10.0.0.5", internal=True, tls=True), client=plain).start()
    assert ("tls", None) in plain.calls
    with_authority = Client()
    ArmorAlarmSubscriber(gate(host="10.0.0.5", internal=True, tls=True, caFile="/etc/ssl/broker-ca.pem"), client=with_authority).start()
    assert ("tls", "/etc/ssl/broker-ca.pem") in with_authority.calls


def test_the_client_that_can_connect_in_the_background_does_and_the_other_connects_at_once() -> None:
    background = Client(asynchronous=True)
    ArmorAlarmSubscriber(gate(host="10.0.0.5", internal=True, port=8883), client=background).start()
    assert ("connect_async", "10.0.0.5", 8883, 30) in background.calls
    assert not any(call[0] == "connect" for call in background.calls)
    blocking = Client()
    ArmorAlarmSubscriber(gate(), client=blocking).start()
    assert ("connect", "127.0.0.1", 1883, 30) in blocking.calls
    for client in (background, blocking):
        assert client.calls[-1] == ("loop_start",), "the loop starts last, after everything is set"
        assert ("delay", {"min_delay": 1, "max_delay": 30}) in client.calls


def test_a_connection_result_is_a_success_only_when_it_says_so() -> None:
    cases = [
        (0, True), (None, True), (5, False), (135, False),
        (SimpleNamespace(is_failure=False), True), (SimpleNamespace(is_failure=True), False),
        (SimpleNamespace(), False),  # an object with no verdict is not a zero: it is not trusted
    ]
    for code, expected in cases:
        client = Client()
        subscriber = ArmorAlarmSubscriber(gate(), client=client)
        subscriber._on_connect(client, None, {}, code)  # noqa: SLF001
        assert subscriber.connected is expected, repr(code)
        assert (("subscribe", "armor/alarm/+/state", 1) in client.calls) is expected, repr(code)


def test_a_disconnect_clears_the_connected_flag_whatever_arguments_come() -> None:
    client = Client()
    subscriber = ArmorAlarmSubscriber(gate(), client=client)
    subscriber._on_connect(client, None, {}, 0)  # noqa: SLF001
    assert subscriber.connected
    subscriber._on_disconnect(client, None, {}, 7, None)  # noqa: SLF001
    assert not subscriber.connected


def test_a_message_without_a_retain_flag_is_a_live_one() -> None:
    seen: list[tuple] = []
    alarm = gate()
    alarm.ingest = lambda topic, payload, now, retained=False: seen.append((topic, payload, retained))  # type: ignore[method-assign]
    subscriber = ArmorAlarmSubscriber(alarm, clock=lambda: 12.5, client=Client())
    subscriber._on_message(None, None, SimpleNamespace(topic="armor/alarm/a/state", payload=b"{}"))  # noqa: SLF001
    subscriber._on_message(None, None, SimpleNamespace(topic="armor/alarm/a/state", payload=b"{}", retain=True))  # noqa: SLF001
    assert seen == [("armor/alarm/a/state", b"{}", False), ("armor/alarm/a/state", b"{}", True)]


def test_stopping_a_subscriber_that_never_started_is_not_an_error_and_a_started_one_stops_its_loop_then_disconnects() -> None:
    ArmorAlarmSubscriber(gate()).stop()
    client = Client()
    subscriber = ArmorAlarmSubscriber(gate(), client=client)
    subscriber.start()
    subscriber._on_connect(client, None, {}, 0)  # noqa: SLF001
    subscriber.stop()
    assert client.calls[-2:] == [("loop_stop",), ("disconnect",)]
    assert subscriber.connected is False
