# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_serve_fatal_paths.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""`serve` refuses to start, with exit code 2 and a message that names the option, whenever a thing it was told to use is bad: a key file, a block of the
zones file, the CAN configuration. A service that starts half configured is worse than one that does not start, so each of these is a refusal before the
port is opened, and none prints the secret it was given."""
from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

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


def serve(*arguments: str) -> int:
    return main(["serve", "--port", str(free_port()), *arguments])


def test_a_state_key_file_that_is_missing_or_too_short_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert serve("--state-key-file", str(tmp_path / "missing")) == 2
    assert "--state-key-file" in capsys.readouterr().err
    short = tmp_path / "short"
    short.write_text("secret-too-short", encoding="utf-8")
    assert serve("--state-key-file", str(short)) == 2
    err = capsys.readouterr().err
    assert "--state-key-file" in err
    assert "secret-too-short" not in err


def test_an_ed25519_state_key_that_is_not_a_private_key_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    pytest.importorskip("cryptography")
    junk = tmp_path / "junk.pem"
    junk.write_text("not a pem file at all", encoding="utf-8")
    assert serve("--state-key-file", str(junk), "--state-signing-alg", "ed25519") == 2
    assert "--state-key-file" in capsys.readouterr().err
    assert serve("--state-key-file", str(tmp_path / "missing.pem"), "--state-signing-alg", "ed25519") == 2


def test_an_identity_key_that_is_short_or_missing_is_refused_without_echoing_it(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    short = tmp_path / "id"
    short.write_text("tiny-secret", encoding="utf-8")
    assert serve("--identity-key-file", str(short)) == 2
    err = capsys.readouterr().err
    assert "--identity-key-file" in err
    assert "tiny-secret" not in err
    assert serve("--identity-key-file", str(tmp_path / "missing")) == 2


@pytest.mark.parametrize("block", ["armorAlarm", "faultEvents", "zoneOccupancy", "recordTrigger"])
def test_a_block_of_the_zones_file_that_is_not_an_object_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture, block: str) -> None:
    path = zones_file(tmp_path, **{block: "on"})
    assert serve("--zones-file", str(path)) == 2
    err = capsys.readouterr().err
    assert "fatal" in err
    assert block in err or "invalid" in err


@pytest.mark.parametrize("block", ["armorAlarm", "faultEvents", "zoneOccupancy", "recordTrigger"])
def test_a_block_switched_on_with_nonsense_settings_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture, block: str) -> None:
    path = zones_file(tmp_path, **{block: {"enabled": True, "timeout_s": "forever", "max_age_s": -1, "port": "x", "topic": 7}})
    assert serve("--zones-file", str(path)) == 2
    assert "fatal" in capsys.readouterr().err


def test_a_stateful_run_with_nonsense_settings_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    path = zones_file(tmp_path, stateful={"enabled": True, "clear_after_s": "never", "hold_s": -3})
    assert serve("--zones-file", str(path)) == 2
    assert "fatal" in capsys.readouterr().err


def test_a_can_configuration_that_is_missing_or_off_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert serve("--can-estop-config", str(tmp_path / "missing.json")) == 2
    assert "--can-estop-config" in capsys.readouterr().err
    off = tmp_path / "can.json"
    off.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    assert serve("--can-estop-config", str(off)) == 2
    assert "enabled" in capsys.readouterr().err
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert serve("--can-estop-config", str(broken)) == 2


def test_a_zones_file_that_is_not_json_is_refused_with_its_name(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    path = tmp_path / "zones.json"
    path.write_text("{ not json", encoding="utf-8")
    assert serve("--zones-file", str(path)) == 2
    assert "could not load --zones-file" in capsys.readouterr().err


def test_a_port_that_is_taken_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        assert main(["serve", "--port", str(port)]) == 2
    assert "could not start the HTTP API" in capsys.readouterr().err
