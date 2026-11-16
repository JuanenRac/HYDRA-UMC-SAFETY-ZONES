# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/cell_state_file.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The file that tells the bridges whether the cell is ``READY``: the producer of the ``[cell_state]`` file of HYDRA-UMC-BRIDGE-LASER, -CNC and -PRINTER3D.

Each bridge decides whether it may take a job from the state of the cell. With ``[cell_state] source = "file"`` it reads the first word of a small file and takes a missing,
unknown or older-than-``max_age_seconds`` file as ``SAFE_STOP``. This module writes that file from the verdicts of this service, and it fails closed in every way it can:

- the word is ``READY`` only while the last verdict of ``POST /check`` is ``READY`` AND is not older than ``verdictMaxAgeS``, no hold is in force, the zones come from a file of
  this machine and the writer is running; anything else is the state of the verdict (``INHIBITED``, ``FAULT``, ``SAFE_STOP``) or ``INHIBITED`` when there is none;
- the file is rewritten every ``refreshS`` seconds even when nothing changed, so that a service that is stopped, hung or killed leaves a file that goes stale and is read as
  ``SAFE_STOP``; a clean stop writes ``SAFE_STOP`` at once;
- the file is replaced whole through a temporary file created exclusively and without following a link, so a reader sees all of a word or the earlier one; a folder that anybody can
  write to is refused, and when a write fails the file is removed (an absent file is ``SAFE_STOP``) instead of leaving an older ``READY`` in place;
- off by default (``cellState.enabled`` in the zones file). The folder is not created here: it is provided by the installation, writable by the account of this service and
  readable by the accounts of the bridges. ``maxAgeS`` is the ``max_age_seconds`` the bridges are configured with; the refresh has to fit three times in it.

