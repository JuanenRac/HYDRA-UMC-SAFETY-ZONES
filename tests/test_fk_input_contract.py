# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_fk_input_contract.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The ``JointPoses`` object of the physics replica is checked against its contract when ``body-from-fk`` reads it (evidence: simulator)."""
from __future__ import annotations

import copy
import math
import random

import pytest

from hydra_umc_safety_zones.fk_input import FkInputError, body_from_fk

POSES = [
    {"joint": "shoulder", "position": {"x": 0.0, "y": 0.0, "z": 0.3}, "quaternion": {"w": 1, "x": 0, "y": 0, "z": 0}},
    {"joint": "elbow", "position": {"x": 0.5, "y": 0.0, "z": 0.3}, "quaternion": {"w": 0.5, "x": 0.5, "y": 0.5, "z": 0.5}},
]
SPEC = {"base": {"x": 0, "y": 0, "z": 0}, "segments": [{"id": "upper", "from": "shoulder", "to": "elbow", "radius": 0.08}]}


def envelope(**changes):
    document = {"schema_version": "1.0", "frame": "base", "poses": copy.deepcopy(POSES), "checked": True, "note": "ok"}
    document.update(changes)
    return document


def test_a_contract_object_is_read() -> None:
    assert body_from_fk(envelope(), SPEC)["segments"][0]["id"] == "upper"


def test_the_optional_fields_may_be_left_out() -> None:
    document = envelope()
    del document["checked"], document["note"]
    assert body_from_fk(document, SPEC) == body_from_fk(envelope(), SPEC)


def test_a_bare_array_keeps_its_lenient_reading() -> None:
    bare = [{"joint": "shoulder", "position": {"x": 0.0, "y": 0.0, "z": 0.3}}, {"joint": "elbow", "position": {"x": 0.5, "y": 0.0, "z": 0.3}}]
    assert body_from_fk(bare, SPEC)["segments"][0]["radius"] == 0.08


def _mutated(mutate):
    document = envelope()
    mutate(document)
    return document


@pytest.mark.parametrize(
    "document",
    [
        _mutated(lambda d: d.update(extra=1)),
        _mutated(lambda d: d.update(frame="world")),
        _mutated(lambda d: d.update(checked="yes")),
        _mutated(lambda d: d.update(note=3)),
        _mutated(lambda d: d.update(note="x" * 1001)),
        _mutated(lambda d: d.update(poses=[copy.deepcopy(POSES[0])] * 257)),
        _mutated(lambda d: d["poses"][0].update(velocity=1)),
        _mutated(lambda d: d["poses"][0].update(joint="j" * 129)),
        _mutated(lambda d: d["poses"][0].pop("quaternion")),
        _mutated(lambda d: d["poses"][0].update(quaternion="up")),
        _mutated(lambda d: d["poses"][0].update(quaternion={"w": 1, "x": 0, "y": 0})),
        _mutated(lambda d: d["poses"][0].update(quaternion={"w": 1, "x": 0, "y": 0, "z": 0, "extra": 0})),
        _mutated(lambda d: d["poses"][0].update(quaternion={"w": 1, "x": 1, "y": 0, "z": 0})),
        _mutated(lambda d: d["poses"][0].update(quaternion={"w": math.nan, "x": 0, "y": 0, "z": 0})),
        _mutated(lambda d: d["poses"][0].update(quaternion={"w": True, "x": 0, "y": 0, "z": 0})),
        _mutated(lambda d: d["poses"][0].update(quaternion={"w": "1", "x": 0, "y": 0, "z": 0})),
        _mutated(lambda d: d["poses"][0]["position"].update(w=1)),
        _mutated(lambda d: d["poses"][1].update(joint="shoulder")),
        _mutated(lambda d: d["poses"].append("shoulder")),
    ],
)
def test_an_object_that_breaks_the_contract_is_refused(document) -> None:
    with pytest.raises(FkInputError):
        body_from_fk(document, SPEC)


def test_the_unit_length_is_judged_within_the_tolerance_of_the_contract() -> None:
    almost = {"w": 1.000004, "x": 0, "y": 0, "z": 0}  # 8e-6 off in squares: inside 1e-5
    document = envelope()
    document["poses"][0]["quaternion"] = almost
    body_from_fk(document, SPEC)
    document["poses"][0]["quaternion"] = {"w": 1.00001, "x": 0, "y": 0, "z": 0}  # 2e-5 off: outside
    with pytest.raises(FkInputError, match="unit quaternion"):
        body_from_fk(document, SPEC)


def test_random_unit_quaternions_pass_and_shrunk_ones_do_not() -> None:
    rng = random.Random(5)
    for _ in range(200):
        raw = [rng.gauss(0, 1) for _ in range(4)]
        norm = math.sqrt(sum(v * v for v in raw))
        w, x, y, z = (v / norm for v in raw)
        document = envelope()
        document["poses"][0]["quaternion"] = {"w": w, "x": x, "y": y, "z": z}
        body_from_fk(document, SPEC)
        document["poses"][0]["quaternion"] = {"w": w * 0.99, "x": x * 0.99, "y": y * 0.99, "z": z * 0.99}
        with pytest.raises(FkInputError):
            body_from_fk(document, SPEC)
