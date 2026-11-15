# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_config_access.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The zones file lives in a directory that belongs to the service alone, the file of the earlier layout is copied once and never overwritten, an account
that cannot reach its zones file is told so before the service is stopped, and a missing or unreadable file is a refusal to start (never "no zones")."""
from __future__ import annotations

import json
import os
import re
import socket
import stat
import sys
from pathlib import Path

import pytest

from hydra_umc_safety_zones.config_access import (
    DEFAULT_ZONES_FILE,
    LEGACY_ZONES_FILE,
    check_access,
    diagnose_access,
    migrate_legacy_zones,
)
from hydra_umc_safety_zones.main import main

UNIT = Path(__file__).resolve().parent.parent / "systemd" / "hydra-umc-safety-zones.service"
POSIX_PERMISSIONS = sys.platform != "win32" and (not hasattr(os, "geteuid") or os.geteuid() != 0)
needs_permissions = pytest.mark.skipif(not POSIX_PERMISSIONS, reason="needs POSIX permissions and an account that is not root")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def write_zones(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"zones": [{"id": "cell", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 1, "y": 1, "z": 1}}]}), encoding="utf-8")
    return path


def unit_text() -> str:
    return UNIT.read_text(encoding="utf-8")


def test_the_unit_keeps_its_zones_in_a_directory_systemd_makes_for_the_service() -> None:
    text = unit_text()
    assert re.search(r"^ConfigurationDirectory=hydra-umc-safety-zones$", text, re.M)
    assert re.search(r"^ConfigurationDirectoryMode=0750$", text, re.M)
    serve = re.search(r"^ExecStart=.*$", text, re.M)
    assert serve is not None
    assert f"--zones-file {DEFAULT_ZONES_FILE}" in serve.group(0)
    assert DEFAULT_ZONES_FILE.startswith("/etc/hydra-umc-safety-zones/")


def test_the_service_account_never_has_to_enter_the_shared_directory() -> None:
    # /etc/hydra-umc is root:hydra-umc-agent 0750: the only things read from it are read by systemd as root (EnvironmentFile) or by the root migration step.
    for line in unit_text().splitlines():
        if line.startswith("#") or "/etc/hydra-umc/" not in line:
            continue
        assert line.startswith(("EnvironmentFile=-", "ExecStartPre=+")), line
        assert not line.startswith("ExecStart="), line


def test_the_migration_runs_as_root_before_the_check_that_runs_as_the_service() -> None:
    lines = [line for line in unit_text().splitlines() if line.startswith("ExecStartPre=")]
    assert len(lines) == 2
    assert lines[0].startswith("ExecStartPre=+") and "prepare-config" in lines[0]
    assert not lines[1].startswith("ExecStartPre=+") and "check-access" in lines[1]


def test_the_default_paths_are_the_ones_of_the_unit() -> None:
    text = unit_text()
    assert DEFAULT_ZONES_FILE in text
    assert LEGACY_ZONES_FILE in text


def test_the_earlier_zones_file_is_copied_once_with_its_signature(tmp_path: Path) -> None:
    legacy = write_zones(tmp_path / "etc" / "hydra-umc" / "safety-zones.zones.json")
    Path(f"{legacy}.sig").write_text("{}", encoding="utf-8")
    target = tmp_path / "etc" / "hydra-umc-safety-zones" / "zones.json"
    assert migrate_legacy_zones(str(legacy), str(target)) == "migrated"
    assert target.read_bytes() == legacy.read_bytes()
    assert Path(f"{target}.sig").read_text(encoding="utf-8") == "{}"
    assert legacy.exists() and Path(f"{legacy}.sig").exists()  # the old file is never deleted
    if POSIX_PERMISSIONS:
        assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_an_existing_zones_file_is_never_overwritten(tmp_path: Path) -> None:
    legacy = write_zones(tmp_path / "old" / "zones.json")
    target = tmp_path / "new" / "zones.json"
    target.parent.mkdir()
    target.write_text("kept", encoding="utf-8")
    assert migrate_legacy_zones(str(legacy), str(target)) == "kept"
    assert target.read_text(encoding="utf-8") == "kept"


def test_nothing_to_migrate_creates_nothing(tmp_path: Path) -> None:
    target = tmp_path / "new" / "zones.json"
    assert migrate_legacy_zones(str(tmp_path / "absent.json"), str(target)) == "none"
    assert not target.exists() and not target.parent.exists()


def test_prepare_config_reports_what_it_did_and_is_safe_to_repeat(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    legacy = write_zones(tmp_path / "old" / "zones.json")
    target = tmp_path / "new" / "zones.json"
    argv = ["prepare-config", "--zones-file", str(target), "--legacy-file", str(legacy), "--group", "no-such-group-here"]
    assert main(argv) == 0
    assert "migrated" in capsys.readouterr().out
    assert main(argv) == 0
    assert "kept" in capsys.readouterr().out


def test_prepare_config_without_an_earlier_file_still_exits_zero_and_serve_stays_closed(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    target = tmp_path / "new" / "zones.json"
    assert main(["prepare-config", "--zones-file", str(target), "--legacy-file", str(tmp_path / "absent")]) == 0
    assert "none" in capsys.readouterr().out
    assert main(["serve", "--port", str(free_port()), "--zones-file", str(target)]) == 2
    err = capsys.readouterr().err
    assert "could not load --zones-file" in err and "does not exist" in err


def test_a_readable_zones_file_passes_the_check_and_a_missing_one_fails_with_the_reason(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    zones = write_zones(tmp_path / "zones.json")
    assert check_access(str(zones))[0] == 0
    assert main(["check-access", "--zones-file", str(zones)]) == 0
    assert "readable" in capsys.readouterr().out
    assert main(["check-access", "--zones-file", str(tmp_path / "missing.json")]) == 2
    assert "does not exist" in capsys.readouterr().err


@needs_permissions
def test_a_directory_the_account_cannot_enter_is_named_and_serve_refuses_to_start(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    shared = tmp_path / "shared"
    zones = write_zones(shared / "service" / "zones.json")
    shared.chmod(0o000)
    try:
        code, message = check_access(str(zones))
        assert code == 2
        assert str(shared) in message and "cannot be entered" in message
        assert main(["check-access", "--zones-file", str(zones)]) == 2
        capsys.readouterr()
        assert main(["serve", "--port", str(free_port()), "--zones-file", str(zones)]) == 2
        err = capsys.readouterr().err
        assert "PermissionError" in err and str(shared) in err
    finally:
        shared.chmod(0o755)


@needs_permissions
def test_a_file_the_account_cannot_read_is_named(tmp_path: Path) -> None:
    zones = write_zones(tmp_path / "zones.json")
    zones.chmod(0o000)
    try:
        assert "cannot be read" in diagnose_access(str(zones))
        assert check_access(str(zones))[0] == 2
    finally:
        zones.chmod(0o644)


def test_a_readable_file_has_nothing_to_diagnose(tmp_path: Path) -> None:
    assert diagnose_access(str(write_zones(tmp_path / "zones.json"))) == ""
