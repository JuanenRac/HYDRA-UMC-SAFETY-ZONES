# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/stateful.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The optional stateful mode of the service (``serve --stateful``).

Without it the service keeps no state about the observer: whoever calls ``POST /check`` brings the observation that says the
detections are fresh, and the observer therefore claims its own freshness. In the stateful mode:

- the observer sends heartbeats to ``POST /heartbeat``; the service stamps each with ITS OWN monotonic clock when it arrives, and a
  heartbeat counts only for ``heartbeat_ttl_s`` seconds after that, whatever time the observer wrote in it;
- ``POST /check`` takes the observation from the last heartbeat (a request that brings its own is refused, as a server that holds its
  own zones refuses zones), so a missing, silent or late observer is INHIBITED;
- the state is pushed to every subscriber of ``GET /events`` (Server-Sent Events) when it changes, and again when it expires: a verdict
  is valid for ``state_ttl_s`` seconds, and when nothing renews it the service itself publishes INHIBITED. A consumer must also treat
  the silence of the stream as not safe.

Everything here is in memory and takes its clock as an argument, so the tests drive it with simulated time. The settings live in the
``stateful`` object of the zones file; the switch is ``--stateful`` (or ``SAFETY_ZONES_STATEFUL=1``) or ``"enabled": true`` there.
Nothing is published to MQTT: that needs a broker and is not part of this mode.
"""
from __future__ import annotations

import json
import math
import queue
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from hydra_umc_safety_zones.observation import ObservationStatus
from hydra_umc_safety_zones.safety_state import SafetyEvaluation, SafetyState, to_sdk_safety_state


@dataclass(frozen=True)
class StatefulSettings:
    """Tunable values of the stateful mode (all in the ``stateful`` object of the zones file).

    - ``enabled`` the switch from the file; ``--stateful`` also turns the mode on.
    - ``heartbeat_ttl_s`` how long a heartbeat counts after it arrives.
    - ``state_ttl_s`` how long a published verdict is valid; the service republishes it at half that time while it lasts.
    - ``max_subscribers`` simultaneous ``GET /events`` streams (at most 16: each one holds a connection slot of the service); one more is answered 503.
    - ``keepalive_s`` comment line on a stream with no event, so a dead connection is noticed.
    """

    enabled: bool = False
    heartbeat_ttl_s: float = 2.0
    state_ttl_s: float = 2.0
    max_subscribers: int = 8
    keepalive_s: float = 5.0

    def __post_init__(self) -> None:
        for name in ("heartbeat_ttl_s", "state_ttl_s", "keepalive_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"stateful.{name} must be a finite number above zero, got {value!r}")
        if isinstance(self.max_subscribers, bool) or not isinstance(self.max_subscribers, int) or not 1 <= self.max_subscribers <= 16:
            raise ValueError(f"stateful.max_subscribers must be an integer from 1 to 16, got {self.max_subscribers!r}")
        if not isinstance(self.enabled, bool):
            raise ValueError(f"stateful.enabled must be true or false, got {self.enabled!r}")


def parse_stateful_settings(data: object) -> StatefulSettings:
    """Reads the optional ``stateful`` object of a zones file; absent means the defaults with the mode off."""
    if data is None:
        return StatefulSettings()
    if not isinstance(data, dict):
        raise ValueError("stateful must be a JSON object")
    allowed = {"enabled", "heartbeat_ttl_s", "state_ttl_s", "max_subscribers", "keepalive_s"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"stateful has unknown field(s): {unknown}")
    return StatefulSettings(**data)


def format_event(payload: dict) -> bytes:
    """One Server-Sent Event: ``id``, ``event`` and a single-line JSON ``data``."""
    return f"id: {payload['sequence']}\nevent: safety-state\ndata: {json.dumps(payload, sort_keys=True)}\n\n".encode()


class Subscriber:
    """One ``GET /events`` stream: a small queue where only the newest events are kept."""

    def __init__(self) -> None:
        self.events: queue.Queue[dict] = queue.Queue(maxsize=16)

    def push(self, payload: dict) -> None:
        while True:
            try:
                self.events.put_nowait(payload)
                return
            except queue.Full:
                try:
                    self.events.get_nowait()  # a slow reader loses the oldest event, never the newest
                except queue.Empty:
                    pass


class StatefulMonitor:
    """Holds the last heartbeat and the last verdict, and publishes the state to the subscribers."""

    def __init__(self, settings: StatefulSettings, config_sha256: str | None = None) -> None:
        self.settings = settings
        self.config_sha256 = config_sha256
        self._lock = threading.Lock()
        self._heartbeat: ObservationStatus | None = None
        self._heartbeat_at: float | None = None
        self._verdict: dict | None = None
        self._verdict_at: float | None = None
        self._published_state: str | None = None
        self._published_at: float | None = None
        self._published_expired = False
        self._sequence = 0
        self._subscribers: list[Subscriber] = []

    # -- heartbeats ---------------------------------------------------------------------------------------------------------------

    def record_heartbeat(self, observation: ObservationStatus, mono_now: float) -> None:
        with self._lock:
            self._heartbeat = observation
            self._heartbeat_at = mono_now

    def current_observation(self, mono_now: float) -> ObservationStatus | None:
        """The observation to evaluate with, or None when no heartbeat arrived within ``heartbeat_ttl_s`` of the service's own clock.

        The age the observer may claim for its observation is capped to the heartbeat TTL: the observer cannot make its evidence last
        longer than the service allows.
        """
        with self._lock:
            observation, arrived = self._heartbeat, self._heartbeat_at
        if observation is None or arrived is None:
            return None
        if mono_now - arrived > self.settings.heartbeat_ttl_s or mono_now < arrived:
            return None
        return replace(observation, max_age_seconds=min(observation.max_age_seconds, self.settings.heartbeat_ttl_s))

    def missing_heartbeat_evaluation(self, mono_now: float) -> SafetyEvaluation:
        with self._lock:
            arrived = self._heartbeat_at
        if arrived is None:
            return SafetyEvaluation(SafetyState.INHIBITED, "no heartbeat from the observer has arrived yet - the detections are not backed by a live observer")
        return SafetyEvaluation(
            SafetyState.INHIBITED,
            f"the last heartbeat of the observer is {mono_now - arrived:.1f}s old, exceeds heartbeat_ttl_s={self.settings.heartbeat_ttl_s}",
        )

    # -- verdicts and push ----------------------------------------------------------------------------------------------------------

    def record_verdict(self, response: dict, mono_now: float) -> None:
        """Remember the verdict of a ``POST /check`` and publish it when the state changed or half of its validity has gone by."""
        sdk = response.get("sdkSafetyState")
        if not isinstance(sdk, dict):
            return
        with self._lock:
            self._verdict = {"sdkSafetyState": dict(sdk), "state": response.get("state"), "reason": response.get("reason")}
            self._verdict_at = mono_now
            changed = sdk.get("state") != self._published_state
            renew = self._published_at is None or mono_now - self._published_at >= self.settings.state_ttl_s / 2
            if changed or renew or self._published_expired:
                self._publish_locked(self._verdict, mono_now)

    def tick(self, mono_now: float) -> None:
        """Called every fraction of a second by the service: when the verdict or the heartbeat has expired, publish INHIBITED once."""
        with self._lock:
            if self._verdict is None or self._verdict_at is None or self._published_expired:
                return
            if mono_now - self._verdict_at > self.settings.state_ttl_s:
                reason = f"the last verdict is more than {self.settings.state_ttl_s}s old and nothing renewed it - the state is not confirmed"
            elif self._heartbeat_at is not None and mono_now - self._heartbeat_at > self.settings.heartbeat_ttl_s:
                reason = f"no heartbeat from the observer for more than {self.settings.heartbeat_ttl_s}s - the state is not confirmed"
            else:
                return
            self._publish_locked(self._verdict_of(SafetyEvaluation(SafetyState.INHIBITED, reason)), mono_now, expired=True)

    @staticmethod
    def _verdict_of(evaluation: SafetyEvaluation) -> dict:
        return {"sdkSafetyState": to_sdk_safety_state(evaluation), "state": evaluation.state.value, "reason": evaluation.reason}

    def _publish_locked(self, verdict: dict, mono_now: float, expired: bool = False) -> None:
        self._sequence += 1
        payload = {
            "sequence": self._sequence,
            "sdkSafetyState": verdict["sdkSafetyState"],
            "state": verdict["state"],
            "reason": verdict["reason"],
            "validForSeconds": 0.0 if expired else self.settings.state_ttl_s,
            "publishedAt": datetime.now(timezone.utc).isoformat(),
        }
        if self.config_sha256:
            payload["configSha256"] = self.config_sha256
        self._published_state = verdict["sdkSafetyState"].get("state")
        self._published_at = mono_now
        self._published_expired = expired
        for subscriber in self._subscribers:
            subscriber.push(payload)

    def current_event(self, mono_now: float) -> dict:
        """What a new subscriber is told first: the held verdict while it is valid, otherwise INHIBITED."""
        with self._lock:
            verdict, at = self._verdict, self._verdict_at
            sequence = self._sequence
        valid = verdict is not None and at is not None and 0 <= mono_now - at <= self.settings.state_ttl_s
        if valid and verdict is not None and at is not None:
            remaining = max(self.settings.state_ttl_s - (mono_now - at), 0.0)
        else:
            reason = (
                "no safety verdict has been reached yet" if verdict is None else "the last verdict has expired and nothing renewed it"
            )
            verdict = self._verdict_of(SafetyEvaluation(SafetyState.INHIBITED, reason))
            remaining = 0.0
        payload = {
            "sequence": sequence,
            "sdkSafetyState": verdict["sdkSafetyState"],
            "state": verdict["state"],
            "reason": verdict["reason"],
            "validForSeconds": remaining,
            "publishedAt": datetime.now(timezone.utc).isoformat(),
        }
        if self.config_sha256:
            payload["configSha256"] = self.config_sha256
        return payload

    # -- subscribers --------------------------------------------------------------------------------------------------------------

    def subscribe(self) -> Subscriber | None:
        """A new subscriber, or None when ``max_subscribers`` streams are already open."""
        with self._lock:
            if len(self._subscribers) >= self.settings.max_subscribers:
                return None
            subscriber = Subscriber()
            self._subscribers.append(subscriber)
            return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)
