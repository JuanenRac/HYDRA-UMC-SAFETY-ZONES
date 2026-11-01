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

HMAC is the default: both sides hold the key, so a consumer could also forge a verdict. Where the consumer must not be able to sign, Ed25519 is
the option (`--state-signing-alg ed25519`): the service holds the private key in a file outside the repository, a consumer holds only the public key,
and the signature is `{"alg": "Ed25519", "key_id", "counter", "sig": "<128 hex>"}` over the same message. Ed25519 needs the `cryptography`
package (`pip install cryptography`); HMAC needs nothing. A consumer says which algorithm it expects (`expected_alg`), so a verdict signed with the
other one, or with no algorithm at all, is never accepted: an attacker cannot pick the weaker check.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

ALGORITHM = "HMAC-SHA256"
ALGORITHM_ED25519 = "Ed25519"
ALGORITHMS = (ALGORITHM, ALGORITHM_ED25519)
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


def _crypto():  # noqa: ANN202
    try:
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: PLC0415
    except ImportError as exc:
        raise SigningError("Ed25519 needs the cryptography package (pip install cryptography)") from exc
    return serialization, ed25519


def load_private_key_file(path: str | Path):  # noqa: ANN201
    """The Ed25519 private key in a PEM file (PKCS#8). It is never printed."""
    serialization, ed25519 = _crypto()
    try:
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    except OSError as exc:
        raise SigningError(f"the private key file cannot be read: {exc.strerror or type(exc).__name__}") from exc
    except (ValueError, TypeError) as exc:
        raise SigningError("the private key file is not an unencrypted PEM private key") from exc
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise SigningError("the private key file does not hold an Ed25519 key")
    return key


def load_public_key_file(path: str | Path):  # noqa: ANN201
    """The Ed25519 public key in a PEM file."""
    serialization, ed25519 = _crypto()
    try:
        key = serialization.load_pem_public_key(Path(path).read_bytes())
    except OSError as exc:
        raise SigningError(f"the public key file cannot be read: {exc.strerror or type(exc).__name__}") from exc
    except (ValueError, TypeError) as exc:
        raise SigningError("the public key file is not a PEM public key") from exc
    if not isinstance(key, ed25519.Ed25519PublicKey):
        raise SigningError("the public key file does not hold an Ed25519 key")
    return key


def generate_keypair(private_path: str | Path, public_path: str | Path) -> None:
    """Writes a new Ed25519 pair. It never overwrites a file, and the private key is created readable by its owner only."""
    serialization, ed25519 = _crypto()
    private = ed25519.Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public_pem = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    for target, data, mode in ((private_path, private_pem, 0o600), (public_path, public_pem, 0o644)):
        try:
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except OSError as exc:
            raise SigningError(f"{target} cannot be created (it must not exist yet): {exc.strerror or type(exc).__name__}") from exc
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)


def _message(state: dict, counter: int, key_id: str) -> bytes:
    body = {"state": state, "counter": counter, "key_id": key_id}
    return DOMAIN + json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


class Signer(Protocol):
    alg: str
    key_id: str

    def sign(self, sdk_state: dict) -> dict: ...


class StateSigner:
    """Signs safety states with a counter that only goes up."""

    alg = ALGORITHM

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


