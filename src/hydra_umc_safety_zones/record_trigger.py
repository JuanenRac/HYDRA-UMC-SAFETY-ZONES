# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/record_trigger.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The client that asks the camera streamer to keep the frames around a safe stop: ``POST <camera>/record/trigger``.

The streamer keeps a ring of the last frames and writes nothing until a safety service tells it to; this service is that safety service. When a verdict
becomes ``SAFE_STOP`` (and only then, once per stop: the state has to leave ``SAFE_STOP`` before the next trigger) it sends ``{"reason": ..., "source": "safety-zones"}``
to every configured camera.

- It only observes. The request goes out from a thread of its own with a short timeout, so a camera that does not answer costs the verdict nothing; a failure is
  counted and printed once. It never changes a verdict and never raises into the service.
- Off by default (``recordTrigger.enabled`` in the zones file). Every target is an ``http`` or ``https`` address of the form ``.../<camera>/record/trigger``; a host that is
  not this machine needs ``internal: true``. The bearer token of the streamer is read from ``tokenFile`` and never printed.
- The reason is the reason of the verdict cut to printable ASCII of at most 120 characters, which is what the streamer accepts.
  Nothing here has run against a real camera streamer with a real camera.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FIELDS = {"enabled", "targets", "tokenFile", "timeoutS", "internal", "minIntervalS"}
_TARGET = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+(:\d{1,5})?(/[A-Za-z0-9._~\-]{1,64})?/record/trigger$")
_LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")
MAX_TARGETS = 16
REASON_LIMIT = 120
SOURCE = "safety-zones"


class RecordTriggerConfigError(ValueError):
    """The ``recordTrigger`` object of the zones file cannot be used."""


@dataclass(frozen=True)
class RecordTriggerSettings:
    """``enabled`` the switch (off); ``targets`` the trigger addresses of the cameras; ``token_file`` the file with the bearer token ("" for none); ``timeout_s`` the wait for
    an answer; ``internal`` says that a target that is not on this machine is on the installation's own network; ``min_interval_s`` the shortest time between two triggers."""

    enabled: bool = False
    targets: tuple[str, ...] = ()
    token_file: str = ""
    timeout_s: float = 2.0
    internal: bool = False
    min_interval_s: float = 5.0


