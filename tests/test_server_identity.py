# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_server_identity.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The identity of HYDRA-UMC-SERVER (server_identity.py): off by default; on, a signed token of the server is accepted and nothing is open."""
from __future__ import annotations

import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from hydra_umc_safety_zones import server_identity
from hydra_umc_safety_zones.api import SafetyZonesServer

KEY = b"a-signing-key-of-the-server-0123456789"
TOKEN = "own-token-of-the-service-0123"
OTHER = b"another-key-of-more-than-32-characters!"


def _serve(token: str | None, key: bytes | None, health_open: bool = False) -> Iterator[tuple[str, object]]:
    server = SafetyZonesServer(("127.0.0.1", 0), "test role", token=token or None)
    if key is not None:
        server.identity_key = key
    server.health_open = health_open
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _call(url: str, method: str = "GET", bearer: str | None = None) -> int:
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    if method != "GET":
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=b"{}" if method != "GET" else None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        error.read()
        return error.code


def test_it_is_off_by_default() -> None:
    for base, server in _serve(None, None):
        assert server.identity_key is None
        assert _call(base + "/stats") == 200
        assert _call(base + "/stats", bearer=server_identity.sign_token(KEY)) == 200  # nothing is asked while it is off
    for base, _ in _serve(TOKEN, None):
        assert _call(base + "/check", "POST", server_identity.sign_token(KEY)) == 401  # a token of the SERVER means nothing while it is off


def test_with_the_identity_on_nothing_is_open_and_a_viewer_only_reads() -> None:
    for base, _ in _serve(None, KEY, health_open=True):
        for path in ("/stats", "/health", "/metrics"):
            assert _call(base + path) == 401, path  # no credential, even on loopback and without a token of the service
        assert _call(base + "/stats", bearer="not-a-token") == 401
        viewer = server_identity.sign_token(KEY, role="viewer")
        assert _call(base + "/stats", bearer=viewer) == 200
        assert _call(base + "/health", bearer=viewer) == 200
        assert _call(base + "/check", "POST", viewer) == 401  # a viewer reads but does not change
        for role in ("operator", "admin"):
            assert _call(base + "/check", "POST", server_identity.sign_token(KEY, role=role)) not in (401, 403)
        assert _call(base + "/stats", bearer=server_identity.sign_token(OTHER)) == 401
        assert _call(base + "/stats", bearer=server_identity.sign_token(KEY, ttl_seconds=-5)) == 401


def test_the_own_token_keeps_working_with_the_identity_on() -> None:
    for base, _ in _serve(TOKEN, KEY):
        assert _call(base + "/stats", bearer=TOKEN) == 200
        assert _call(base + "/stats") == 401


def test_the_key_comes_from_a_file(tmp_path: Path) -> None:
    key_file = tmp_path / "identity.key"
    key_file.write_text(KEY.decode(), encoding="utf-8")
    assert server_identity.identity_key_from(str(key_file)) == KEY
    assert server_identity.identity_key_from(None, {}) is None
    assert server_identity.ENV_IDENTITY_KEY_FILE == "HYDRA_UMC_SAFETY_ZONES_IDENTITY_KEY_FILE"
    assert server_identity.probe(1, "/health", str(key_file)) is False  # nothing listens there


def test_the_copy_of_the_shared_module_is_the_canonical_one() -> None:
    canonical = Path(__file__).resolve().parents[2] / "HYDRA-UMC-SDK" / "tools" / "canonical" / "server_identity.py"
    if not canonical.is_file():
        pytest.skip("the SDK checkout is not next to this repository")
    assert Path(server_identity.__file__).read_bytes().replace(b"\r\n", b"\n") == canonical.read_bytes().replace(b"\r\n", b"\n")
