# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_cell_state_file.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The writer of the file that the bridges read as the state of the cell: when the word is READY, every way it fails closed, the settings, and the running service.

The file is read here the way the bridges read it (first word of the file, a missing, unknown or old file is SAFE_STOP). Nothing here has run against a real bridge.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from hydra_umc_safety_zones.api import SafetyZonesServer
from hydra_umc_safety_zones.cell_state_file import (
    DEFAULT_PATH,
    CellStateFileConfigError,
    CellStateFileSettings,
    CellStateWriter,
    parse_cell_state_file_settings,
    write_word,
)


def bridge_reads(path: Path, max_age: float = 30.0, clock: Any = time.time) -> str:
    """The reading of ``cell_state_reader`` of the bridges: the first word of a fresh file, else SAFE_STOP."""
    try:
        age = clock() - os.stat(path).st_mtime
        if age > max_age or age < -max_age:
            return "SAFE_STOP"
        words = path.read_text(encoding="utf-8")[:256].split()
    except (OSError, UnicodeDecodeError):
        return "SAFE_STOP"
    word = words[0] if words else None
    return word if word in ("READY", "INHIBITED", "FAULT", "SAFE_STOP") else "SAFE_STOP"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def verdict(state: str) -> dict[str, Any]:
    return {"sdkSafetyState": {"state": state}, "reason": "x"}


def make(tmp_path: Path, **kwargs: Any) -> tuple[CellStateWriter, Clock, Path]:
    clock = Clock()
    path = tmp_path / "cell_state"
    settings = CellStateFileSettings(enabled=True, path=str(path))
    return CellStateWriter(settings, clock=clock, **kwargs), clock, path


# -- the settings -----------------------------------------------------------------------------------------------------------------

def test_it_is_off_by_default_and_the_defaults_are_the_conservative_ones() -> None:
    settings = parse_cell_state_file_settings(None)
    assert settings == CellStateFileSettings() and settings.enabled is False
    assert settings.path == DEFAULT_PATH == "/run/hydra-umc/cell_state"
    assert settings.refresh_s * 3 <= settings.max_age_s and settings.verdict_max_age_s <= settings.max_age_s
    assert parse_cell_state_file_settings({}).enabled is False


def test_the_settings_come_from_the_zones_file_and_bad_ones_are_refused() -> None:
    settings = parse_cell_state_file_settings({"enabled": True, "path": "/run/x/cell", "refreshS": 1, "verdictMaxAgeS": 3, "maxAgeS": 10})
    assert (settings.enabled, settings.path, settings.refresh_s, settings.verdict_max_age_s, settings.max_age_s) == (True, "/run/x/cell", 1.0, 3.0, 10.0)
    bad: list[Any] = [
        "yes", {"enabled": "yes"}, {"extra": 1}, {"path": "relative"}, {"path": "/run/../etc/x"}, {"path": "/run/dir/"}, {"path": 3}, {"path": "/a\0b"},
        {"refreshS": 0}, {"refreshS": True}, {"refreshS": "2"}, {"verdictMaxAgeS": 0.1}, {"maxAgeS": 0}, {"refreshS": 11, "maxAgeS": 30}, {"verdictMaxAgeS": 40, "maxAgeS": 30},
    ]
    for value in bad:
        with pytest.raises(CellStateFileConfigError):
            parse_cell_state_file_settings(value)


# -- when the word is READY -------------------------------------------------------------------------------------------------------

def test_without_a_verdict_the_word_is_inhibited_and_ready_needs_a_fresh_ready_verdict(tmp_path: Path) -> None:
    writer, clock, path = make(tmp_path)
    assert writer.word() == "INHIBITED"
    writer.observe(verdict("READY"))
    assert writer.word() == "READY" and bridge_reads(path) == "READY"
    clock.now += 4.9
    assert writer.word() == "READY"
    clock.now += 0.2      # past verdictMaxAgeS (5 s): nobody judged the cell for too long
    assert writer.word() == "INHIBITED"
    writer.publish()
    assert bridge_reads(path) == "INHIBITED"


def test_every_other_state_of_a_verdict_is_written_as_it_is_and_an_unknown_one_is_inhibited(tmp_path: Path) -> None:
    writer, _clock, path = make(tmp_path)
    for state in ("INHIBITED", "FAULT", "SAFE_STOP", "READY"):
        writer.observe(verdict(state))
        assert bridge_reads(path) == state
    writer.observe(verdict("MYSTERY"))
    assert bridge_reads(path) == "INHIBITED"
    writer.observe({"reason": "no state"})
    assert bridge_reads(path) == "INHIBITED"
    writer.observe({"sdkSafetyState": "READY"})
    assert bridge_reads(path) == "INHIBITED"


def test_a_hold_in_force_keeps_the_cell_inhibited_even_when_the_verdict_is_ready(tmp_path: Path) -> None:
    holds = [0]
    writer, _clock, path = make(tmp_path, holds=lambda: holds[0])
    writer.observe(verdict("READY"))
    assert bridge_reads(path) == "READY"
    holds[0] = 1
    assert writer.word() == "INHIBITED"
    writer.publish()
    assert bridge_reads(path) == "INHIBITED"
    holds[0] = 0
    writer.observe(verdict("READY"))
    assert bridge_reads(path) == "READY"


