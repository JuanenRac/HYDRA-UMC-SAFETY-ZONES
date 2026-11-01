# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_signing_ed25519.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""The optional Ed25519 signature of the safety state: valid, altered, repeated, expired, downgraded and the key files."""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("cryptography")

from hydra_umc_safety_zones.api import SafetyZonesServer  # noqa: E402
from hydra_umc_safety_zones.main import main  # noqa: E402
from hydra_umc_safety_zones.signing import (  # noqa: E402
    ALGORITHM,
    ALGORITHM_ED25519,
    Ed25519StateSigner,
    SigningError,
    StateSigner,
    effective_state,
    generate_keypair,
    load_private_key_file,
    load_public_key_file,
    verify,
)

HMAC_KEY = b"k" * 32


def _state(state: str = "READY", age_ms: int = 0) -> dict:
    when = datetime.now(timezone.utc) - timedelta(milliseconds=age_ms)
    return {"schema_version": "1.0", "state": state, "source": "hydra-umc-safety-zones", "timestamp_utc": when.isoformat()}


@pytest.fixture()
def pair(tmp_path):
    private, public = tmp_path / "state.key", tmp_path / "state.pub"
    generate_keypair(private, public)
    return load_private_key_file(private), load_public_key_file(public), private, public


def test_a_valid_ed25519_verdict_verifies_with_the_public_key_only(pair) -> None:
    private, public, _, _ = pair
    signer = Ed25519StateSigner(private, "line1")
    state = _state()
    signature = signer.sign(state)
    assert signature["alg"] == ALGORITHM_ED25519 and len(signature["sig"]) == 128
    assert signer.sign(state)["counter"] > signature["counter"]
    result = verify(state, signature, public_key=public, expected_alg=ALGORITHM_ED25519, key_id="line1")
    assert result.ok and result.counter == signature["counter"]
    assert effective_state(state, signature, public_key=public, expected_alg=ALGORITHM_ED25519)[0] == "READY"


def test_an_altered_verdict_signature_counter_or_key_does_not_verify(pair, tmp_path) -> None:
    private, public, _, _ = pair
    signer = Ed25519StateSigner(private)
    state = _state("INHIBITED")
    signature = signer.sign(state)
    assert not verify({**state, "state": "READY"}, signature, public_key=public).ok
    assert not verify(state, {**signature, "counter": signature["counter"] + 1}, public_key=public).ok
    assert not verify(state, {**signature, "key_id": "other"}, public_key=public).ok
    flipped = ("0" if signature["sig"][0] != "0" else "1") + signature["sig"][1:]
    assert not verify(state, {**signature, "sig": flipped}, public_key=public).ok
    assert not verify(state, {**signature, "sig": "zz"}, public_key=public).ok
    assert not verify(state, {**signature, "sig": signature["sig"][:-2]}, public_key=public).ok
    other = tmp_path / "o.pub"
    generate_keypair(tmp_path / "o.key", other)
    assert not verify(state, signature, public_key=load_public_key_file(other)).ok
    assert not verify(state, signature).ok  # no public key configured


def test_an_old_or_repeated_verdict_is_refused_but_the_same_verdict_polled_again_is_not(pair) -> None:
    private, public, _, _ = pair
    signer = Ed25519StateSigner(private)
    old = _state(age_ms=10_000)
    assert "old" in verify(old, signer.sign(old), public_key=public, max_age_ms=2000).reason
    state = _state()
    signature = signer.sign(state)
    assert verify(state, signature, public_key=public, last_counter=signature["counter"] - 1).ok
    replay = verify(state, signature, public_key=public, last_counter=signature["counter"])
    assert not replay.ok and "replay" in replay.reason
    assert verify(state, signature, public_key=public, last_counter=signature["counter"], allow_repeat=True).ok
    older = verify(state, signature, public_key=public, last_counter=signature["counter"] + 1, allow_repeat=True)
    assert not older.ok and "replay" in older.reason


