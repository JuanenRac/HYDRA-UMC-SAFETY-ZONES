# Zone shapes: model, assumptions and limits

By default every zone is an axis-aligned box (`min` and `max` corners). A zones
file can switch on three more shapes, so that a zone can follow the real
footprint of a cell instead of a box around it:

| shape | what it is | typical use |
|-------|------------|-------------|
| `obb` | oriented box: centre, half extents, rotation | a fence line or a table that is not aligned with the axes |
| `cylinder` | vertical cylinder: centre of the base, radius, height | the area around the base of a robot |
| `capsule` | every point within a radius of a segment | a link of a robot arm, a conveyor edge |

**Nothing in this feature has been tried with a real robot, a real camera or a
real cell.** It is tested against computed reference points only.

## The switch

Shapes are **off** unless the zones file says so:

```json
{
  "geometry": { "shapes": true, "mode": "exact" },
  "zones": [ ... ],
  "calibration": { ... }
}
```

- Without a `geometry` object, or with `"shapes": false`, the service behaves
  exactly as before: only boxes are understood. A zone that names `obb`,
  `cylinder` or `capsule` then makes the **whole file invalid** (`check` ends
  with `INHIBITED` and exit status 3; `serve --zones-file` refuses to start; a
  request answers 400). It is never read as a box, because that could be
  smaller than the shape it was meant to be.
- A zone that names any other `shape` is refused, with or without the switch.
  `"shape": "box"` is the same as no `shape`.
- `mode` is `exact` (default) or `bounding`. In `bounding` mode every shape is
  replaced, when the file is read, by the smallest axis-aligned box that
  contains it. That box is larger than the shape, so it is the conservative
  choice when you doubt the exact one.

A box zone that carried a stray `shape` key with another value used to have it
ignored; it is now refused. Extra keys on a box zone other than `shape` are
still ignored, as before.

## Zone fields

Angles are degrees, lengths are metres, in the same cell frame as the boxes.
Every number must be finite (a `NaN` or `Infinity` is refused). A shape zone
accepts only its own fields; an unknown field is an error.

- `obb`: `center {x,y,z}`, `half_extents {x,y,z}` (each at least 0),
  optional `rotation_deg {roll,pitch,yaw}` (missing angles are 0). The local
  axes are rotated by `R = Rz(yaw) * Ry(pitch) * Rx(roll)`.
- `cylinder`: `base_center {x,y,z}`, `radius` (above 0), `height` (above 0).
  The axis is the vertical Z axis and the cylinder rises from the base. A
  tilted cylinder is not supported.
- `capsule`: `start {x,y,z}`, `end {x,y,z}`, `radius` (above 0). Equal ends
  make a sphere.

## What "never under-estimates" means here

A zone must never be smaller than the solid it stands for. The code keeps that in
four ways:

1. **Exact distances, reduced by one nanometre.** The distance from a point to
   each shape is the exact Euclidean distance to the solid minus `1e-9` m. The
   rounding of the floating point arithmetic is far below that, so it can only
   make a point read as closer, never farther. Touching counts as inside, like
   the boxes.
2. **Growing never shrinks.** The velocity margin and the ISO 13855 separation
   model grow a shape by at least the margin in every direction: an oriented box
   and a cylinder move their faces outward (which also covers the rounded
   corners of the exact offset), a capsule grows its radius. A negative or
   non-finite margin is refused.
3. **Bounding boxes contain the whole shape.** The `bounding` mode and the
   `obstacles` export use `bounding_box()`, which contains the shape (it is not
   the tightest box around the rounded corners, it is a safe one).
4. **A mesh harness proves it.** `tests/test_shapes_mesh.py` builds, for 75 seeded
   random shapes, a dense mesh of points that lie inside the solid by
   construction, with arithmetic independent of the code under test, and
   requires that every mesh point and every corner is contained, that every
   sphere reaching a mesh point is a breach, that every point within the margin
   of the solid is inside the grown shape, and that the bounding box contains
   the mesh. A control test shows the harness fails a shape shrunk by a tenth.

## Obstacles and path planners

`obstacles` covers a shape through its bounding box, cut into cubes and spheres
as for a box, so the spheres contain the whole shape and also the corners of the
box around it. An oriented box therefore exports more than it is.

## Limits and what this does NOT cover

- A zone does not know where the robot is. A capsule zone stands for a link only
  if whoever writes the file placed it there, so a capsule zone on a moving link
  is a fixed zone. To follow the robot, send its body with each request instead
  (`docs/ROBOT_BODY.md`).
- It does not mask a robot's own body out of the detections (self-occlusion).
- Overestimation is of the **zone**, not of the **person**: the size of an
  object still comes from its `radiusM` and the detector.
- The vertical cylinder cannot be tilted; an oriented box can approximate a
  tilted one but is larger.
- The sizes, margins and rotations are the installation's own figures from its
  risk assessment; the service assumes nothing.
- Hand-written zones are only as good as their coordinates. Re-check the file
  with `check` and a detection at each corner whenever you change it.
- None of this is a certified safety function. The service requests an
  emergency stop; it does not perform it.
