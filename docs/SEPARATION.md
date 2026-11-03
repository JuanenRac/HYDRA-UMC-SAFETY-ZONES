# Separation distance per zone (K, T and C without defaults)

The distance of separation between a protective field and the hazard is usually written in the form of ISO 13855 / ISO 13857:

```
S = K * T + C
```

- `K` approach speed of a person (m/s) - `approach_speed_mps`
- `T` total stopping time (s): the reaction time of the detection plus the full stopping time of the machine - `total_stop_time_s`
- `C` intrusion margin (m), the reach of a body part towards the hazard before it is detected (ISO 13857) - `intrusion_distance_m`

The formula is **indicative**. This service is not a certified safety function and the verdict it reaches is not a certification: the standard's own method (heights, orientation of the detector, the rest of the clauses) and a measured stopping time of the real machine are the job of the installation's risk assessment.

## No default value

`K`, `T` and `C` are configuration, per zone or for the whole machine, and **there is no built-in value**. A zone is evaluated only when all three are given. Otherwise it is **not evaluated**, with the empty figures named in the reason; nothing is computed with an invented figure. An empty figure is written `null` (or left out); a figure that is present but wrong (negative, not finite, not a number, all three zero) refuses the whole file.

Where the figures come from, in this order:

1. the `separation` object of the zone, when the zone has one. If it is incomplete, the zone is not evaluated: the machine figures are not mixed in.
2. the top-level `separation` object of the zones file (the whole machine).
3. nothing: not evaluated.

`docs/zones.separation.example.json` is a zones file with every figure empty.

## The three verdicts

Every answer of `POST /check`, and the held verdict of `GET /state`, carries an optional `separation` field (added to the answer; the signed `sdkSafetyState` is not changed):

```json
"separation": {
  "status": "not_evaluated",
  "required": false,
  "zones": [
    {"zone": "danger1", "verdict": "not_evaluated", "reason": "not evaluated: no separation figures (zone or machine); empty: approach_speed_mps, total_stop_time_s, intrusion_distance_m"},
    {"zone": "warn1", "verdict": "evaluated_safe", "reason": "no object inside the zone grown by S=0.900 m (machine figures)", "distanceM": 0.9}
  ]
}
```

| verdict | meaning |
| --- | --- |
| `evaluated_safe` | the zone was grown by its own S and no object is inside it |
| `evaluated_unsafe` | the zone was grown by its own S and an object is inside it |
| `not_evaluated` | a figure is missing; nothing was computed |

`status` is the worst of the zones (unsafe, then not evaluated, then safe). A zone grows by `max(linear margin, S)` as before, so the model can only make a zone larger, and the distance the tool itself travels (`v * T`, and `v^2 / 2a` with `tool_deceleration_mps2`) is added when a tool velocity is sent. The reported `distanceM` is `S` for a still tool.

## What a consumer does

**Not evaluated does not release.** A consumer that needs the separation distance (the semantic planner gate, Studio) must proceed only on `status == "evaluated_safe"`, and treat `not_evaluated` and a missing `separation` field the same way as `evaluated_unsafe`.

The service itself does the same when the zones file says `"separation_required": true`: a verdict that would be READY becomes INHIBITED (SDK state `INHIBITED`) with the zones that are not evaluated in the reason. A breach (WARNING or DANGER) keeps its own verdict. With `"separation_required": false`, the default, the verdict of a zones file written before this existed does not change; the answer only reports `not_evaluated`.
