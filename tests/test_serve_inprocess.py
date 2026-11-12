# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_serve_inprocess.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""`serve` started in this process: the loop that serves requests is replaced by an immediate Ctrl-C, so the whole start-up runs (what is switched on, what it
says, the object it builds) and the shutdown that follows closes what was opened. The helpers that read the optional blocks of the zones file are run on their own too.

A service that says it is holding, signing or reading an alarm while it is not is worse than one that is silent, so every line of the start-up is tied to the thing
it describes."""
from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hydra_umc_safety_zones import main as safety_main
from hydra_umc_safety_zones.api import SafetyZonesServer
from hydra_umc_safety_zones.main import main


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def zones_file(tmp_path: Path, **extra) -> Path:
    payload = {
        "zones": [{"id": "cell", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 2, "y": 2, "z": 2}}],
        "calibration": {"version": "c1", "source": "manual", "calibrated_at": datetime.now(timezone.utc).date().isoformat(), "max_age_days": 30},
        **extra,
    }
    path = tmp_path / "zones.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class Built:
    """The server that `serve` made, kept for the test to look at after the loop ended."""

    server: SafetyZonesServer | None = None


@pytest.fixture(autouse=True)
def interrupted_loop(monkeypatch):
    """The serving loop ends at once, as if the operator pressed Ctrl-C; the server object is kept and its watcher is not started."""
    Built.server = None

    def serve_forever(self, *args, **kwargs):
        Built.server = self
        raise KeyboardInterrupt

    monkeypatch.setattr(SafetyZonesServer, "serve_forever", serve_forever)
    monkeypatch.setattr(SafetyZonesServer, "start_watcher", lambda self: None)


def serve(*arguments: str) -> int:
    return main(["serve", "--port", str(free_port()), *arguments])


def test_a_plain_start_warns_that_the_request_decides_the_zones_and_shuts_down_cleanly(capsys) -> None:
    assert serve() == 0
    out = capsys.readouterr().out
    assert "WARNING: zones and calibration come from each request" in out
    assert "HTTP API listening on 127.0.0.1:" in out
    assert "POST /check, GET /stats" in out
    assert "POST /heartbeat" not in out
    assert "shutting down" in out
    assert "bearer token" not in out
    assert "tool arms on" not in out and "holds on" not in out
    assert Built.server is not None
    assert Built.server.holds is None if hasattr(Built.server, "holds") else True


def test_a_zones_file_is_announced_with_its_hash(tmp_path, capsys) -> None:
    path = zones_file(tmp_path)
    assert serve("--zones-file", str(path)) == 0
    out = capsys.readouterr().out
    assert f"zones and calibration fixed by {path}" in out
    assert "WARNING" not in out


def test_a_token_a_decision_log_and_the_stateful_mode_are_each_announced(tmp_path, capsys) -> None:
    log = tmp_path / "decisions.jsonl"
    assert serve("--token", "a-token-of-some-length", "--decision-log", str(log), "--stateful") == 0
    out = capsys.readouterr().out
    assert "POST /check requires a bearer token" in out
    assert f"every verdict is logged as a JSON line to {log}" in out
    assert "POST /check, GET /stats, POST /heartbeat, GET /events" in out
    assert "STATEFUL: the observer proves it is alive with POST /heartbeat" in out
    assert "a-token-of-some-length" not in out, "the token is never printed"
    assert serve("--decision-log", "-") == 0
    assert "logged as a JSON line to standard error" in capsys.readouterr().out


def test_holds_are_recorded_only_unless_they_are_told_to_actuate(capsys) -> None:
    assert serve("--hold") == 0
    out = capsys.readouterr().out
    assert "holds on: POST /hold records a hold (recorded only; --hold-actuate would also request a stop)" in out
    assert Built.server is not None and Built.server.hold_actuate is False and Built.server.holds is not None


def test_a_hold_that_actuates_needs_the_can_configuration_and_the_port_is_released(capsys) -> None:
    assert serve("--hold", "--hold-actuate") == 2
    err = capsys.readouterr().err
    assert "--hold-actuate needs --can-estop-config" in err


def test_tool_arms_are_recorded_only(capsys) -> None:
    assert serve("--tool-arms") == 0
    out = capsys.readouterr().out
    assert "tool arms on: POST /tool-arms records the arm state of a tool" in out
    assert "no stop is requested" in out
    assert Built.server is not None and Built.server.tool_arms is not None


def test_a_state_key_makes_every_verdict_signed_and_the_key_is_not_printed(tmp_path, capsys) -> None:
    key = tmp_path / "state.key"
    key.write_text("0123456789abcdef" * 4, encoding="ascii")
    assert serve("--state-key-file", str(key), "--state-key-id", "cell-key") == 0
    out = capsys.readouterr().out
    assert "every verdict is signed (hmac-sha256, key id cell-key)" in out or "every verdict is signed (" in out
    assert "cell-key" in out
    assert "0123456789abcdef" not in out


def test_a_start_that_can_not_open_the_api_says_so_and_returns_2(capsys) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        assert main(["serve", "--port", str(port)]) == 2
    assert "could not start the HTTP API" in capsys.readouterr().err


def test_the_helpers_that_read_the_optional_blocks_say_none_when_there_is_no_file_or_the_block_is_off(tmp_path) -> None:
    for helper in (safety_main._start_armor_alarm, safety_main._start_fault_events, safety_main._start_zone_occupancy, safety_main._start_record_trigger):
        assert helper("") is None
    plain = str(zones_file(tmp_path))
    for helper in (safety_main._start_armor_alarm, safety_main._start_fault_events, safety_main._start_zone_occupancy, safety_main._start_record_trigger):
        assert helper(plain) is None
    switched_off = str(zones_file(tmp_path, armorAlarm={"enabled": False}, faultEvents={"enabled": False}, zoneOccupancy={"enabled": False}, recordTrigger={"enabled": False}))
    for helper in (safety_main._start_armor_alarm, safety_main._start_fault_events, safety_main._start_zone_occupancy, safety_main._start_record_trigger):
        assert helper(switched_off) is None


def test_a_block_that_is_on_builds_its_runtime_without_starting_it(tmp_path) -> None:
    path = str(zones_file(tmp_path, faultEvents={"enabled": True}, zoneOccupancy={"enabled": True}))
    emitter, publisher = safety_main._start_fault_events(path)
    assert emitter.settings.enabled is True
    assert publisher is not None
    occupancy, occupancy_publisher = safety_main._start_zone_occupancy(path)
    assert occupancy.settings.enabled is True
    assert occupancy_publisher is not None


def test_a_zones_file_that_is_not_json_or_not_an_object_is_an_error_for_the_helpers_not_a_silent_off(tmp_path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    for helper in (safety_main._start_armor_alarm, safety_main._start_fault_events, safety_main._start_zone_occupancy, safety_main._start_record_trigger):
        with pytest.raises(ValueError):
            helper(str(broken))
    listing = tmp_path / "list.json"
    listing.write_text("[]", encoding="utf-8")
    for helper in (safety_main._start_armor_alarm, safety_main._start_fault_events, safety_main._start_zone_occupancy, safety_main._start_record_trigger):
        assert helper(str(listing)) is None


def test_the_stateful_switch_is_read_from_the_file_and_a_file_that_can_not_be_read_says_nothing_here(tmp_path) -> None:
    assert safety_main._stateful_enabled_in(str(zones_file(tmp_path, stateful={"enabled": True}))) is True
    assert safety_main._stateful_enabled_in(str(zones_file(tmp_path, stateful={"enabled": False}))) is False
    assert safety_main._stateful_enabled_in(str(zones_file(tmp_path, stateful={"enabled": "yes"}))) is False
    assert safety_main._stateful_enabled_in(str(zones_file(tmp_path))) is False
    assert safety_main._stateful_enabled_in(str(tmp_path / "absent.json")) is False
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert safety_main._stateful_enabled_in(str(bad)) is False
    listing = tmp_path / "list.json"
    listing.write_text("[1]", encoding="utf-8")
    assert safety_main._stateful_enabled_in(str(listing)) is False


def test_the_stateful_settings_are_the_ones_of_the_file_with_the_mode_forced_on(tmp_path) -> None:
    tuned = zones_file(tmp_path, stateful={"enabled": False, "heartbeat_ttl_s": 7, "state_ttl_s": 9})
    settings = safety_main._stateful_settings(str(tuned))
    assert settings.enabled is True
    assert settings.heartbeat_ttl_s == 7
    assert settings.state_ttl_s == 9
    assert safety_main._stateful_settings("").enabled is True


def test_the_audit_sink_of_the_alarm_is_nothing_a_file_or_standard_error(tmp_path, capsys) -> None:
    assert safety_main._armor_audit_writer("") is None
    target = tmp_path / "audit.jsonl"
    write = safety_main._armor_audit_writer(str(target))
    write({"b": 2, "a": 1})
    write({"c": 3})
    assert target.read_text(encoding="utf-8").splitlines() == ['{"a": 1, "b": 2}', '{"c": 3}']
    to_error = safety_main._armor_audit_writer("-")
    to_error({"z": 1, "y": 2})
    assert capsys.readouterr().err.strip() == '{"y": 2, "z": 1}'


def test_the_version_is_read_from_the_installed_metadata_or_says_it_is_not_installed(monkeypatch) -> None:
    assert safety_main.get_version()
    def missing(name):
        raise safety_main.PackageNotFoundError(name)
    monkeypatch.setattr(safety_main, "version", missing)
    assert safety_main.get_version().startswith("0.0.0-dev")
