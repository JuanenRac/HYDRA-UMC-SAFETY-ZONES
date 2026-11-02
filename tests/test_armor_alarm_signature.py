# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_armor_alarm_signature.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The signature of the state of the ARMOR alarm: a state that is not signed by A.R.M.O.R. never changes the answer of the cell.

The vectors are the ones A.R.M.O.R. shares with every reader of its signed states (the same canonical bytes and the same signatures); the messages, the clock
and the key file are simulated. Nothing here was tried with a real alarm, a real panel or a real broker."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from hydra_umc_safety_zones.armor_alarm import (
    ArmorAlarmConfigError,
    ArmorAlarmGate,
    ArmorAlarmSettings,
    alarm_state_canonical,
    alarm_state_signature,
    parse_settings,
)

VECTORS = Path(__file__).resolve().parent / "data" / "armor_alarm_state_signature_vectors.json"
SIBLING = Path(__file__).resolve().parents[2] / "ARMOR-COMMON" / "conformance" / "signatures" / "alarm_state.json"
DOCUMENT = json.loads(VECTORS.read_text(encoding="utf-8"))
KEY = DOCUMENT["key"]
TOPIC = "armor/alarm/alarm-1/state"
NOW_MS = 1_790_000_100_000


def state(phase: str = "armed", ts: int = 1000, **extra: object) -> dict:
    body: dict = {
        "kind": "alarm", "node_id": "alarm-1", "timestamp_ms": ts, "phase": phase, "mode": "disarmed" if phase == "disarmed" else "away", "siren": phase == "alarm",
        "locked_out": False, "commands_enabled": True,
        "zones": [{"id": "front-door", "kind": "entry", "state": "normal", "bypassed": False}], "open_zones": [], "events": [],
    }
    body.update(extra)
    return body


def signed(message: dict, seq: int = NOW_MS, key: str = KEY) -> bytes:
    body = dict(message, seq=seq)
    body["sig"] = alarm_state_signature(key, body, seq)
    return json.dumps(body).encode()


def gate(**kw: object) -> ArmorAlarmGate:
    settings = ArmorAlarmSettings(enabled=True, hysteresis_s=0.0, **kw)  # type: ignore[arg-type]
    return ArmorAlarmGate(settings, wall_clock=lambda: NOW_MS / 1000.0, key_loader=lambda: KEY)


# -- the shared vectors ----------------------------------------------------------------------------------------------------------


def test_the_vectors_are_the_ones_armor_publishes() -> None:
    if not SIBLING.is_file():
        pytest.skip("ARMOR-COMMON is not checked out next to this repository")
    assert VECTORS.read_bytes().replace(b"\r\n", b"\n") == SIBLING.read_bytes().replace(b"\r\n", b"\n"), "the copy of the shared vectors is out of date"


def test_the_canonical_bytes_and_the_signatures_are_the_shared_ones() -> None:
    assert len(DOCUMENT["cases"]) >= 5
    for case in DOCUMENT["cases"]:
        message, seq = case["message"], case["message"]["seq"]
        assert alarm_state_canonical(message, seq).decode("utf-8") == case["canonical"], case["name"]
        assert alarm_state_signature(KEY, message, seq) == case["sig"], case["name"]
        # the same value computed by hand, without this module
        assert hmac.new(bytes.fromhex(KEY), case["canonical"].encode(), hashlib.sha256).hexdigest() == case["sig"], case["name"]


def test_an_altered_message_does_not_verify() -> None:
    for case in DOCUMENT["tampered"]:
        message = case["message"]
        assert alarm_state_signature(KEY, message, message["seq"]) != case["sig"], case["name"]


def test_what_the_signature_does_not_cover_is_refused_when_it_is_not_of_the_contract_form() -> None:
    base = DOCUMENT["cases"][0]["message"]
    for bad in (
        dict(base, siren="no"), dict(base, timestamp_ms=True), dict(base, timestamp_ms=1.5), dict(base, phase="Armed|x"),
        dict(base, zones=[{"id": "a,b", "state": "normal", "bypassed": False}]), dict(base, zones="x"), dict(base, open_zones=["A"]),
        {k: v for k, v in base.items() if k != "mode"},
    ):
        with pytest.raises(ValueError):
            alarm_state_canonical(bad, 1)
    with pytest.raises(ValueError):
        alarm_state_signature("short", base, 1)


# -- the gate --------------------------------------------------------------------------------------------------------------------


def test_a_signed_state_is_taken_and_a_signed_alarm_inhibits() -> None:
    g = gate()
    g.ingest(TOPIC, signed(state("armed")), 0.0)
    assert g.status(1.0).state == "clear" and g.status(1.0).inhibits is False
    g.ingest(TOPIC, signed(state("alarm", ts=2000), seq=NOW_MS + 1), 2.0)
    assert g.status(3.0).state == "alarm" and g.status(3.0).inhibits is True
    assert g.refused == dict.fromkeys(g.refused, 0)