def test_zones_that_do_not_come_from_a_file_of_this_machine_never_give_ready(tmp_path: Path) -> None:
    writer, _clock, path = make(tmp_path, zones_loaded=False)
    writer.observe(verdict("READY"))
    assert bridge_reads(path) == "INHIBITED"


def test_a_stopped_writer_is_safe_stop_at_once_and_ignores_later_verdicts(tmp_path: Path) -> None:
    writer, _clock, path = make(tmp_path)
    writer.start()
    assert bridge_reads(path) == "INHIBITED"      # the state of a service that has not judged anything yet
    writer.observe(verdict("READY"))
    assert bridge_reads(path) == "READY"
    writer.stop()
    assert bridge_reads(path) == "SAFE_STOP"
    writer.observe(verdict("READY"))
    assert bridge_reads(path) == "SAFE_STOP"


def test_a_file_that_nobody_refreshes_goes_stale_and_the_bridges_read_safe_stop(tmp_path: Path) -> None:
    writer, _clock, path = make(tmp_path)
    writer.observe(verdict("READY"))
    now = time.time()
    assert bridge_reads(path, 30.0, lambda: now) == "READY"
    assert bridge_reads(path, 30.0, lambda: now + 31) == "SAFE_STOP"      # a service that was killed leaves exactly this


