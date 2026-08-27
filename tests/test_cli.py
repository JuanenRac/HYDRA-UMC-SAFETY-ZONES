import json
from datetime import datetime, timezone

from hydra_umc_safety_zones.main import main


def _today_str():
    return datetime.now(timezone.utc).date().isoformat()


def _write_zones(tmp_path, calibration="valid"):
    """`calibration`: "valid" (fresh, today), "missing" (no calibration key
    at all), or "expired" (calibrated far enough in the past to exceed
    max_age_days=30)."""
    payload = {
        "zones": [
            {
                "id": "warn1",
                "level": "warning",
                "min": {"x": 0, "y": 0, "z": 0},
                "max": {"x": 10, "y": 10, "z": 10},
            },
            {
                "id": "danger1",
                "level": "danger",
                "min": {"x": 0, "y": 0, "z": 0},
                "max": {"x": 2, "y": 2, "z": 2},
            },
        ]
    }
    if calibration == "valid":
        payload["calibration"] = {
            "version": "cal-1",
            "source": "manual",
            "calibrated_at": _today_str(),
            "max_age_days": 30,
        }
    elif calibration == "expired":
        payload["calibration"] = {
            "version": "cal-0",
            "source": "manual",
            "calibrated_at": "2020-01-01",
            "max_age_days": 30,
        }
    elif calibration != "missing":
        raise ValueError(f"unknown calibration fixture kind: {calibration!r}")

    path = tmp_path / "zones.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_detections(tmp_path, x, y, z):
    path = tmp_path / "detections.json"
    path.write_text(
        json.dumps({"objects": [{"id": "op1", "position": {"x": x, "y": y, "z": z}}]}),
        encoding="utf-8",
    )
    return path


def test_bare_invocation_prints_identity_and_exits_zero(capsys):
    exit_code = main([])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "HYDRA-UMC-SAFETY-ZONES" in out


def test_check_no_breach_exits_zero(tmp_path, capsys):
    zones = _write_zones(tmp_path)
    detections = _write_detections(tmp_path, 50, 50, 50)
    exit_code = main(["check", "--zones", str(zones), "--detections", str(detections)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "SAFETY STATE: READY" in out


def test_check_warning_breach_exits_one(tmp_path, capsys):
    zones = _write_zones(tmp_path)
    detections = _write_detections(tmp_path, 5, 5, 5)
    exit_code = main(["check", "--zones", str(zones), "--detections", str(detections)])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "SAFETY STATE: WARNING" in out
    assert "warning zone 'warn1'" in out
    assert "E-STOP" not in out


def test_check_danger_breach_exits_two_and_requests_estop(tmp_path, capsys):
    zones = _write_zones(tmp_path)
    detections = _write_detections(tmp_path, 1, 1, 1)
    exit_code = main(["check", "--zones", str(zones), "--detections", str(detections)])
    out = capsys.readouterr().out
    assert exit_code == 2
    assert "SAFETY STATE: DANGER" in out
    assert "danger zone 'danger1'" in out
    assert "E-STOP REQUESTED" in out


def test_check_missing_calibration_inhibits_regardless_of_position(tmp_path, capsys):
    """Fail-safe: no calibration metadata at all must never fall through to
    READY, even when no object is anywhere near a zone."""
    zones = _write_zones(tmp_path, calibration="missing")
    detections = _write_detections(tmp_path, 500, 500, 500)
    exit_code = main(["check", "--zones", str(zones), "--detections", str(detections)])
    out = capsys.readouterr().out
    assert exit_code == 3
    assert "SAFETY STATE: INHIBITED" in out
    assert "no calibration metadata present" in out
    # The fail-safe path must short-circuit before any breach check runs.
    assert "BREACH" not in out
    assert "E-STOP" not in out


def test_check_expired_calibration_inhibits_even_inside_danger_zone(tmp_path, capsys):
    """Fail-safe: an expired calibration wins over what the (untrusted)
    geometry would otherwise report - even a real-looking danger breach
    must not be reported as such against stale geometry."""
    zones = _write_zones(tmp_path, calibration="expired")
    detections = _write_detections(tmp_path, 1, 1, 1)
    exit_code = main(["check", "--zones", str(zones), "--detections", str(detections)])
    out = capsys.readouterr().out
    assert exit_code == 3
    assert "SAFETY STATE: INHIBITED" in out
    assert "cal-0" in out
    assert "BREACH" not in out
    assert "E-STOP" not in out