def test_a_consumer_never_accepts_the_other_algorithm(pair) -> None:
    private, public, _, _ = pair
    state = _state()
    ed = Ed25519StateSigner(private).sign(state)
    hm = StateSigner(HMAC_KEY).sign(state)
    # a consumer that expects Ed25519 refuses an HMAC verdict (and the other way round), even with the right key
    assert not verify(state, hm, HMAC_KEY, public_key=public, expected_alg=ALGORITHM_ED25519).ok
    assert not verify(state, ed, HMAC_KEY, public_key=public, expected_alg=ALGORITHM).ok
    assert effective_state(state, hm, HMAC_KEY, public_key=public, expected_alg=ALGORITHM_ED25519)[0] == "INHIBITED"
    # an unknown algorithm, or none, is never accepted
    assert not verify(state, {**ed, "alg": "none"}, public_key=public).ok
    assert not verify(state, {k: v for k, v in ed.items() if k != "alg"}, public_key=public).ok
    # the HMAC path is unchanged
    assert verify(state, hm, HMAC_KEY, expected_alg=ALGORITHM).ok


def test_key_files_are_checked_and_never_overwritten(tmp_path) -> None:
    private, public = tmp_path / "k", tmp_path / "k.pub"
    generate_keypair(private, public)
    with pytest.raises(SigningError, match="must not exist"):
        generate_keypair(private, tmp_path / "other.pub")
    assert load_private_key_file(private) is not None
    with pytest.raises(SigningError):
        load_private_key_file(public)  # a public key is not a private one
    with pytest.raises(SigningError):
        load_public_key_file(tmp_path / "missing")
    bad = tmp_path / "bad"
    bad.write_text("not a key", encoding="utf-8")
    with pytest.raises(SigningError):
        load_private_key_file(bad)
    with pytest.raises(SigningError):
        Ed25519StateSigner(object(), "x")
    if os.name == "posix":
        assert oct(private.stat().st_mode & 0o777) == "0o600"


def test_the_keygen_command_never_prints_the_private_key(tmp_path, capsys) -> None:
    private, public = tmp_path / "s.key", tmp_path / "s.pub"
    assert main(["state-keygen", "--private-out", str(private), "--public-out", str(public)]) == 0
    out = capsys.readouterr().out
    assert "PRIVATE KEY" not in out and private.read_text(encoding="utf-8")[:30] not in out
    assert main(["state-keygen", "--private-out", str(private), "--public-out", str(public)]) == 2


def test_the_service_signs_with_ed25519_and_a_consumer_verifies(pair) -> None:
    private, public, _, _ = pair
    server = SafetyZonesServer(("127.0.0.1", 0), "test", signer=Ed25519StateSigner(private, "line1"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        today = datetime.now(timezone.utc).date().isoformat()
        body = {
            "zones": {"zones": [{"id": "d", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 2, "y": 2, "z": 2}}],
                      "calibration": {"version": "c", "source": "manual", "calibrated_at": today, "max_age_days": 30}},
            "detections": {"objects": [{"id": "o", "position": {"x": 50, "y": 50, "z": 50}}]},
            "observation": {"active": True, "observedAt": datetime.now(timezone.utc).isoformat(), "maxAgeSeconds": 3600, "error": None},
        }
        req = urllib.request.Request(f"{base}/check", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            answer = json.loads(resp.read())
        assert answer["sdkSafetyStateSignature"]["alg"] == ALGORITHM_ED25519
        assert verify(answer["sdkSafetyState"], answer["sdkSafetyStateSignature"], public_key=public, expected_alg=ALGORITHM_ED25519, key_id="line1").ok
        with urllib.request.urlopen(f"{base}/state", timeout=5) as resp:
            polled = json.loads(resp.read())
        assert polled["sdkSafetyStateSignature"] == answer["sdkSafetyStateSignature"]
    finally:
        server.shutdown()
        server.server_close()
