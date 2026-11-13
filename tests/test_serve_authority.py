# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_serve_authority.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The authority of the service: `serve` starts only with zones fixed on this machine (and signed, when asked), and an address reachable from the network
needs a credential. Each refusal is an exit code 2 that names the option, before the port is opened, and none prints the key."""
from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hydra_umc_safety_zones.main import main
from hydra_umc_safety_zones.zones_signature import ZonesSignatureError, sign_zones, verify_zones

KEY = "a-key-of-at-least-thirty-two-characters-0123"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def zones_file(tmp_path: Path) -> Path:
    payload = {
        "zones": [{"id": "cell", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 2, "y": 2, "z": 2}}],
        "calibration": {"version": "c1", "source": "manual", "calibrated_at": datetime.now(timezone.utc).date().isoformat(), "max_age_days": 30},
    }
    path = tmp_path / "zones.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def key_file(tmp_path: Path, name: str = "zones.key", text: str = KEY) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def quiet_loop(monkeypatch):
    """A start that gets as far as serving returns at once."""
    from hydra_umc_safety_zones.api import SafetyZonesServer

    def serve_forever(self, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(SafetyZonesServer, "serve_forever", serve_forever)
    monkeypatch.setattr(SafetyZonesServer, "start_watcher", lambda self: None)
    monkeypatch.delenv("SAFETY_ZONES_ALLOW_REQUEST_ZONES", raising=False)
    monkeypatch.delenv("SAFETY_ZONES_ZONES_KEY_FILE", raising=False)


def serve(*arguments: str) -> int:
    return main(["serve", "--port", str(free_port()), *arguments])


def test_without_a_zones_file_the_service_does_not_start(capsys: pytest.CaptureFixture) -> None:
    assert serve() == 2
    err = capsys.readouterr().err
    assert "--zones-file is required" in err
    assert "--allow-request-zones" in err


def test_the_test_setting_starts_it_and_says_who_decides_the_zones(capsys: pytest.CaptureFixture) -> None:
    assert serve("--allow-request-zones") == 0
    assert "WARNING: zones and calibration come from each request" in capsys.readouterr().out


def test_the_environment_variable_is_the_same_switch(monkeypatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("SAFETY_ZONES_ALLOW_REQUEST_ZONES", "1")
    assert serve() == 0
    monkeypatch.setenv("SAFETY_ZONES_ALLOW_REQUEST_ZONES", "yes")
    assert serve() == 2


def test_a_zones_file_starts_it(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert serve("--zones-file", str(zones_file(tmp_path))) == 0
    assert "zones and calibration fixed by" in capsys.readouterr().out


def test_an_address_reachable_from_the_network_needs_a_token_or_an_identity(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    zones = str(zones_file(tmp_path))
    assert serve("--zones-file", zones, "--addr", "0.0.0.0") == 2
    err = capsys.readouterr().err
    assert "is not loopback" in err
    assert "--token" in err
    assert serve("--zones-file", zones, "--addr", "0.0.0.0", "--token", "a-token-0123456789") == 0
    identity = key_file(tmp_path, "identity.key")
    assert serve("--zones-file", zones, "--addr", "0.0.0.0", "--identity-key-file", str(identity)) == 0


def test_loopback_needs_no_credential(tmp_path: Path) -> None:
    zones = str(zones_file(tmp_path))
    for address in ("127.0.0.1", "localhost"):
        assert serve("--zones-file", zones, "--addr", address) == 0


def test_a_signed_zones_file_starts_and_says_the_key_id(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    zones = zones_file(tmp_path)
    key = key_file(tmp_path)
    assert main(["sign-zones", "--zones", str(zones), "--key-file", str(key), "--key-id", "cell-1"]) == 0
    assert (tmp_path / "zones.json.sig").is_file()
    assert serve("--zones-file", str(zones), "--zones-key-file", str(key)) == 0
    out = capsys.readouterr().out
    assert "the zones file is signed (key id cell-1)" in out
    assert KEY not in out


@pytest.mark.parametrize("problem", ["missing", "changed", "other-key", "garbage", "short-key"])
def test_a_zones_file_without_a_valid_signature_does_not_start(tmp_path: Path, capsys: pytest.CaptureFixture, problem: str) -> None:
    zones = zones_file(tmp_path)
    key = key_file(tmp_path)
    if problem != "missing":
        assert main(["sign-zones", "--zones", str(zones), "--key-file", str(key)]) == 0
    signature = tmp_path / "zones.json.sig"
    if problem == "changed":
        zones.write_text(zones.read_text(encoding="utf-8").replace("c1", "c2"), encoding="utf-8")
    if problem == "other-key":
        key = key_file(tmp_path, "other.key", "another-key-of-at-least-thirty-two-chars-99")
    if problem == "garbage":
        signature.write_text("{ not json", encoding="utf-8")
    if problem == "short-key":
        key = key_file(tmp_path, "short.key", "too-short")
    assert serve("--zones-file", str(zones), "--zones-key-file", str(key)) == 2
    err = capsys.readouterr().err
    assert "--zones-key-file" in err
    assert KEY not in err


def test_a_signature_file_alone_is_a_mistake(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    zones = zones_file(tmp_path)
    assert serve("--zones-file", str(zones), "--zones-signature-file", str(tmp_path / "x.sig")) == 2
    assert "needs --zones-key-file" in capsys.readouterr().err


def test_the_signature_can_live_elsewhere(tmp_path: Path) -> None:
    zones = zones_file(tmp_path)
    key = key_file(tmp_path)
    destination = tmp_path / "approved" / "cell.sig"
    destination.parent.mkdir()
    assert main(["sign-zones", "--zones", str(zones), "--key-file", str(key), "--out", str(destination)]) == 0
    assert serve("--zones-file", str(zones), "--zones-key-file", str(key), "--zones-signature-file", str(destination)) == 0


def test_signing_never_overwrites_and_needs_a_good_key(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    zones = zones_file(tmp_path)
    key = key_file(tmp_path)
    assert main(["sign-zones", "--zones", str(zones), "--key-file", str(key)]) == 0
    assert main(["sign-zones", "--zones", str(zones), "--key-file", str(key)]) == 2
    assert "never overwrites" in capsys.readouterr().err
    short = key_file(tmp_path, "short.key", "short")
    assert main(["sign-zones", "--zones", str(zones), "--key-file", str(short), "--out", str(tmp_path / "other.sig")]) == 2
    assert main(["sign-zones", "--zones", str(tmp_path / "none.json"), "--key-file", str(key), "--out", str(tmp_path / "n.sig")]) == 2


def test_the_library_calls_verify_the_exact_bytes(tmp_path: Path) -> None:
    zones = zones_file(tmp_path)
    key = key_file(tmp_path)
    document = sign_zones(zones, key, "k1")
    assert document["alg"] == "HMAC-SHA256" and document["key_id"] == "k1"
    signature = tmp_path / "sig.json"
    signature.write_text(json.dumps(document), encoding="utf-8")
    assert verify_zones(zones, signature, key) == "k1"
    for broken in ({**document, "sig": "0" * 64}, {**document, "alg": "none"}, {**document, "key_id": ""}, [1]):
        signature.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(ZonesSignatureError):
            verify_zones(zones, signature, key)