The file holds one word and a newline, nothing secret. Nothing here has run against a real bridge on a real cell: the tests read the file the way the bridges read it.
"""
from __future__ import annotations

import os
import secrets
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PATH = "/run/hydra-umc/cell_state"
STATES = ("READY", "INHIBITED", "FAULT", "SAFE_STOP")
_FIELDS = {"enabled", "path", "refreshS", "verdictMaxAgeS", "maxAgeS"}


class CellStateFileConfigError(ValueError):
    """The ``cellState`` object of the zones file cannot be used."""


@dataclass(frozen=True)
class CellStateFileSettings:
    """``enabled`` the switch (off); ``path`` the file the bridges read; ``refresh_s`` how often it is rewritten; ``verdict_max_age_s`` how old the last verdict may be and still
    count (older is ``INHIBITED``); ``max_age_s`` the ``max_age_seconds`` the bridges use for the age of the file."""

    enabled: bool = False
    path: str = DEFAULT_PATH
    refresh_s: float = 2.0
    verdict_max_age_s: float = 5.0
    max_age_s: float = 30.0


def _number(data: dict[str, Any], key: str, default: float, low: float, high: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise CellStateFileConfigError(f"cellState.{key} must be a number from {low:g} to {high:g}")
    return float(value)


def parse_cell_state_file_settings(data: object) -> CellStateFileSettings:
    """Reads the optional ``cellState`` object; absent means the defaults with the writer off. Anything unknown or malformed is refused."""
    if data is None:
        return CellStateFileSettings()
    if not isinstance(data, dict):
        raise CellStateFileConfigError("cellState must be a JSON object")
    unknown = sorted(set(data) - _FIELDS)
    if unknown:
        raise CellStateFileConfigError(f"cellState has unknown field(s): {unknown}")
    enabled = data.get("enabled", False)
    if not isinstance(enabled, bool):
        raise CellStateFileConfigError(f"cellState.enabled must be true or false, got {enabled!r}")
    path = data.get("path", DEFAULT_PATH)
    if not isinstance(path, str) or "\0" in path or not (path.startswith("/") or os.path.isabs(path)) or path.endswith(("/", "\\")) or len(path) > 200 or ".." in path.replace("\\", "/").split("/"):
        raise CellStateFileConfigError("cellState.path must be an absolute path of a file, without '..'")
    refresh = _number(data, "refreshS", 2.0, 0.2, 60.0)
    verdict_age = _number(data, "verdictMaxAgeS", 5.0, 0.5, 300.0)
    max_age = _number(data, "maxAgeS", 30.0, 1.0, 600.0)
    if refresh * 3 > max_age:
        raise CellStateFileConfigError("cellState.refreshS must fit at least three times in maxAgeS, or a late refresh would be read as a stopped service")
    if verdict_age > max_age:
        raise CellStateFileConfigError("cellState.verdictMaxAgeS must not be above maxAgeS")
    return CellStateFileSettings(enabled=enabled, path=path, refresh_s=refresh, verdict_max_age_s=verdict_age, max_age_s=max_age)


def write_word(path: str, word: str) -> None:
    """Replaces the file whole with ``word`` and a newline. Raises ``OSError`` when it cannot be done safely (a missing or world-writable folder, a link in the way)."""
    if word not in STATES:
        raise ValueError(f"not a state of the cell: {word!r}")
    target = Path(path)
    folder = target.parent
    info = os.stat(folder)
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(str(folder))
    if os.name == "posix" and info.st_mode & stat.S_IWOTH and not info.st_mode & stat.S_ISVTX:
        raise PermissionError(f"{folder} can be written by anybody")
    temporary = folder / f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as out:
            out.write(f"{word}\n".encode("ascii"))
        os.replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


class CellStateWriter:
    """Keeps the file up to date from the verdicts. ``observe`` takes every verdict of ``POST /check``; ``start`` begins the refresh thread; ``stop`` writes ``SAFE_STOP``."""

    def __init__(
        self,
        settings: CellStateFileSettings,
        *,
        zones_loaded: bool = True,
        holds: Callable[[], int] = lambda: 0,
        clock: Callable[[], float] = time.monotonic,
        write: Callable[[str, str], None] = write_word,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self._zones_loaded = zones_loaded
        self._holds = holds
        self._clock = clock
        self._write = write
        self._log = log or (lambda _message: None)
        self._lock = threading.Lock()
        self._verdict: str | None = None
        self._verdict_at: float | None = None
        self._written: str | None = None
        self._stopped = False
        self._warned = False
        self._thread: threading.Thread | None = None
        self._halt = threading.Event()
        self.writes = 0
        self.failures = 0

    # -- the decision ---------------------------------------------------------------------------------------------------------------

    def word(self) -> str:
        """The state to write now. ``READY`` needs a fresh ``READY`` verdict, zones from a file, no hold and a writer that is running."""
        with self._lock:
            verdict, at, stopped = self._verdict, self._verdict_at, self._stopped
        if stopped:
            return "SAFE_STOP"
        if not self._zones_loaded or verdict is None or at is None:
            return "INHIBITED"
        age = self._clock() - at
        if age < 0 or age > self.settings.verdict_max_age_s:
            return "INHIBITED"
        if verdict == "READY" and self._holds() > 0:
            return "INHIBITED"
        return verdict

    def observe(self, response: dict) -> None:
        """Called with every verdict. Never raises and never changes the verdict. A change of the word is written at once."""
        try:
            sdk = response.get("sdkSafetyState")
            state = sdk.get("state") if isinstance(sdk, dict) else None
            with self._lock:
                self._verdict = state if state in STATES else "INHIBITED"
                self._verdict_at = self._clock()
            word = self.word()
            if word != self._written:
                self.publish()
        except Exception:  # noqa: BLE001 - nothing here may reach the service
            self.failures += 1

    # -- the file -------------------------------------------------------------------------------------------------------------------

    def publish(self) -> str:
        """Writes the current word. When the write fails the file is removed, so that the bridges read ``SAFE_STOP`` and not an older word."""
        word = self.word()
        try:
            self._write(self.settings.path, word)
            self._written = word
            self.writes += 1
        except (OSError, ValueError) as error:
            self.failures += 1
            self._written = None
            try:
                os.unlink(self.settings.path)
            except OSError:
                pass
            if not self._warned:
                self._warned = True
                self._log(f"[safety-zones] cell state: could not write {self.settings.path} ({type(error).__name__}); the bridges read a missing or old file as SAFE_STOP")
        return word

    def start(self) -> None:
        """Writes ``INHIBITED`` (the state of a service that has not judged anything yet) and starts the refresh."""
        if self._thread is not None:
            return
        self.publish()

        def run() -> None:
            while not self._halt.wait(self.settings.refresh_s):
                try:
                    self.publish()
                except Exception:  # noqa: BLE001
                    self.failures += 1

        self._thread = threading.Thread(target=run, name="safety-zones-cell-state", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """A clean stop: ``SAFE_STOP`` at once, without waiting for the file to go stale."""
        with self._lock:
            self._stopped = True
        self._halt.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self.publish()