def test_the_signature_is_required_by_default_and_an_unsigned_state_changes_nothing() -> None:
    g = gate()
    g.ingest(TOPIC, json.dumps(state("armed")).encode(), 0.0)
    status = g.status(1.0)
    assert status.state == "unknown" and status.inhibits is True, "no authenticated news is not 'no alarm'"
    assert g.refused["unsigned"] == 1
    assert status.as_dict()["signature"] == {"required": True, "refused": dict(g.refused), "refusedTotal": 1}


def test_an_unsigned_message_never_lifts_an_inhibition_nor_keeps_a_panel_alive() -> None:
    g = gate(max_age_s=30.0)
    g.ingest(TOPIC, signed(state("alarm", ts=1000), seq=NOW_MS), 0.0)
    assert g.status(1.0).inhibits is True
    # a forged "no alarm", unsigned, with a wrong key, with the signature of another message, and an old signed one replayed
    forged = [
        json.dumps(state("disarmed", ts=5000)).encode(),
        signed(state("disarmed", ts=5000), seq=NOW_MS + 5, key="ff" * 32),
        json.dumps(dict(state("disarmed", ts=5000), seq=NOW_MS + 6, sig=json.loads(signed(state("alarm", ts=1000), seq=NOW_MS))["sig"])).encode(),
        signed(state("armed", ts=900), seq=NOW_MS - 10),
    ]
    for index, payload in enumerate(forged):
        g.ingest(TOPIC, payload, 2.0 + index)
        assert g.status(2.5 + index).state == "alarm" and g.status(2.5 + index).inhibits is True, index
    assert sum(g.refused.values()) == len(forged)
    # the refused messages did not refresh the liveness: after maxAgeS of real silence the panel is unknown, which inhibits
    status = g.status(100.0)
    assert status.state == "unknown" and status.inhibits is True


def test_a_message_changed_after_it_was_signed_is_refused() -> None:
    g = gate()
    body = json.loads(signed(state("alarm", ts=2000)))
    body.update(phase="disarmed", mode="disarmed", siren=False)   # the signature is the one of the alarm
    g.ingest(TOPIC, json.dumps(body).encode(), 0.0)
    assert g.refused["invalid"] == 1 and g.status(1.0).state == "unknown"
    body = json.loads(signed(state("alarm", ts=2000)))
    body["zones"][0]["state"] = "tamper"
    g.ingest(TOPIC, json.dumps(body).encode(), 0.5)
    assert g.refused["invalid"] == 2


def test_seq_has_to_go_up_and_stay_near_the_clock_of_this_machine() -> None:
    g = gate()
    g.ingest(TOPIC, signed(state("armed", ts=1000), seq=NOW_MS), 0.0)
    g.ingest(TOPIC, signed(state("armed", ts=1100), seq=NOW_MS), 1.0)          # the same seq again
    g.ingest(TOPIC, signed(state("armed", ts=1200), seq=NOW_MS - 1), 2.0)      # a lower one
    assert g.refused["replay"] == 2
    g.ingest(TOPIC, signed(state("armed", ts=1300), seq=NOW_MS + 1), 3.0)      # above: taken
    assert g.refused["replay"] == 2
    g.ingest(TOPIC, signed(state("armed", ts=1400), seq=NOW_MS + 301_000), 4.0)   # five minutes and a second ahead
    g.ingest("armor/alarm/alarm-2/state", signed(dict(state("armed"), node_id="alarm-2"), seq=NOW_MS - 400_000), 5.0)   # and one that is old
    assert g.refused["skew"] == 2
    off = gate(signature_max_skew_s=0.0)
    off.ingest(TOPIC, signed(state("armed"), seq=5), 0.0)                       # the check of the clock is a setting
    assert off.refused["skew"] == 0 and off.status(1.0).state == "clear"


def test_a_node_id_in_the_message_that_is_not_the_one_of_the_topic_is_refused() -> None:
    g = gate()
    g.ingest("armor/alarm/alarm-2/state", signed(state("armed")), 0.0)   # signed for alarm-1, said on the topic of alarm-2
    assert g.status(1.0).inhibits is True


