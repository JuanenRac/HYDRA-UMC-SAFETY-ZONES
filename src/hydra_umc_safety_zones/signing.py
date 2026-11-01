# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/signing.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A signature for the safety state, so a consumer can tell where it came from and that it is fresh.

The service that reads a safety state (the visual servoing service, the orchestrator, a bridge) is told over HTTP. Without a signature
anyone who can reach the network can answer "READY". With a key shared between this service and the consumer, every verdict carries:

    "sdkSafetyStateSignature": {"alg": "HMAC-SHA256", "key_id": "...", "counter": N, "sig": "<hex>"}

The signature covers the whole `sdkSafetyState` object (state, source, timestamp), the counter and the key id, under a fixed domain
string. The counter only goes up (it is never lower than the clock in milliseconds), so a consumer that remembers the last counter
refuses a verdict that is replayed or older than one it has seen; the timestamp in the state is the freshness check.

`effective_state` is what a consumer calls: it returns the state to act on, and anything that is not a valid, fresh, newer signed
verdict is `INHIBITED` (fail closed). The signature is opt-in on the service (`--state-key-file`) because a consumer has to verify before
the service starts signing; a consumer that requires signatures simply treats an unsigned verdict as `INHIBITED`.

This is HMAC: both sides hold the key, so a consumer could also forge a verdict. Where the consumer must not be able to sign, an
asymmetric signature (Ed25519) is the right tool; it is not part of this module.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ALGORITHM = "HMAC-SHA256"
DOMAIN = b"hydra-umc-safety-state-v1\n"
MIN_KEY_CHARS = 32
DEFAULT_MAX_AGE_MS = 2000
DEFAULT_KEY_ID = "default"


class SigningError(ValueError):
    """The key or its file cannot be used."""


def load_key_file(path: str | Path) -> bytes:
    """The key in a file (its text, trimmed). It is never printed."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SigningError(f"the key file cannot be read: {exc.strerror or type(exc).__name__}") from exc
    if len(text) < MIN_KEY_CHARS:
        raise SigningError(f"the key file must hold at least {MIN_KEY_CHARS} characters")
    return text.encode("utf-8")


def _message(state: dict, counter: int, key_id: str) -> bytes:
    body = {"state": state, "counter": counter, "key_id": key_id}
    return DOMAIN + json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


class StateSigner:
    """Signs safety states with a counter that only goes up."""

    def __init__(self, key: bytes, key_id: str = DEFAULT_KEY_ID, clock_ms: Callable[[], int] | None = None) -> None:
        if len(key) < MIN_KEY_CHARS:
            raise SigningError(f"the key must hold at least {MIN_KEY_CHARS} characters")
        if not key_id or len(key_id) > 64 or not all(c.isalnum() or c in "._-" for c in key_id):
            raise SigningError("the key id must be 1 to 64 letters, digits or . _ -")
        self._key = key
        self.key_id = key_id
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._last = 0
        self._lock = threading.Lock()

    def sign(self, sdk_state: dict) -> dict:
        with self._lock:
            counter = max(self._last + 1, self._clock_ms())
            self._last = counter
        sig = hmac.new(self._key, _message(sdk_state, counter, self.key_id), hashlib.sha256).hexdigest()
        return {"alg": ALGORITHM, "key_id": self.key_id, "counter": counter, "sig": sig}


@dataclass(frozen=True)
class Verification:
    ok: bool
    reason: str = ""
    counter: int | None = None


def _age_ms(sdk_state: dict, now_ms: int) -> float | None:
    stamp = sdk_state.get("timestamp_utc")
    if not isinstance(stamp, str):
        return None
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return now_ms - when.timestamp() * 1000.0


def verify(
    sdk_state: object,
    signature: object,
    key: bytes,
    *,
    now_ms: int | None = None,
    max_age_ms: int = DEFAULT_MAX_AGE_MS,
    last_counter: int | None = None,
    key_id: str | None = None,
) -> Verification:
    """Checks a signed verdict: the shape, the key id, the signature, the freshness of its timestamp and, when `last_counter` is given, that it is newer."""
    if not isinstance(sdk_state, dict) or not isinstance(signature, dict):
        return Verification(False, "the verdict is not signed")
    if signature.get("alg") != ALGORITHM:
        return Verification(False, "unknown signature algorithm")
    counter, sig, kid = signature.get("counter"), signature.get("sig"), signature.get("key_id")
    if not isinstance(counter, int) or isinstance(counter, bool) or counter < 1 or not isinstance(sig, str) or not isinstance(kid, str):
        return Verification(False, "the signature is malformed")
    if key_id is not None and kid != key_id:
        return Verification(False, "the signature was made with another key id")
    expected = hmac.new(key, _message(sdk_state, counter, kid), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return Verification(False, "the signature does not match")
    now = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    age = _age_ms(sdk_state, now)
    if age is None:
        return Verification(False, "the verdict has no readable timestamp")
    if age > max_age_ms or age < -max_age_ms:
        return Verification(False, f"the verdict is {int(age)} ms old (limit {max_age_ms} ms)")
    if last_counter is not None and counter <= last_counter:
        return Verification(False, "the verdict is not newer than one already seen (replay)")
    return Verification(True, "", counter)


def effective_state(
    sdk_state: object,
    signature: object,
    key: bytes,
    *,
    now_ms: int | None = None,
    max_age_ms: int = DEFAULT_MAX_AGE_MS,
    last_counter: int | None = None,
) -> tuple[str, Verification]:
    """The state a consumer acts on: the signed one when it verifies, `INHIBITED` otherwise (unsigned, forged, old or replayed)."""
    result = verify(sdk_state, signature, key, now_ms=now_ms, max_age_ms=max_age_ms, last_counter=last_counter)
    if not result.ok:
        return "INHIBITED", result
    state = sdk_state.get("state") if isinstance(sdk_state, dict) else None  # type: ignore[union-attr]
    return (state if isinstance(state, str) else "INHIBITED"), result
