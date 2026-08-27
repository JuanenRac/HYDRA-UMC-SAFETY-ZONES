import json

from hydra_umc_safety_zones.main import main


def _write_zones(tmp_path):
    path = tmp_path / "zones.json"
    path.write_text(
        json.dumps(
            {
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
        ),
        encoding="utf-8",
    )
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
    assert "No zone breaches" in out


def test_check_warning_breach_exits_one(tmp_path, capsys):
    zones = _write_zones(tmp_path)
    detections = _write_detections(tmp_path, 5, 5, 5)
    exit_code = main(["check", "--zones", str(zones), "--detections", str(detections)])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "warning zone 'warn1'" in out
    assert "E-STOP" not in out


def test_check_danger_breach_exits_two_and_requests_estop(tmp_path, capsys):
    zones = _write_zones(tmp_path)
    detections = _write_detections(tmp_path, 1, 1, 1)
    exit_code = main(["check", "--zones", str(zones), "--detections", str(detections)])
    out = capsys.readouterr().out
    assert exit_code == 2
    assert "danger zone 'danger1'" in out
    assert "E-STOP REQUESTED" in out