def test_a_bad_signature_field_is_refused_in_every_shape() -> None:
    g = gate()
    sig = json.loads(signed(state("armed")))["sig"]
    for index, extra in enumerate((
        {"seq": NOW_MS}, {"sig": sig}, {"seq": str(NOW_MS), "sig": sig}, {"seq": True, "sig": sig}, {"seq": -1, "sig": sig}, {"seq": 1.5, "sig": sig},
        {"seq": NOW_MS, "sig": sig.upper()}, {"seq": NOW_MS, "sig": sig[:-1]}, {"seq": NOW_MS, "sig": None}, {"seq": 2 ** 60, "sig": sig},
    )):
        g.ingest(TOPIC, json.dumps(dict(state("armed"), **extra)).encode(), float(index))
    for payload in (b"", b"[]", b"not json", b'{"seq": NaN}', b"x" * 70000):
        g.ingest(TOPIC, payload, 20.0)
    assert g.status(21.0).state == "unknown"
    assert sum(g.refused.values()) == 15


def test_without_a_usable_key_everything_is_refused_and_inhibits(tmp_path: Path) -> None:
    for content in (None, "", "short", "zz" * 32, KEY.upper()):
        path = tmp_path / "key.txt"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        elif path.exists():
            path.unlink()
        g = ArmorAlarmGate(ArmorAlarmSettings(enabled=True, signature_key_file=str(path)), wall_clock=lambda: NOW_MS / 1000.0)
        g.ingest(TOPIC, signed(state("armed")), 0.0)
        assert g.refused["key"] == 1, content
        assert g.status(1.0).state == "unknown" and g.status(1.0).inhibits is True
        assert all(KEY not in json.dumps(r) for r in g.audit_records)


def test_the_key_is_read_from_the_file_and_never_written_in_the_audit(tmp_path: Path) -> None:
    path = tmp_path / "alarm-state.key"
    path.write_text(KEY + "\n", encoding="utf-8")
    lines: list[dict] = []
    g = ArmorAlarmGate(ArmorAlarmSettings(enabled=True, hysteresis_s=0.0, signature_key_file=str(path)), lines.append, wall_clock=lambda: NOW_MS / 1000.0)
    g.ingest(TOPIC, signed(state("armed")), 0.0)
    assert g.status(1.0).state == "clear"
    g.ingest(TOPIC, signed(state("armed", ts=1100), seq=NOW_MS + 1, key="aa" * 32), 2.0)
    path.write_text("bb" * 32, encoding="utf-8")                       # the key is changed on disk: the next message is judged by the new one
    g.ingest(TOPIC, signed(state("armed", ts=1200), seq=NOW_MS + 2), 3.0)
    assert g.refused["invalid"] == 2
    text = json.dumps(lines) + json.dumps(g.audit_records)
    assert KEY not in text and "bb" * 32 not in text and "aa" * 32 not in text


def test_the_audit_of_refused_messages_is_written_once_a_minute_per_panel_and_reason() -> None:
    lines: list[dict] = []
    g = ArmorAlarmGate(ArmorAlarmSettings(enabled=True), lines.append, wall_clock=lambda: NOW_MS / 1000.0, key_loader=lambda: KEY)
    for second in range(0, 130):
        g.ingest(TOPIC, json.dumps(state("armed")).encode(), float(second))
    refused = [line for line in lines if line["event"] == "refused"]
    assert g.refused["unsigned"] == 130
    assert len(refused) == 3 and "more like it" in refused[1]["detail"]


def test_with_the_signature_not_required_the_unsigned_state_is_read_as_before() -> None:
    g = gate(require_signature=False)
    g.ingest(TOPIC, json.dumps(state("armed")).encode(), 0.0)
    assert g.status(1.0).state == "clear"
    assert g.status(1.0).as_dict()["signature"]["required"] is False


# -- the settings ----------------------------------------------------------------------------------------------------------------


def test_the_settings_require_the_signature_by_default_and_name_the_key_file() -> None:
    s = parse_settings({"enabled": True, "signatureKeyFile": "/etc/hydra-umc/alarm-state.key"})
    assert s.require_signature is True and s.signature_key_file == "/etc/hydra-umc/alarm-state.key" and s.signature_max_skew_s == 300.0
    assert parse_settings(None).enabled is False and parse_settings({}).require_signature is True
    assert parse_settings({"enabled": True, "requireSignature": False}).require_signature is False
    assert parse_settings({"enabled": True, "signatureKeyFile": "k", "signatureMaxSkewS": 0}).signature_max_skew_s == 0.0
    # a signed state does not depend on where the broker is, an unsigned one does
    assert parse_settings({"enabled": True, "signatureKeyFile": "k", "broker": {"host": "10.0.0.5"}}).broker.host == "10.0.0.5"
    with pytest.raises(ArmorAlarmConfigError):
        parse_settings({"enabled": True, "requireSignature": False, "broker": {"host": "10.0.0.5"}})
    with pytest.raises(ArmorAlarmConfigError):
        parse_settings({"enabled": True})
