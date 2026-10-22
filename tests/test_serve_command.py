# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_serve_command.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The `serve` command end to end: it refuses to start on a zones file it cannot
read or on a port that is taken, and, started for real with a zones file, it
holds those zones fixed, tags every verdict with the file's hash and demands the
bearer token."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from hydra_umc_safety_zones.main import main

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _zones_file(tmp_path: Path) -> Path:
    payload = {
        "zones": [{"id": "cell", "level": "danger", "min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 2, "y": 2, "z": 2}}],
        "calibration": {"version": "c1", "source": "manual", "calibrated_at": datetime.now(timezone.utc).date().isoformat(), "max_age_days": 30},
    }
    path = tmp_path / "zones.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_serve_refuses_to_start_on_an_unreadable_zones_file(tmp_path, capsys) -> None:
    code = main(["serve", "--port", str(_free_port()), "--zones-file", str(tmp_path / "missing.json")])
    assert code == 2
    assert "could not load --zones-file" in capsys.readouterr().err


def test_serve_refuses_to_start_on_a_zones_file_that_is_not_valid(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"zones": []}), encoding="utf-8")
    assert main(["serve", "--port", str(_free_port()), "--zones-file", str(bad)]) == 2
    assert "must not be empty" in capsys.readouterr().err


def test_serve_refuses_to_start_when_the_port_is_taken(capsys) -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        assert main(["serve", "--port", str(port)]) == 2
    assert "could not start the HTTP API" in capsys.readouterr().err


def _call(url: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_serve_with_a_zones_file_holds_the_zones_tags_the_hash_and_needs_the_token(tmp_path) -> None:
    zones = _zones_file(tmp_path)
    port = _free_port()
    environment = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""), PYTHONUNBUFFERED="1")
    process = subprocess.Popen(
        [sys.executable, "-m", "hydra_umc_safety_zones.main", "serve", "--port", str(port), "--zones-file", str(zones), "--token", "unit-token"],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if _call(f"{base}/stats")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("the server did not come up")
        now = datetime.now(timezone.utc).isoformat()
        request = {
            "detections": {"objects": [{"id": "person", "position": {"x": 1, "y": 1, "z": 1}}]},
            "observation": {"active": True, "observedAt": now, "maxAgeSeconds": 60, "error": None},
        }
        assert _call(f"{base}/check", request)[0] == 401                              # no token
        assert _call(f"{base}/check", request, token="wrong")[0] == 401               # wrong token
        status, verdict = _call(f"{base}/check", request, token="unit-token")
        assert status == 200
        assert verdict["state"] == "danger"                                            # the person is inside the fixed danger zone
        assert verdict["estopRequests"], "a danger verdict must carry the stop request"
        assert verdict["configSha256"] == hashlib.sha256(zones.read_bytes()).hexdigest()
        # a request that brings its own, emptier zones cannot replace the fixed ones
        sneaky = dict(request, zones={"zones": [{"id": "x", "level": "warning", "min": {"x": 9, "y": 9, "z": 9}, "max": {"x": 10, "y": 10, "z": 10}}]})
        status, body = _call(f"{base}/check", sneaky, token="unit-token")
        assert status == 400 and "must not send zones" in body["error"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()
