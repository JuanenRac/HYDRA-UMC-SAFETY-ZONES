# The body of the robot: capsules placed by forward kinematics

The zones of this service are fixed volumes of a cell. Until now the only thing
compared with them was a detected object (a person, a pallet). The **robot body**
adds the robot itself: a list of segments, each with the two ends where forward
kinematics put it and a radius that you declare, which the service turns into
capsules in the frame of the zones and compares with the zones, together with the
detections and with the same velocity and separation margins.

Typical uses: a keep-out zone that the arm must not enter (a fence, a bench, a
person's work area drawn as a zone), or a warning zone that the arm must slow down
in.

**Nothing in this feature has been tried with a real robot, a real cell or real
zones.** It is tested against computed reference points only. It is **not a
certified safety function**: the service requests an emergency stop and does not
perform it.

## The switch

The feature is **off** unless the zones file says so, in the `geometry` object:

```json
{
  "geometry": {
    "robot_body": {
      "enabled": true,
      "required": false,
      "resolution_m": 0.05,
      "margin_m": 0.0,
      "max_segments": 64,
      "max_pieces_per_segment": 200
    }
  },
  "zones": [ ... ],
  "calibration": { ... }
}
```

`"robot_body": true` is the same as `{"enabled": true}` with the other values at
the figures above; `false`, or no `robot_body` key at all, is off.

- **Off (the default).** The service behaves exactly as before. A request that
  carries a `robotBody` is **not ignored**: `POST /check` answers `400` with
  `"state": "inhibited"` (and `sdkSafetyState` `INHIBITED`), `check --body` prints
  `SAFETY STATE: INHIBITED` and exits with status 3. The reason is the same as for
  the shapes: a client that thinks the robot is being watched must be told that it
  is not.
- **`required`.** With `true`, a request **without** a robot body is `INHIBITED`
  too, so a client that stops sending it is never read as all clear. Default
  `false`: a request with no body checks only the detections, as before.
- **`resolution_m`** (the longest piece a segment is cut into, above 0),
  **`margin_m`** (extra radius for every segment, not negative),
  **`max_segments`** (1 to 1024) and **`max_pieces_per_segment`** (1 to 10000) are
  the tuning of the installation. They live in the zones file, not in the code.
  Unknown fields, wrong types and out-of-range values make the **whole file
  invalid**.

## What a request carries

`POST /check` takes an optional `robotBody` next to `zones`, `detections` and
`observation`; `check` takes the same object in a file with `--body`:

```json
{
  "frame": { "translation": {"x": 1.2, "y": 0, "z": 0}, "rotation_deg": {"roll": 0, "pitch": 0, "yaw": 90} },
  "segments": [
    { "id": "upper_arm", "start": {"x": 0, "y": 0, "z": 0.3}, "end": {"x": 0.5, "y": 0, "z": 0.3}, "radius": 0.08 },
    { "id": "forearm",   "start": {"x": 0.5, "y": 0, "z": 0.3}, "end": {"x": 1.5, "y": 0, "z": 0.3}, "radius": 0.06 }
  ]
}
```

- Lengths are metres, angles are degrees, as everywhere in this service.
- `segments`: at least one, at most `max_segments`. Each needs `id` (unique),
  `start`, `end` and **`radius`**. There is **no default for the radius**: it is
  the thickness of that link, plus whatever clearance you want, and only the
  person who configures the cell knows it. A missing, zero, negative, non-numeric
  or non-finite radius refuses the request.
- `frame` (optional): where the frame of the poses is in the frame of the zones
  (`point_in_zones = R * point + translation`, `R = Rz(yaw) * Ry(pitch) *
  Rx(roll)`). Without it the poses are taken as already being in the frame of the
  zones.
- Anything unknown, a `NaN` or `Infinity`, a coordinate beyond 1 000 000 m (a unit
  mistake) or a duplicate id refuses the **whole** request. An empty list is
  refused, because a body with nothing in it would read as a robot that is nowhere.
- A detection whose id starts with `robot:` is refused when a body is given,
  because the segments report themselves as `robot:<id>`.

## The answer

A segment that may touch a zone appears in `breaches` like a detected object,
with `object_id` `robot:<segment id>` and the middle of the first piece that
touches as `position`. The state is the worst of all: a segment in a `danger`
zone is `DANGER` and the stop request names it (`object 'robot:forearm' breached
danger zone 'fence'`). The answer carries `"robotBody": {"segments": N}` when a
body was used. A request without a body gets exactly the same keys as before.

The verdict order is unchanged: a missing or expired calibration and the health of
the observer are checked first and win over any breach.

## The model

1. **Frame.** Each end is rotated and moved into the frame of the zones.
2. **Pieces.** A segment of length `L` is cut into `n = min(max_pieces_per_segment,
   ceil(L / resolution_m))` equal pieces.
3. **Spheres.** Each piece is replaced by the sphere around its middle that
   contains it: radius = declared radius + `margin_m` + `L / (2n)` + a rounding
   allowance. Every point of the capsule of the piece is within the declared
   radius of the axis, and every point of the axis of the piece is within `L / (2n)`
   of its middle, so the sphere contains the capsule: it can only **add** contacts.
   When `max_pieces_per_segment` stops the cutting, `L / (2n)` grows instead, so a
   long segment gets a bigger sphere, never a thinner cut that misses something.
4. **Zones.** The zones are the ones of the request, grown by the velocity margin
   and by the ISO 13855 separation model exactly as for detections
   (`toolVelocityMps`, `separation`). A sphere touches a zone when the distance
   from its middle to the solid, reduced by one nanometre as in `GEOMETRY.md`, is
   at most its radius. Touching counts.
5. **Rounding.** The allowance is a nanometre plus a millionth of a millimetre per
   metre of the largest coordinate, which is far above the error of a rotation and
   a sum.

The over-estimate is bounded: a segment is reported only if the distance from its
axis to the zone is at most the radius, the margin, half a piece and the
allowance.

## How it is tested

- `tests/test_characterization_verdicts.py` fixes the verdicts, reasons, margins
  and answer keys of the service **before** this feature, and still passes
  unchanged.
- `tests/test_robot_body_mesh.py` is the harness, with a fixed seed: for 160
  random segments (random ends, radius, frame, margin, resolution and piece cap) it
  builds a dense cloud of points that lie inside the capsule by construction, with
  its own arithmetic, puts a random zone (box, oriented box, cylinder or capsule)
  around one of those points and requires a breach, every time. A second property
  requires that a zone clearly farther than the bound above is **not** reported. A
  control shows that the harness fails a body whose radius is shrunk to 60 %.
- `tests/test_robot_body.py` covers the switch (off, on, `required`), every
  malformed input, the frame, the margins, the API (`400` and `INHIBITED` when it is
  off, a server that holds its own zones) and both commands.

## Chaining it with HYDRA-UMC-PHYSICS-REPLICA

This service **never runs** the other program and has no dependency on it. The
poses arrive already computed. The chain is three steps that you run:

```
# 1. forward kinematics of the pose you want to check
hydra-umc-physics-replica fk --urdf robot.urdf --joints "shoulder=0.5,elbow=0.3" --json > fk.json

# 2. say which joint frames each segment joins and declare the radii
hydra-umc-safety-zones body-from-fk --fk fk.json --segments segments.json --output body.json

# 3. check the body against the zones (and the detections)
hydra-umc-safety-zones check --zones zones.json --detections detections.json --observation observation.json --body body.json
```

`segments.json` names the segments; nothing is inferred:

```json
{
  "base": {"x": 0, "y": 0, "z": 0},
  "frame": {"translation": {"x": 1.2, "y": 0, "z": 0}},
  "segments": [
    {"id": "column", "from": "base",     "to": "shoulder", "radius": 0.10},
    {"id": "upper",  "from": "shoulder", "to": "elbow",    "radius": 0.08},
    {"id": "tool",   "from": "elbow",    "to": {"x": 0.9, "y": 0, "z": 0.1}, "radius": 0.05}
  ]
}
```

- `from` and `to` are a joint name of the `fk` output, `base` (declared in the file,
  because `fk` does not print the base) or an explicit point.
- The output of `fk --json` holds the origin of the frame of each joint in the
  frame of the robot. If the zones are in another frame, give it in `frame`.
- `body-from-fk` refuses what it cannot resolve (an unknown name, a joint called
  `base`, a duplicated joint, a missing or non-positive radius, `NaN`) and prints
  nothing on the standard output, exit status 3.
- `predict-collision` of PHYSICS-REPLICA answers whether a short trajectory touches
  the zones and prints the contacts, not the poses; use `fk` for the poses. The two
  models are alike (a capsule per segment, cut into spheres that contain it) but are
  separate: the sizes of `predict-collision` are in its own `--config` and the radii
  here are in the segments file. Use the same figures in both so that they assume
  the same robot.
- One pose is one check. To follow a movement, run the chain for each pose, or ask
  PHYSICS-REPLICA to predict the trajectory first.

## Limits, said plainly

- **Not tried with a real robot.** There is no collision geometry in the poses: a
  link whose real shape sticks out of the radius you declared, a tool longer than
  the segment that stands for it and anything the tool carries are not seen. A
  radius smaller than the real link makes the answer wrong in the unsafe direction
  and the model cannot detect it.
- The poses are the **commanded or computed** ones. If they do not match where the
  robot really is (a wrong URDF, a joint that did not move, a late reading), the
  answer is about a robot that is not there. Only the caller knows how fresh they
  are: nothing here ages them.
- One instant only. It does not look at the movement between two requests, the time
  to stop, the overshoot of the controller or the speed (apart from the velocity
  and separation margins the zones already have). Use `toolVelocityMps` and a
  separation model for the reach during the stop.
- Zones that include the robot's own base or fixture will always be breached by the
  segments that stand in them: draw the zones around what the robot must not
  touch.
- The piece cut is a trade between speed and tightness; `resolution_m` smaller is
  tighter and slower.
- It is not a safety function. It does not replace the protective devices of the
  cell nor the interlock of the bridge.
