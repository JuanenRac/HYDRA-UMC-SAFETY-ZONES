# E-STOP request over CAN

`serve --can-estop-config can_estop.json` sends the stop request of a danger verdict on a CAN bus. This service still only REQUESTS a stop: the board that receives the frame decides what to do with
it and is the one that cuts power. Not tried on a real bus; it is tested with a simulated bus (python-can `virtual`, or a stand-in).

## The frame of the stack (default, `"protocol": "hydra"`)

The emergency stop of the whole stack is the frame that HYDRA-UMC defines on identifier **0x001** (standard 11-bit identifier, 8 data bytes). This service sends it as a node that asks for a stop:

| Byte | Field | Value sent |
|---|---|---|
| 0 | `kind` | `0x01` TRIP |
| 1 | `detail` | `0x04` safety zone |
| 2 | `source` | `0x10` the CM5 bridge |
| 3 | `epoch` | counts stops, modulo 256; goes up by one when a request comes more than `episode_gap_ms` after the previous one |
| 4-5 | `sequence` | little-endian, +1 on every frame this service sends, modulo 65536 |
| 6 | `kind` complement | `0x01 XOR 0xFF` |
| 7 | `detail` complement | `0x04 XOR 0xFF` |

* Only a TRIP is sent. ALIVE and RELEASE belong to the kinematic brain alone (a frame that claims them from another source is taken as a TRIP by every node), so **no heartbeat goes out on 0x001**: the
  liveness of the stack is the brain's. The frame carries no zone name; the answer of `POST /check` and the decision log still name the zones.
* A node latches on any TRIP whatever the epoch or the sequence, so a difference between this epoch and the one of the brain never delays a stop.
* Every one of the `repeat` frames of a request takes its own sequence number (the nodes ignore a sender that repeats one).
* A stop request that cannot be sent (the bus is off, python-can is missing) is never reported as sent: the answer carries `estopTransportError`, `estopRequests` stays empty and the state is still `SAFE_STOP`.
* The service refuses to start if the configuration is wrong or the bus cannot be opened. With this protocol `estop_id` must be 1 and `extended_id` false.

## The older frames (secondary path, `"protocol": "legacy"`)

The frames of earlier versions stay available: identifiers `estop_id` and `heartbeat_id` (1 and 2 by default), a check byte (CRC-8, polynomial `0x07`) and a safe heartbeat. With `"protocol": "hydra"` the
heartbeat of this path can still be sent on `heartbeat_id` with `"legacy_heartbeat": true`; it is off by default.

## The configuration file

```json
{
  "enabled": true,
  "interface": "socketcan",
  "channel": "can0",
  "bitrate": 500000,
  "extended_id": false,
  "estop_id": 1,
  "heartbeat_id": 2,
  "heartbeat_period_ms": 100,
  "state_max_age_ms": 1000,
  "repeat": 3,
  "protocol": "hydra",
  "episode_gap_ms": 1000,
  "legacy_heartbeat": false
}
```

Every field is optional (the values above are the defaults, except `enabled`, which is `false`); an unknown field or a wrong value stops the start. `estop_id` must be lower than
`heartbeat_id` (the lowest identifier wins arbitration, so the stop comes first), `protocol` is `hydra` (the default) or `legacy`, `episode_gap_ms` (50 to 60000) is the silence after which a request starts a new stop, `legacy_heartbeat` switches the older heartbeat on, `repeat` is how many times a stop frame is sent in a row (a bus may drop one) and `state_max_age_ms`
is the oldest verdict that still counts as healthy. `interface: "virtual"` is python-can's simulator. python-can is an optional dependency (`pip install python-can`).

## The bytes of the older frames

Both have 8 data bytes.

| Byte | Stop request | Safe heartbeat |
|---|---|---|
| 0 | `0x01` | `0x02` |
| 1 | version, `1` | version, `1` |
| 2-3 | sequence, little-endian, wraps at 65535 | sequence |
| 4-5 | 16-bit FNV-1a of the first danger zone that was breached | `0` |
| 6 | number of danger zones breached (at most 255) | `0` |
| 7 | CRC-8 (polynomial `0x07`) of bytes 0 to 6 | CRC-8 |

## What makes the older path fail safe

* The heartbeat goes out every `heartbeat_period_ms` ONLY while the last verdict of `POST /check` is `READY` and not older than `state_max_age_ms`. A service that crashed, froze,
  lost its observer or turned `INHIBITED` or `SAFE_STOP` stops sending it, and the board that stops seeing it must stop the machine: the stop needs no frame to be sent.
* A stop request that cannot be sent (the bus is off, python-can is missing) is never reported as sent: the answer carries `estopTransportError`, `estopRequests` stays empty and the
  state is still `SAFE_STOP`.
* The service refuses to start if the configuration is wrong or the bus cannot be opened.

The board must (1) stop on a valid stop frame, (2) stop when no valid heartbeat arrives within a few periods, and (3) ignore a frame whose check byte or version is wrong. None of this
exists in the firmware yet.