def test_the_refresh_thread_rewrites_the_file_when_nothing_changed_and_downgrades_a_silent_verdict(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "cell_state"
    writer = CellStateWriter(CellStateFileSettings(enabled=True, path=str(path), refresh_s=0.2, verdict_max_age_s=5.0, max_age_s=30.0), clock=clock)
    writer.start()
    try:
        writer.observe(verdict("READY"))
        before = os.stat(path).st_mtime_ns
        deadline = time.time() + 5
        while os.stat(path).st_mtime_ns == before and time.time() < deadline:
            time.sleep(0.05)
        assert os.stat(path).st_mtime_ns != before, "the file is rewritten without a new verdict"
        clock.now += 60      # the verdicts stopped coming
        deadline = time.time() + 5
        while bridge_reads(path) == "READY" and time.time() < deadline:
            time.sleep(0.05)
        assert bridge_reads(path) == "INHIBITED"
    finally:
        writer.stop()


# -- the file ---------------------------------------------------------------------------------------------------------------------

def test_the_file_holds_one_word_and_a_newline_and_no_temporary_file_is_left(tmp_path: Path) -> None:
    path = tmp_path / "cell_state"
    write_word(str(path), "READY")
    assert path.read_bytes() == b"READY\n"
    write_word(str(path), "SAFE_STOP")
    assert path.read_bytes() == b"SAFE_STOP\n"
    assert [p.name for p in tmp_path.iterdir()] == ["cell_state"]
    with pytest.raises(ValueError):
        write_word(str(path), "ready")


def test_the_temporary_file_is_created_exclusively_without_following_a_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    real_open = os.open

    def spy(file: Any, flags: int, *rest: Any, **named: Any) -> int:
        if str(file).endswith(".tmp"):
            seen.append(flags)
        return real_open(file, flags, *rest, **named)

    monkeypatch.setattr(os, "open", spy)
    write_word(str(tmp_path / "cell_state"), "READY")
    assert len(seen) == 1 and seen[0] & os.O_EXCL and seen[0] & os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        assert seen[0] & os.O_NOFOLLOW


def test_a_link_in_place_of_the_file_is_replaced_and_never_written_through(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    try:
        os.symlink(victim, tmp_path / "cell_state")
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create links")
    write_word(str(tmp_path / "cell_state"), "READY")
    assert victim.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "cell_state").is_symlink() and (tmp_path / "cell_state").read_text(encoding="utf-8") == "READY\n"


@pytest.mark.skipif(os.name != "posix", reason="folder modes are those of a POSIX host")
def test_a_folder_that_anybody_can_write_to_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "open"
    folder.mkdir()
    folder.chmod(0o777)
    with pytest.raises(PermissionError):
        write_word(str(folder / "cell_state"), "READY")
    assert list(folder.iterdir()) == []


def test_a_missing_folder_is_an_error_and_the_service_does_not_create_it(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        write_word(str(tmp_path / "missing" / "cell_state"), "READY")
    assert not (tmp_path / "missing").exists()


def test_when_a_write_fails_the_old_file_is_removed_so_that_the_bridges_never_keep_reading_an_old_ready(tmp_path: Path) -> None:
    failing = [False]
    messages: list[str] = []

    def write(path: str, word: str) -> None:
        if failing[0]:
            raise PermissionError("read-only")
        write_word(path, word)

    writer, _clock, path = make(tmp_path, write=write, log=messages.append)
    writer.observe(verdict("READY"))
    assert bridge_reads(path) == "READY"
    failing[0] = True
    writer.observe(verdict("SAFE_STOP"))      # the danger must not leave the READY of before in place
    assert not path.exists() and bridge_reads(path) == "SAFE_STOP"
    writer.observe(verdict("READY"))
    assert len(messages) == 1 and "SAFE_STOP" in messages[0]      # said once
    failing[0] = False
    writer.observe(verdict("READY"))
    assert bridge_reads(path) == "READY"


def test_observing_never_raises_into_the_service(tmp_path: Path) -> None:
    writer, _clock, _path = make(tmp_path, holds=lambda: 1 // 0)
    writer.observe(verdict("READY"))
    writer.observe({})
    assert writer.failures >= 1


# -- the running service ----------------------------------------------------------------------------------------------------------

def _zones() -> dict:
    return {"zones": [{"id": "d", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 2, "y": 2, "z": 2}}],
            "calibration": {"version": "c", "source": "manual", "calibrated_at": datetime.now(timezone.utc).date().isoformat(), "max_age_days": 30}}


def _check(base: str, x: float) -> dict:
    body: dict[str, Any] = {"zones": _zones(), "detections": {"objects": [{"id": "p", "position": {"x": x, "y": 0, "z": 0}}]}}
    body["observation"] = {"active": True, "observedAt": datetime.now(timezone.utc).isoformat(), "maxAgeSeconds": 3600, "error": None}
    request = urllib.request.Request(base + "/check", data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - loopback test server
        return json.loads(response.read())


@contextmanager
def _running(writer: CellStateWriter | None) -> Iterator[str]:
    server = SafetyZonesServer(("127.0.0.1", 0), "test")
    server.cell_state_writer = writer
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_person_walking_into_a_zone_flips_the_file_the_bridges_read_and_the_verdict_is_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "cell_state"
    writer = CellStateWriter(CellStateFileSettings(enabled=True, path=str(path)))
    writer.start()
    try:
        assert bridge_reads(path) == "INHIBITED"
        with _running(writer) as base:
            outside = _check(base, 50.0)
            assert bridge_reads(path) == "READY"
            inside = _check(base, 1.0)
            assert bridge_reads(path) == "SAFE_STOP"
            again = _check(base, 50.0)
            assert bridge_reads(path) == "READY"
        with _running(None) as base:
            plain_outside, plain_inside = _check(base, 50.0), _check(base, 1.0)
    finally:
        writer.stop()
    assert bridge_reads(path) == "SAFE_STOP"
    assert again["state"] == outside["state"]
    for with_writer, without in ((outside, plain_outside), (inside, plain_inside)):
        for key in ("state", "reason", "breaches"):
            assert with_writer[key] == without[key]


# -- `serve` ----------------------------------------------------------------------------------------------------------------------

def _serve_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **extra: Any) -> tuple[int, dict]:
    import socket

    from hydra_umc_safety_zones.main import main

    seen: dict[str, Any] = {}

    def serve_forever(self: SafetyZonesServer, *args: Any, **kwargs: Any) -> None:
        seen["writer"] = self.cell_state_writer
        seen["file_in_service"] = (tmp_path / "cell_state").read_text(encoding="utf-8") if (tmp_path / "cell_state").exists() else None
        raise KeyboardInterrupt

    monkeypatch.setattr(SafetyZonesServer, "serve_forever", serve_forever)
    monkeypatch.setattr(SafetyZonesServer, "start_watcher", lambda self: None)
    zones = tmp_path / "zones.json"
    zones.write_text(json.dumps({**_zones(), **extra}), encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return main(["serve", "--port", str(port), "--zones-file", str(zones)]), seen


def test_serve_does_nothing_about_the_file_unless_the_zones_file_switches_it_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, seen = _serve_with(tmp_path, monkeypatch, cellState={"enabled": False, "path": str(tmp_path / "cell_state")})
    assert code == 0 and seen["writer"] is None and not (tmp_path / "cell_state").exists()
    code, seen = _serve_with(tmp_path, monkeypatch)
    assert code == 0 and seen["writer"] is None and not (tmp_path / "cell_state").exists()


def test_serve_writes_inhibited_when_it_starts_and_safe_stop_when_it_ends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    code, seen = _serve_with(tmp_path, monkeypatch, cellState={"enabled": True, "path": str(tmp_path / "cell_state")})
    assert code == 0
    assert seen["file_in_service"] == "INHIBITED\n"
    assert bridge_reads(tmp_path / "cell_state") == "SAFE_STOP"      # the clean stop wrote SAFE_STOP
    assert (tmp_path / "cell_state").read_text(encoding="utf-8") == "SAFE_STOP\n"
    assert "cell state:" in capsys.readouterr().out


def test_serve_refuses_nonsense_settings_of_the_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    for block in ("on", {"enabled": True, "path": "relative"}, {"enabled": True, "refreshS": 99}, {"enabled": "yes"}):
        code, _seen = _serve_with(tmp_path, monkeypatch, cellState=block)
        assert code == 2
        assert "invalid cellState settings" in capsys.readouterr().err