def parse_record_trigger_settings(data: object) -> RecordTriggerSettings:
    """Reads the optional ``recordTrigger`` object; absent means the defaults with the client off. Anything unknown or malformed is refused."""
    if data is None:
        return RecordTriggerSettings()
    if not isinstance(data, dict):
        raise RecordTriggerConfigError("recordTrigger must be a JSON object")
    unknown = sorted(set(data) - _FIELDS)
    if unknown:
        raise RecordTriggerConfigError(f"recordTrigger has unknown field(s): {unknown}")
    for key in ("enabled", "internal"):
        if key in data and not isinstance(data[key], bool):
            raise RecordTriggerConfigError(f"recordTrigger.{key} must be true or false, got {data[key]!r}")
    targets = data.get("targets", [])
    if not isinstance(targets, list) or len(targets) > MAX_TARGETS or not all(isinstance(t, str) for t in targets):
        raise RecordTriggerConfigError(f"recordTrigger.targets must be a list of at most {MAX_TARGETS} addresses")
    internal = data.get("internal", False)
    for target in targets:
        if not _TARGET.fullmatch(target):
            raise RecordTriggerConfigError(f"recordTrigger target {target!r} must look like http://host:port/<camera>/record/trigger")
        host = urllib.parse.urlsplit(target).hostname or ""
        if host not in _LOCAL_HOSTS and not internal:
            raise RecordTriggerConfigError(f"recordTrigger target host {host!r} is not on this machine: declare the network internal (internal: true) if it is the installation's own")
    if data.get("enabled", False) and not targets:
        raise RecordTriggerConfigError("recordTrigger is enabled but names no target")
    token_file = data.get("tokenFile", "")
    if not isinstance(token_file, str):
        raise RecordTriggerConfigError("recordTrigger.tokenFile must be a text")
    timeout = data.get("timeoutS", 2.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 10:
        raise RecordTriggerConfigError("recordTrigger.timeoutS must be a number from 0.1 to 10")
    interval = data.get("minIntervalS", 5.0)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 0 <= interval <= 3600:
        raise RecordTriggerConfigError("recordTrigger.minIntervalS must be a number from 0 to 3600")
    return RecordTriggerSettings(
        enabled=data.get("enabled", False), targets=tuple(targets), token_file=token_file, timeout_s=float(timeout), internal=internal, min_interval_s=float(interval)
    )


def trigger_reason(reason: object) -> str:
    """The reason of a verdict as the streamer accepts it: printable ASCII, 1 to 120 characters."""
    text = reason if isinstance(reason, str) else ""
    text = re.sub(r"[^\x20-\x7e]+", "?", text).strip()
    return (f"safe stop: {text}" if text else "safe stop")[:REASON_LIMIT]


Send = Callable[[str, bytes, str], None]


def _read_token(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RecordTriggerConfigError(f"recordTrigger.tokenFile cannot be read: {exc.strerror or type(exc).__name__}") from exc


def http_sender(timeout_s: float) -> Send:
    """Sends one POST; raises on anything but a 2xx or a 429 (the streamer's own minimum gap between two recordings is not a failure)."""

    def send(url: str, body: bytes, token: str) -> None:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=body, method="POST", headers=headers)  # noqa: S310 - the scheme is http/https by the pattern of the target
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
                response.read(1024)
        except urllib.error.HTTPError as error:
            if error.code != 429:
                raise

    return send


class RecordTriggerClient:
    """Watches the verdicts and asks the cameras to keep their frames when a safe stop begins. ``send(url, body, token)`` sends one request."""

    def __init__(
        self,
        settings: RecordTriggerSettings,
        send: Send,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] | None = None,
        token: str = "",
        background: bool = True,
    ) -> None:
        self.settings = settings
        self._send = send
        self._clock = clock
        self._log = log or (lambda _message: None)
        self._token = token
        self._background = background
        self._lock = threading.Lock()
        self._stopped = False
        self._last_at: float | None = None
        self._warned = False
        self.sent = 0
        self.failed = 0
        self._threads: list[threading.Thread] = []

    def observe(self, response: dict) -> None:
        """Called with every verdict of ``POST /check``. Never raises and never changes the verdict."""
        try:
            sdk = response.get("sdkSafetyState")
            state = sdk.get("state") if isinstance(sdk, dict) else None
            if not isinstance(state, str) or not self.settings.enabled:
                return
            now = self._clock()
            with self._lock:
                if state != "SAFE_STOP":
                    self._stopped = False
                    return
                if self._stopped:
                    return
                if self._last_at is not None and now - self._last_at < self.settings.min_interval_s:
                    return
                self._stopped, self._last_at = True, now
            body = json.dumps({"reason": trigger_reason(response.get("reason")), "source": SOURCE}).encode("utf-8")
            thread = threading.Thread(target=self._send_all, args=(body,), name="safety-zones-record-trigger", daemon=True)
            if self._background:
                thread.start()
                with self._lock:
                    self._threads = [t for t in self._threads if t.is_alive()] + [thread]
            else:
                self._send_all(body)
        except Exception:  # noqa: BLE001 - nothing here may reach the service
            self.failed += 1

    def join(self, timeout: float = 5.0) -> None:
        """Waits for the requests in flight (for tests and for a clean stop)."""
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)

    def _send_all(self, body: bytes) -> None:
        for target in self.settings.targets:
            try:
                self._send(target, body, self._token)
                self.sent += 1
            except Exception as error:  # noqa: BLE001 - a camera that does not answer must not stop the others
                self.failed += 1
                with self._lock:
                    first = not self._warned
                    self._warned = True
                if first:
                    self._log(f"[safety-zones] record trigger: could not reach a camera ({type(error).__name__}); the verdicts are not affected")


def build_client(settings: RecordTriggerSettings, log: Callable[[str], None] | None = None) -> RecordTriggerClient:
    """The client for these settings, with the real HTTP sender and the token read from its file."""
    token = _read_token(settings.token_file) if settings.token_file else ""
    return RecordTriggerClient(settings, http_sender(settings.timeout_s), log=log, token=token)


def describe(settings: RecordTriggerSettings) -> dict[str, Any]:
    return {"enabled": settings.enabled, "targets": len(settings.targets), "tokenFile": bool(settings.token_file), "timeoutS": settings.timeout_s}