class Ed25519StateSigner:
    """Signs safety states with an Ed25519 private key and a counter that only goes up."""

    alg = ALGORITHM_ED25519

    def __init__(self, private_key: object, key_id: str = DEFAULT_KEY_ID, clock_ms: Callable[[], int] | None = None) -> None:
        _, ed25519 = _crypto()
        if not isinstance(private_key, ed25519.Ed25519PrivateKey):
            raise SigningError("the private key must be an Ed25519 key")
        if not key_id or len(key_id) > 64 or not all(c.isalnum() or c in "._-" for c in key_id):
            raise SigningError("the key id must be 1 to 64 letters, digits or . _ -")
        self._key = private_key
        self.key_id = key_id
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._last = 0
        self._lock = threading.Lock()

    def sign(self, sdk_state: dict) -> dict:
        with self._lock:
            counter = max(self._last + 1, self._clock_ms())
            self._last = counter
        sig = self._key.sign(_message(sdk_state, counter, self.key_id)).hex()  # type: ignore[attr-defined]
        return {"alg": ALGORITHM_ED25519, "key_id": self.key_id, "counter": counter, "sig": sig}


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
    key: bytes | None = None,
    *,
    now_ms: int | None = None,
    max_age_ms: int = DEFAULT_MAX_AGE_MS,
    last_counter: int | None = None,
    key_id: str | None = None,
    public_key: object | None = None,
    expected_alg: str | None = None,
    allow_repeat: bool = False,
) -> Verification:
    """Checks a signed verdict: the shape, the algorithm, the key id, the signature, the freshness of its timestamp and, when `last_counter` is given, that it is newer.

    `key` is the shared HMAC key and `public_key` the Ed25519 public key; `expected_alg` fixes which one the consumer accepts (a verdict signed with the other, or with
    none, is refused). `allow_repeat` lets the counter equal `last_counter`: a consumer that polls the last verdict reads the same one again and again, and only an OLDER
    counter is a replay."""
    if not isinstance(sdk_state, dict) or not isinstance(signature, dict):
        return Verification(False, "the verdict is not signed")
    alg = signature.get("alg")
    if alg not in ALGORITHMS:
        return Verification(False, "unknown signature algorithm")
    if expected_alg is not None and alg != expected_alg:
        return Verification(False, f"the verdict is signed with {alg}, this consumer accepts {expected_alg}")
    counter, sig, kid = signature.get("counter"), signature.get("sig"), signature.get("key_id")
    if not isinstance(counter, int) or isinstance(counter, bool) or counter < 1 or not isinstance(sig, str) or not isinstance(kid, str):
        return Verification(False, "the signature is malformed")
    if key_id is not None and kid != key_id:
        return Verification(False, "the signature was made with another key id")
    message = _message(sdk_state, counter, kid)
    if alg == ALGORITHM:
        if not isinstance(key, bytes):
            return Verification(False, "no HMAC key is configured")
        if not hmac.compare_digest(hmac.new(key, message, hashlib.sha256).hexdigest(), sig):
            return Verification(False, "the signature does not match")
    else:
        if public_key is None:
            return Verification(False, "no Ed25519 public key is configured")
        try:
            raw_sig = bytes.fromhex(sig)
        except ValueError:
            return Verification(False, "the signature is malformed")
        if len(raw_sig) != 64:
            return Verification(False, "the signature is malformed")
        try:
            public_key.verify(raw_sig, message)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - InvalidSignature, or a key of the wrong kind: both are "does not match"
            return Verification(False, "the signature does not match")
    now = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    age = _age_ms(sdk_state, now)
    if age is None:
        return Verification(False, "the verdict has no readable timestamp")
    if age > max_age_ms or age < -max_age_ms:
        return Verification(False, f"the verdict is {int(age)} ms old (limit {max_age_ms} ms)")
    if last_counter is not None and (counter < last_counter or (counter == last_counter and not allow_repeat)):
        return Verification(False, "the verdict is not newer than one already seen (replay)")
    return Verification(True, "", counter)


def effective_state(
    sdk_state: object,
    signature: object,
    key: bytes | None = None,
    *,
    now_ms: int | None = None,
    max_age_ms: int = DEFAULT_MAX_AGE_MS,
    last_counter: int | None = None,
    public_key: object | None = None,
    expected_alg: str | None = None,
    allow_repeat: bool = False,
) -> tuple[str, Verification]:
    """The state a consumer acts on: the signed one when it verifies, `INHIBITED` otherwise (unsigned, forged, old or replayed)."""
    result = verify(
        sdk_state, signature, key, now_ms=now_ms, max_age_ms=max_age_ms, last_counter=last_counter,
        public_key=public_key, expected_alg=expected_alg, allow_repeat=allow_repeat,
    )
    if not result.ok:
        return "INHIBITED", result
    state = sdk_state.get("state") if isinstance(sdk_state, dict) else None  # type: ignore[union-attr]
    return (state if isinstance(state, str) else "INHIBITED"), result
