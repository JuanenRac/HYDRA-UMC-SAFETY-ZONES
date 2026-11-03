# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_property_safety.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Property-based tests of the safety decision and of the signature of the safety state.

The cases come from a small generator with a FIXED seed (``random.Random``), so a run is the same on every machine and no test dependency is needed. The number of
cases per property is bounded to keep this file in a few seconds. Two properties carry the whole design: nothing but a complete, fresh, signed verdict is ever read as
released, and a separation distance that was not evaluated releases nothing when the zones file requires it.
"""
from __future__ import annotations

import copy
import random
from datetime import datetime, timedelta, timezone

import pytest

from hydra_umc_safety_zones import signing
from hydra_umc_safety_zones.breach import DetectedObject
from hydra_umc_safety_zones.calibration import ZoneCalibration
from hydra_umc_safety_zones.geometry import AABB, Point3D
from hydra_umc_safety_zones.observation import ObservationStatus
from hydra_umc_safety_zones.safety_state import SafetyEvaluation, SafetyState, evaluate_safety, to_sdk_safety_state
from hydra_umc_safety_zones.zones import SeparationModel, Zone, ZoneLevel, ZoneSet

SEED = 7001
CASES = 400
NOW = datetime(2030, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
KEY = b"k" * 40


def rng(name: str) -> random.Random:
    return random.Random(f"{SEED}:{name}")


# --- the signature -------------------------------------------------------------------------------------------------------------------------------
def sdk_state(state: str = "READY", at: datetime = NOW) -> dict:
    return {"schema_version": "1.0", "state": state, "source": "hydra-umc-safety-zones", "timestamp_utc": at.isoformat()}


def now_ms(at: datetime = NOW) -> int:
    return int(at.timestamp() * 1000)


def test_a_valid_fresh_verdict_verifies_and_only_that():
    signer = signing.StateSigner(KEY, clock_ms=lambda: now_ms())
    state = sdk_state()
    sig = signer.sign(state)
    assert signing.effective_state(state, sig, KEY, now_ms=now_ms())[0] == "READY"
    r = rng("tamper")
    for _ in range(CASES):
        s2, g2, k2 = copy.deepcopy(state), copy.deepcopy(sig), KEY
        what = r.randrange(9)
        if what == 0:
            s2["state"] = r.choice(["ready", "INHIBITED", "SAFE_STOP", "FAULT", "READY ", ""])
        elif what == 1:
            s2["source"] = "x" + s2["source"]
        elif what == 2:
            g2["counter"] = g2["counter"] + r.choice([-1, 1, 1000])
        elif what == 3:
            h = list(g2["sig"])
            i = r.randrange(len(h))
            h[i] = "0" if h[i] != "0" else "1"
            g2["sig"] = "".join(h)
        elif what == 4:
            g2["key_id"] = r.choice(["other", "", "default "])
        elif what == 5:
            k2 = bytes(r.randrange(256) for _ in range(40))
        elif what == 6:
            g2["alg"] = r.choice(["none", "hmac-sha256", "", "Ed25519", None, 5])
        elif what == 7:
            g2.pop(r.choice(["alg", "key_id", "counter", "sig"]))
        else:
            s2["extra"] = r.choice([1, "x", None])
        value, verdict = signing.effective_state(s2, g2, k2, now_ms=now_ms())
        assert value == "INHIBITED" and not verdict.ok, (what, s2, g2)


def test_unsigned_or_malformed_verdicts_are_always_inhibited():
    r = rng("malformed")
    odd = [None, True, 0, 1, "READY", [], {}, {"state": "READY"}, "x" * 300, 2 ** 80, 1.5]
    for _ in range(CASES):
        state = r.choice([sdk_state(), r.choice(odd)])
        sig = r.choice([r.choice(odd), {"alg": signing.ALGORITHM, "key_id": "default", "counter": r.choice(odd), "sig": r.choice(odd)}])
        value, verdict = signing.effective_state(state, sig, KEY, now_ms=now_ms())
        assert value == "INHIBITED" and not verdict.ok


def test_freshness_is_symmetric_and_exact():
    signer = signing.StateSigner(KEY, clock_ms=lambda: now_ms())
    state = sdk_state()
    sig = signer.sign(state)
    r = rng("fresh")
    for _ in range(CASES):
        limit = r.choice([1, 500, signing.DEFAULT_MAX_AGE_MS, 60000])
        skew = r.randrange(-2 * limit, 2 * limit + 1)
        ok = signing.verify(state, sig, KEY, now_ms=now_ms() + skew, max_age_ms=limit).ok
        assert ok == (-limit <= skew <= limit), (limit, skew)


def test_a_replayed_or_older_counter_is_refused_and_a_repeat_only_when_allowed():
    signer = signing.StateSigner(KEY, clock_ms=lambda: now_ms())
    state = sdk_state()
    sig = signer.sign(state)
    counter = sig["counter"]
    r = rng("replay")
    for _ in range(CASES):
        last = counter + r.randrange(-5, 6)
        plain = signing.verify(state, sig, KEY, now_ms=now_ms(), last_counter=last).ok
        repeat = signing.verify(state, sig, KEY, now_ms=now_ms(), last_counter=last, allow_repeat=True).ok
        assert plain == (counter > last)
        assert repeat == (counter >= last)


def test_the_counter_of_a_signer_only_goes_up_whatever_the_clock_does():
    r = rng("counter")
    clock = {"t": 10_000}
    signer = signing.StateSigner(KEY, clock_ms=lambda: clock["t"])
    last = 0
    for _ in range(CASES):
        clock["t"] += r.choice([-5000, 0, 0, 1, 1000])
        counter = signer.sign(sdk_state())["counter"]
        assert counter > last
        last = counter


def test_a_key_id_and_a_key_that_are_too_weak_are_refused():
    r = rng("keys")
    for _ in range(CASES):
        key = bytes(r.randrange(256) for _ in range(r.randrange(0, 50)))
        kid = "".join(r.choice("abcXYZ019._- /+\n") for _ in range(r.choice([0, 1, 5, 64, 65])))
        good_key = len(key) >= signing.MIN_KEY_CHARS
        good_kid = 0 < len(kid) <= 64 and all(c.isalnum() or c in "._-" for c in kid)
        try:
            signing.StateSigner(key, kid)
        except signing.SigningError:
            assert not (good_key and good_kid)
        else:
            assert good_key and good_kid


def test_a_verdict_signed_with_the_other_algorithm_is_never_accepted():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric import ed25519

    private = ed25519.Ed25519PrivateKey.generate()
    ed_signer = signing.Ed25519StateSigner(private, clock_ms=lambda: now_ms())
    hmac_signer = signing.StateSigner(KEY, clock_ms=lambda: now_ms())
    state = sdk_state()
    ed_sig, hm_sig = ed_signer.sign(state), hmac_signer.sign(state)
    public = private.public_key()
    assert signing.verify(state, ed_sig, public_key=public, expected_alg="Ed25519", now_ms=now_ms()).ok
    assert signing.verify(state, hm_sig, KEY, expected_alg="HMAC-SHA256", now_ms=now_ms()).ok
    assert not signing.verify(state, ed_sig, KEY, public_key=public, expected_alg="HMAC-SHA256", now_ms=now_ms()).ok
    assert not signing.verify(state, hm_sig, KEY, public_key=public, expected_alg="Ed25519", now_ms=now_ms()).ok
    r = rng("ed")
    for _ in range(60):
        g = dict(ed_sig)
        raw = bytearray(bytes.fromhex(g["sig"]))
        raw[r.randrange(64)] ^= 1 << r.randrange(8)
        g["sig"] = r.choice([bytes(raw).hex(), g["sig"][:-2], g["sig"] + "00", "zz" * 64])
        assert not signing.verify(state, g, public_key=public, expected_alg="Ed25519", now_ms=now_ms()).ok


# --- the decision --------------------------------------------------------------------------------------------------------------------------------
BOX = AABB(Point3D(0, 0, 0), Point3D(2, 2, 2))
MODEL = SeparationModel(approach_speed_mps=1.6, total_stop_time_s=0.3, intrusion_distance_m=0.1)


def random_case(r: random.Random):
    calibration = r.choice([None, ZoneCalibration("c1", "manual", TODAY, 30), ZoneCalibration("c1", "manual", TODAY - timedelta(days=31), 30)])
    zones = (Zone("d1", ZoneLevel.DANGER, BOX),) if r.random() < 0.7 else (Zone("w1", ZoneLevel.WARNING, BOX), Zone("d1", ZoneLevel.DANGER, BOX))
    zone_set = ZoneSet(zones=zones, calibration=calibration, separation=r.choice([None, MODEL]), separation_required=r.random() < 0.5)
    age = r.choice([None, -10.0, 0.0, 0.5, 4.9, 5.0, 5.1, 3600.0])
    observation = r.choice([
        None,
        ObservationStatus(r.random() < 0.8, None if age is None else NOW - timedelta(seconds=age), 5.0, r.choice([None, None, None, "driver lost"])),
    ])
    objects = tuple(DetectedObject(f"p{i}", Point3D(*(r.choice([-5.0, 1.0, 9.0]) for _ in range(3)))) for i in range(r.choice([0, 0, 1, 2])))
    return zone_set, observation, objects


def test_ready_needs_every_condition_and_never_less():
    r = rng("decision")
    seen = set()
    for _ in range(CASES * 2):
        zone_set, observation, objects = random_case(r)
        result = evaluate_safety(zone_set, objects, TODAY, observation, now=NOW)
        calibration_ok = zone_set.calibration is not None and (TODAY - zone_set.calibration.calibrated_at).days <= zone_set.calibration.max_age_days
        observer_ok = (observation is not None and observation.error is None and observation.active and observation.observed_at is not None
                       and -0.25 <= (NOW - observation.observed_at).total_seconds() <= observation.max_age_seconds)
        inside = any(0 <= o.position.x <= 2 and 0 <= o.position.y <= 2 and 0 <= o.position.z <= 2 for o in objects)
        separation_ok = zone_set.separation is not None or not zone_set.separation_required
        if result.state is SafetyState.READY:
            assert calibration_ok and observer_ok and not inside and separation_ok, (zone_set, observation, objects)
        if calibration_ok and observer_ok and not inside and separation_ok:
            assert result.state is SafetyState.READY
        if not (calibration_ok and observer_ok):
            assert result.state is SafetyState.INHIBITED
        if observation is not None and observation.error is not None and calibration_ok:
            assert result.fault is True
        sdk = to_sdk_safety_state(result)
        assert (sdk["state"] == "READY") == (result.state is SafetyState.READY)
        assert sdk["state"] in ("READY", "INHIBITED", "SAFE_STOP", "FAULT")
        seen.add((result.state, result.fault))
    assert {s for s, _ in seen} >= {SafetyState.READY, SafetyState.INHIBITED, SafetyState.DANGER}


def test_a_separation_that_was_not_evaluated_never_releases_when_required():
    r = rng("kct")
    for _ in range(CASES):
        zone_set = ZoneSet(zones=(Zone("d1", ZoneLevel.DANGER, BOX),), calibration=ZoneCalibration("c1", "manual", TODAY, 30), separation=None,
                           separation_required=True)
        observation = ObservationStatus(True, NOW - timedelta(seconds=r.random()), 5.0)
        objects = tuple(DetectedObject(f"p{i}", Point3D(9.0, 9.0, 9.0)) for i in range(r.choice([0, 1, 3])))
        result = evaluate_safety(zone_set, objects, TODAY, observation, now=NOW)
        assert result.state is SafetyState.INHIBITED
        assert result.separation is not None and result.separation["status"] == "not_evaluated"
        assert to_sdk_safety_state(result)["state"] == "INHIBITED"
        # the same cell with the figures given is released, so the refusal comes from the missing K, T and C and from nothing else
        released = evaluate_safety(ZoneSet(zone_set.zones, zone_set.calibration, separation=MODEL, separation_required=True), objects, TODAY, observation, now=NOW)
        assert released.state is SafetyState.READY
        assert released.reason == "no breach, calibration valid"


def test_the_sdk_state_translation_is_conservative():
    for state in SafetyState:
        for fault in (False, True):
            sdk = to_sdk_safety_state(SafetyEvaluation(state, "x", fault=fault))["state"]
            assert (sdk == "READY") == (state is SafetyState.READY and not fault)
            assert (sdk == "FAULT") == fault
            if state is SafetyState.WARNING and not fault:
                assert sdk == "INHIBITED"
            if state is SafetyState.DANGER and not fault:
                assert sdk == "SAFE_STOP"


# --- cases added after a mutation run (a mutant that survived is a behaviour nothing checked) ---------------------------------------------------------
def test_the_signed_text_is_exactly_the_one_the_consumers_rebuild():
    """The text under the signature is a contract with other repositories (the planner builds the same one), so it is written out here by hand."""
    state = {"schema_version": "1.0", "state": "READY", "source": "café", "timestamp_utc": "2030-06-01T12:00:00+00:00"}
    expected = (b'hydra-umc-safety-state-v1\n{"counter":5,"key_id":"default","state":{"schema_version":"1.0","source":"caf' + bytes([92]) + b'u00e9",'
                b'"state":"READY","timestamp_utc":"2030-06-01T12:00:00+00:00"}}')
    assert signing._message(state, 5, "default") == expected
    import hashlib
    import hmac

    signer = signing.StateSigner(KEY, "default", clock_ms=lambda: 5)
    out = signer.sign(state)
    assert out == {"alg": "HMAC-SHA256", "key_id": "default", "counter": 5, "sig": hmac.new(KEY, expected, hashlib.sha256).hexdigest()}


def test_the_counter_goes_up_by_exactly_one_when_the_clock_stands_still_and_follows_the_clock_otherwise():
    clock = {"t": 1000}
    signer = signing.StateSigner(KEY, clock_ms=lambda: clock["t"])
    assert [signer.sign(sdk_state())["counter"] for _ in range(3)] == [1000, 1001, 1002]
    clock["t"] = 5000
    assert signer.sign(sdk_state())["counter"] == 5000
    clock["t"] = 10
    assert signer.sign(sdk_state())["counter"] == 5001


def test_the_ways_of_writing_the_time_of_a_verdict():
    signer = signing.StateSigner(KEY, clock_ms=lambda: now_ms())
    for text in (NOW.isoformat().replace("+00:00", "Z"), NOW.isoformat(), NOW.replace(tzinfo=None).isoformat()):
        state = sdk_state()
        state["timestamp_utc"] = text
        assert signing.verify(state, signer.sign(state), KEY, now_ms=now_ms()).ok, text
    shifted = sdk_state()
    shifted["timestamp_utc"] = NOW.replace(tzinfo=None).isoformat() + "+01:00"      # one hour off once the offset is applied
    assert not signing.verify(shifted, signer.sign(shifted), KEY, now_ms=now_ms()).ok
    for odd in (None, 5, "", "yesterday", [], "2030-13-45T00:00:00Z"):
        state = sdk_state()
        state["timestamp_utc"] = odd
        verdict = signing.verify(state, signer.sign(state), KEY, now_ms=now_ms())
        assert not verdict.ok and verdict.reason == "the verdict has no readable timestamp"


def test_the_reasons_of_the_refusals_are_the_documented_ones():
    signer = signing.StateSigner(KEY, clock_ms=lambda: now_ms())
    state = sdk_state()
    sig = signer.sign(state)

    def reason(**kwargs):
        s, g = kwargs.pop("state", state), kwargs.pop("sig", sig)
        verdict = signing.verify(s, g, kwargs.pop("key", KEY), now_ms=now_ms(), **kwargs)
        assert verdict.ok is False and verdict.counter is None
        return verdict.reason

    assert reason(state="x") == "the verdict is not signed"
    assert reason(sig={**sig, "alg": "none"}) == "unknown signature algorithm"
    assert reason(sig={**sig, "counter": 0}) == "the signature is malformed"
    assert reason(key_id="other") == "the signature was made with another key id"
    assert reason(key=None) == "no HMAC key is configured"
    assert reason(sig={**sig, "sig": "0" * 64}) == "the signature does not match"
    assert reason(last_counter=sig["counter"]) == "the verdict is not newer than one already seen (replay)"
    assert reason(expected_alg="Ed25519").startswith("the verdict is signed with HMAC-SHA256, this consumer accepts Ed25519")
    assert signing.verify(state, sig, KEY, now_ms=now_ms() + 10_000).reason.startswith("the verdict is 10000 ms old (limit 2000 ms)")
    assert signing.verify(state, sig, KEY, now_ms=now_ms()) == signing.Verification(True, "", sig["counter"])


def test_a_counter_of_one_is_valid_and_zero_is_not():
    for counter, ok in ((1, True), (2, True), (0, False), (-1, False)):
        state = sdk_state()
        message = signing._message(state, counter, "default")
        import hashlib
        import hmac

        sig = {"alg": "HMAC-SHA256", "key_id": "default", "counter": counter, "sig": hmac.new(KEY, message, hashlib.sha256).hexdigest()}
        assert signing.verify(state, sig, KEY, now_ms=now_ms()).ok is ok


def _zone_set(**kwargs):
    from hydra_umc_safety_zones.robot_body import RobotBodySettings

    kwargs.setdefault("calibration", ZoneCalibration("c1", "manual", TODAY, 30))
    body = kwargs.pop("robot_body", RobotBodySettings())
    return ZoneSet(zones=(Zone("d1", ZoneLevel.DANGER, BOX), Zone("w1", ZoneLevel.WARNING, AABB(Point3D(5, 5, 5), Point3D(6, 6, 6)))), robot_body=body, **kwargs)


def test_every_refusal_of_the_decision_says_why():
    fresh = ObservationStatus(True, NOW - timedelta(seconds=1), 5.0)
    far = (DetectedObject("p", Point3D(-50.0, -50.0, -50.0)),)

    def decide(zone_set, observation=fresh, objects=far, **kwargs):
        return evaluate_safety(zone_set, objects, TODAY, observation, now=NOW, **kwargs)

    assert decide(_zone_set(calibration=None)).reason == "no calibration metadata present - zone geometry cannot be trusted"
    old = decide(_zone_set(calibration=ZoneCalibration("c7", "survey", TODAY - timedelta(days=40), 30)))
    assert old.state is SafetyState.INHIBITED and old.reason == "calibration 'c7' (source=survey) is 40 day(s) old, exceeds max_age_days=30"
    by = decide(_zone_set(calibration=ZoneCalibration("c7", "survey", TODAY - timedelta(days=40), 30, "ana")))
    assert by.reason == "calibration 'c7' (source=survey, by ana) is 40 day(s) old, exceeds max_age_days=30"
    assert decide(_zone_set(), observation=None).reason.startswith("no observation status provided")
    failing = decide(_zone_set(), ObservationStatus(True, NOW, 5.0, "driver lost"))
    assert failing.reason == "observer reported an internal error: driver lost" and failing.fault is True
    assert decide(_zone_set(), ObservationStatus(False, NOW, 5.0)).reason == "tracking observer is disabled - dependent functions are inhibited"
    assert decide(_zone_set(), ObservationStatus(True, None, 5.0)).reason.startswith("no real observation has been received yet this session")
    assert decide(_zone_set(), ObservationStatus(True, NOW - timedelta(seconds=9), 5.0)).reason == "last real observation is 9.0s old, exceeds max_age_seconds=5.0"
    assert decide(_zone_set(), objects=(DetectedObject("p1", Point3D(1.0, 1.0, 1.0)),)).reason == "object(s) ['p1'] breached a danger zone"
    assert decide(_zone_set(), objects=(DetectedObject("p2", Point3D(5.5, 5.5, 5.5)),)).reason == "object(s) ['p2'] breached a warning zone"


def test_the_robot_body_rules_and_the_naming_of_the_zones_without_figures():
    from hydra_umc_safety_zones.robot_body import RobotBodySettings

    fresh = ObservationStatus(True, NOW - timedelta(seconds=1), 5.0)
    required = _zone_set(robot_body=RobotBodySettings(enabled=True, required=True))
    result = evaluate_safety(required, (), TODAY, fresh, now=NOW)
    assert result.state is SafetyState.INHIBITED
    assert result.reason == "the zones file requires the robot body (geometry.robot_body.required) and the request carries none"
    optional = _zone_set(robot_body=RobotBodySettings(enabled=True, required=False))
    assert evaluate_safety(optional, (), TODAY, fresh, now=NOW).state is SafetyState.READY
    off = _zone_set()
    refused = evaluate_safety(off, (), TODAY, fresh, now=NOW, body=object())
    assert refused.state is SafetyState.INHIBITED and refused.reason.startswith("robot body refused: ")
    # the zones whose figures are missing are named, the ones with figures are not
    mixed = ZoneSet(zones=(Zone("with", ZoneLevel.DANGER, BOX, separation=MODEL), Zone("without", ZoneLevel.WARNING, AABB(Point3D(5, 5, 5), Point3D(6, 6, 6)))),
                    calibration=ZoneCalibration("c1", "manual", TODAY, 30), separation_required=True)
    result = evaluate_safety(mixed, (), TODAY, fresh, now=NOW)
    assert result.state is SafetyState.INHIBITED
    assert result.reason == "separation distance not evaluated for zone(s) ['without']: K, T and C are not configured (the zones file requires them)"
    assert to_sdk_safety_state(result)["schema_version"] == "1.0"


def test_the_ed25519_refusals_say_why_the_real_clock_is_the_default_and_a_repeat_is_not_allowed_by_default():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric import ed25519

    private = ed25519.Ed25519PrivateKey.generate()
    signer = signing.Ed25519StateSigner(private, clock_ms=lambda: now_ms())
    state = sdk_state()
    sig = signer.sign(state)
    public = private.public_key()
    assert signing.verify(state, sig, public_key=None, expected_alg="Ed25519", now_ms=now_ms()).reason == "no Ed25519 public key is configured"
    assert signing.verify(state, {**sig, "sig": "zz"}, public_key=public, now_ms=now_ms()).reason == "the signature is malformed"
    assert signing.verify(state, {**sig, "sig": sig["sig"][:-2]}, public_key=public, now_ms=now_ms()).reason == "the signature is malformed"
    assert signing.verify(state, {**sig, "sig": "00" * 64}, public_key=public, now_ms=now_ms()).reason == "the signature does not match"
    # no clock given: the real one is read
    from datetime import datetime as real_datetime, timezone as real_timezone

    moment = real_datetime.now(real_timezone.utc)
    live = {**sdk_state(), "timestamp_utc": moment.isoformat()}
    live_sig = signing.StateSigner(KEY).sign(live)
    assert signing.verify(live, live_sig, KEY).ok
    stale = {**sdk_state(), "timestamp_utc": (moment - timedelta(seconds=30)).isoformat()}
    assert not signing.verify(stale, signing.StateSigner(KEY).sign(stale), KEY).ok
    # the default of effective_state refuses a verdict whose counter was already seen, and a signed state that is not text is INHIBITED
    value, verdict = signing.effective_state(state, signing.StateSigner(KEY, clock_ms=lambda: now_ms()).sign(state), KEY, now_ms=now_ms(), last_counter=now_ms())
    assert value == "INHIBITED" and not verdict.ok
    odd = {**sdk_state(), "state": 5}
    value, verdict = signing.effective_state(odd, signing.StateSigner(KEY, clock_ms=lambda: now_ms()).sign(odd), KEY, now_ms=now_ms())
    assert value == "INHIBITED" and verdict.ok
