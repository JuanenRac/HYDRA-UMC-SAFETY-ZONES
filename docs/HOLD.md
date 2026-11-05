# Holding one robot: `POST /hold`

A service that has the identity of the server (HYDRA-UMC-NODE-HEALING is the first) can ask this service to hold one robot before it touches it, for example while the robot still holds a mission. The route is a request channel, not a stop: it is off, it never answers without a credential, and by default it only records what was asked.

## Switches (both off by default)

| Switch | Meaning |
|---|---|
| `--hold` (or `SAFETY_ZONES_HOLD=1`) | Starts `POST /hold` and `GET /holds`. Without it both answer `404`. |
| `--hold-actuate` (or `SAFETY_ZONES_HOLD_ACTUATE=1`) | A hold also hands a stop request to the same requester as a breach. Needs `--hold` and `--can-estop-config`, or the service refuses to start. |

## Access

Neither a token nor the identity key: the route answers `403`, because nobody could be asked who is holding the robot. With `--identity-key-file`, a signed token of the server with the role `operator` or `admin` holds (a viewer reads but does not hold, and the holder is named in the list). With only the service token, that bearer token holds.

## `POST /hold`

```json
{"robot": "arm-3", "reason": "node lost"}
```

`robot` is a robot id of letters, digits and `. _ : -` (at most 64 characters), `reason` a text of at most 200 characters (required to hold). `{"robot": "arm-3", "release": true}` ends the hold.

Answer `200`: `{"robot": "arm-3", "held": true, "seq": 1, "actuated": false}`. `actuated` is true only when `--hold-actuate` is on and the requester took the request. `400` for a body that does not fit, `429` when 64 robots are already held.

## `GET /holds`

`{"holds": [{"robot", "reason", "by", "seq"}], "actuate": false}`. The holds are kept in memory: a restart forgets them, which the caller sees when it asks again.

Even with `--hold-actuate` the service only requests a stop; cutting power is the job of the firmware (see `CAN_ESTOP.md`). Nothing about this route has run on a real bus.
