# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_simulation_bench.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Simulation bench: the real safety decision and the real stateful monitor against a simulated cell with people walking in it.

The cell is a warning zone around a danger zone. A person walks through it on a path drawn from a fixed seed (steps, turns, standing still, running) and the
observer that watches the cell sends a heartbeat every step, or fails to: it drops out, it is late, it reports an internal error, it comes back. Time is simulated:
the monitor and the decision take the clock as an argument, so a run of minutes takes a few milliseconds. The bench keeps its own picture of where the person
is and requires the state to be the one that picture demands, and above all never READY when it cannot be. Nothing here says how the real detector, the real
cameras or the real stop circuit behave: that needs the cell."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from hydra_umc_safety_zones.breach import DetectedObject
from hydra_umc_safety_zones.calibration import ZoneCalibration
from hydra_umc_safety_zones.geometry import AABB, Point3D
from hydra_umc_safety_zones.observation import ObservationStatus
from hydra_umc_safety_zones.safety_state import SafetyState, evaluate_safety, to_sdk_safety_state
from hydra_umc_safety_zones.stateful import StatefulMonitor, StatefulSettings
from hydra_umc_safety_zones.zones import Zone, ZoneLevel, ZoneSet

SEED = 20260101
EPOCH = datetime(2030, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
TODAY = EPOCH.date()
STEP_S = 0.1
DANGER_X = (0.0, 2.0)
WARNING_X = (-2.0, 4.0)


def _cell() -> ZoneSet:
    return ZoneSet(
        zones=(Zone("warning", ZoneLevel.WARNING, AABB(Point3D(WARNING_X[0], -2, -1), Point3D(WARNING_X[1], 4, 3))),
               Zone("danger", ZoneLevel.DANGER, AABB(Point3D(DANGER_X[0], 0, 0), Point3D(DANGER_X[1], 2, 2)))),
        calibration=ZoneCalibration("c1", "manual", TODAY, 30),
    )


def _expected(x: float) -> SafetyState:
    """Where the person is, by the picture the bench keeps (not by calling the code under test)."""
    if DANGER_X[0] <= x <= DANGER_X[1]:
        return SafetyState.DANGER
    if WARNING_X[0] <= x <= WARNING_X[1]:
        return SafetyState.WARNING
    return SafetyState.READY


def _walk(rng: random.Random, steps: int) -> list[float]:
    """The x of a person who walks, stops, turns and sometimes runs across the cell (y and z stay inside it)."""
    x, speed, path = rng.uniform(-8, 8), 0.0, []
    for _ in range(steps):
        if rng.random() < 0.08:
            speed = rng.choice([0.0, 0.0, 0.8, -0.8, 1.4, -1.4, 3.0, -3.0])
        x += speed * STEP_S
        if abs(x) > 10:
            speed = -speed
        path.append(x)
    return path


def _decide(zones: ZoneSet, x: float, observation: ObservationStatus | None, now: datetime):
    return evaluate_safety(zones, (DetectedObject("person", Point3D(x, 1.0, 1.0)),), TODAY, observation, now=now)


def test_with_a_healthy_observer_the_state_follows_the_person_through_the_cell_step_by_step() -> None:
    zones = _cell()
    for walk in range(20):
        for step, x in enumerate(_walk(random.Random(SEED + walk), 300)):
            now = EPOCH + timedelta(seconds=step * STEP_S)
            result = _decide(zones, x, ObservationStatus(True, now, 1.0), now)
            assert result.state is _expected(x), (walk, step, x)


def test_a_person_walking_straight_in_passes_warning_before_danger_and_leaves_the_same_way() -> None:
    zones = _cell()
    states = []
    for step in range(200):
        x = -6.0 + step * 0.06
        now = EPOCH + timedelta(seconds=step * STEP_S)
        states.append(_decide(zones, x, ObservationStatus(True, now, 1.0), now).state)
    compact = [s for i, s in enumerate(states) if i == 0 or s is not states[i - 1]]
    assert compact == [SafetyState.READY, SafetyState.WARNING, SafetyState.DANGER, SafetyState.WARNING, SafetyState.READY]


def test_the_cell_is_never_ready_while_the_observer_cannot_be_trusted_whatever_the_person_does() -> None:
    zones = _cell()
    rng = random.Random(SEED)
    for step, x in enumerate(_walk(rng, 600)):
        now = EPOCH + timedelta(seconds=step * STEP_S)
        health = rng.choice(["fresh", "fresh", "stale", "inactive", "error", "never", "future", "missing"])
        observation = {
            "fresh": ObservationStatus(True, now - timedelta(seconds=0.2), 1.0),
            "stale": ObservationStatus(True, now - timedelta(seconds=30), 1.0),
            "inactive": ObservationStatus(False, now, 1.0),
            "error": ObservationStatus(True, now, 1.0, "camera driver lost"),
            "never": ObservationStatus(True, None, 1.0),
            "future": ObservationStatus(True, now + timedelta(seconds=60), 1.0),
            "missing": None,
        }[health]
        result = _decide(zones, x, observation, now)
        if health == "fresh":
            assert result.state is _expected(x)
        else:
            assert result.state is SafetyState.INHIBITED or result.state is SafetyState.DANGER, (health, result)
            assert result.state is not SafetyState.READY


def test_an_empty_cell_is_ready_only_with_a_live_observer() -> None:
    zones = _cell()
    now = EPOCH
    assert evaluate_safety(zones, (), TODAY, ObservationStatus(True, now, 1.0), now=now).state is SafetyState.READY
    assert evaluate_safety(zones, (), TODAY, None, now=now).state is SafetyState.INHIBITED
    assert evaluate_safety(zones, (), TODAY, ObservationStatus(True, now - timedelta(seconds=9), 1.0), now=now).state is SafetyState.INHIBITED


def test_a_calibration_that_expires_in_the_middle_of_the_shift_takes_the_cell_out_of_ready() -> None:
    zones = ZoneSet(zones=_cell().zones, calibration=ZoneCalibration("c1", "manual", TODAY, 2))
    observation_at = lambda day: ObservationStatus(True, datetime.combine(day, EPOCH.timetz()), 1.0)  # noqa: E731
    states = []
    for days in range(5):
        day = TODAY + timedelta(days=days)
        now = datetime.combine(day, EPOCH.timetz())
        states.append(evaluate_safety(zones, (), day, observation_at(day), now=now).state)
    assert states == [SafetyState.READY, SafetyState.READY, SafetyState.READY, SafetyState.INHIBITED, SafetyState.INHIBITED]


# ------------------------------------------------------------------------------------------------------------------- the stateful monitor
def _drive(seed: int, steps: int, *, drop_after: int | None = None, drop_for: int = 0):
    """One shift in simulated time: the observer sends a heartbeat each step (except in its outage), the service decides and publishes."""
    zones = _cell()
    monitor = StatefulMonitor(StatefulSettings(enabled=True, heartbeat_ttl_s=0.35, state_ttl_s=1.0))
    subscriber = monitor.subscribe()
    assert subscriber is not None
    published, truth = [], []
    for step, x in enumerate(_walk(random.Random(seed), steps)):
        mono = step * STEP_S
        now = EPOCH + timedelta(seconds=mono)
        outage = drop_after is not None and drop_after <= step < drop_after + drop_for
        if not outage:
            monitor.record_heartbeat(ObservationStatus(True, now, 5.0), mono)
        observation = monitor.current_observation(mono)
        evaluation = _decide(zones, x, observation, now) if observation is not None else monitor.missing_heartbeat_evaluation(mono)
        monitor.record_verdict({"sdkSafetyState": to_sdk_safety_state(evaluation), "state": evaluation.state.value, "reason": evaluation.reason}, mono)
        monitor.tick(mono)
        truth.append((x, outage, evaluation.state))
        while not subscriber.events.empty():
            published.append((mono, subscriber.events.get_nowait()))
    return published, truth, monitor


def test_a_shift_without_outages_publishes_every_change_of_state_and_never_a_stale_ready() -> None:
    published, truth, _ = _drive(SEED, 400)
    states = [event["state"] for _, event in published]
    changes = sum(1 for i in range(1, len(truth)) if truth[i][2] is not truth[i - 1][2])
    assert len([1 for i in range(1, len(states)) if states[i] != states[i - 1]]) >= changes
    assert all(event["validForSeconds"] >= 0 for _, event in published)
    sequence = [event["sequence"] for _, event in published]
    assert sequence == sorted(set(sequence))  # the numbers only grow, so a consumer can see a missed event


def test_when_the_observer_goes_silent_the_state_becomes_inhibited_within_the_heartbeat_limit_and_comes_back_after() -> None:
    published, truth, monitor = _drive(SEED + 1, 300, drop_after=100, drop_for=60)
    silent = [state for _, outage, state in truth if outage]
    # the last heartbeat still counts for 0.35 s (three steps); after that nothing but INHIBITED is possible
    assert set(silent[4:]) == {SafetyState.INHIBITED}
    assert SafetyState.READY not in silent[4:]
    assert truth[-1][2] is _expected(truth[-1][0])
    assert any(event["state"] == "inhibited" for mono, event in published if 10.0 <= mono <= 16.0)
    assert monitor.subscriber_count == 1


def test_a_verdict_nobody_renews_expires_into_inhibited_on_its_own() -> None:
    monitor = StatefulMonitor(StatefulSettings(enabled=True, heartbeat_ttl_s=1.0, state_ttl_s=1.0))
    subscriber = monitor.subscribe()
    assert subscriber is not None
    ready = {"sdkSafetyState": to_sdk_safety_state(_decide(_cell(), 9.0, ObservationStatus(True, EPOCH, 1.0), EPOCH)), "state": "ready", "reason": ""}
    monitor.record_heartbeat(ObservationStatus(True, EPOCH, 5.0), 0.0)
    monitor.record_verdict(ready, 0.0)
    assert monitor.current_event(0.5)["state"] == "ready" and monitor.current_event(0.5)["validForSeconds"] == pytest.approx(0.5)
    monitor.tick(1.5)
    pushed = []
    while not subscriber.events.empty():
        pushed.append(subscriber.events.get_nowait())
    assert [e["state"] for e in pushed] == ["ready", "inhibited"] and pushed[-1]["validForSeconds"] == 0.0
    assert monitor.current_event(1.5)["state"] == "inhibited"
    monitor.tick(2.0)  # it is published once, not on every tick
    assert subscriber.events.empty()


def test_a_clock_that_goes_backwards_never_extends_a_heartbeat() -> None:
    monitor = StatefulMonitor(StatefulSettings(enabled=True, heartbeat_ttl_s=1.0))
    monitor.record_heartbeat(ObservationStatus(True, EPOCH, 5.0), 100.0)
    assert monitor.current_observation(100.5) is not None
    assert monitor.current_observation(99.0) is None
    assert monitor.current_observation(101.5) is None


def test_the_observer_cannot_make_its_evidence_last_longer_than_the_service_allows() -> None:
    monitor = StatefulMonitor(StatefulSettings(enabled=True, heartbeat_ttl_s=0.5))
    monitor.record_heartbeat(ObservationStatus(True, EPOCH, 3600.0), 0.0)
    observation = monitor.current_observation(0.1)
    assert observation is not None and observation.max_age_seconds == 0.5


def test_many_readers_and_a_slow_one_never_block_the_service_and_the_limit_is_kept() -> None:
    monitor = StatefulMonitor(StatefulSettings(enabled=True, max_subscribers=3, heartbeat_ttl_s=5.0, state_ttl_s=5.0))
    readers = [monitor.subscribe() for _ in range(5)]
    assert [r is not None for r in readers] == [True, True, True, False, False]
    slow = readers[0]
    assert slow is not None
    for step in range(100):  # nobody reads the first reader: only the newest 16 events stay
        mono = step * STEP_S
        state = "danger" if step % 2 else "ready"
        monitor.record_verdict({"sdkSafetyState": {"state": state.upper(), "source": "s", "schema_version": "1.0", "timestamp_utc": EPOCH.isoformat()}, "state": state, "reason": ""}, mono)
    assert slow.events.qsize() == 16
    newest = [slow.events.get_nowait() for _ in range(16)]
    assert [e["sequence"] for e in newest] == sorted(e["sequence"] for e in newest) and newest[-1]["sequence"] == 100
    monitor.unsubscribe(slow)
    assert monitor.subscriber_count == 2
    assert monitor.subscribe() is not None


def test_the_same_seed_gives_the_same_shift_and_another_seed_does_not() -> None:
    def run(seed: int) -> list[str]:
        _, truth, _ = _drive(seed, 200)
        return [state.value for _, _, state in truth]

    assert run(SEED) == run(SEED) and run(SEED) != run(SEED + 1)
