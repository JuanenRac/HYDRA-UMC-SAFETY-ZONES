# E-STOP request and safe heartbeat over CAN

`serve --can-estop-config can_estop.json` sends the stop request of a danger verdict on a CAN bus, and a heartbeat that says "the chain is healthy" only while it is.
This service still only REQUESTS a stop: the board that receives the frame decides what to do with it and is the one that cuts power. Not tried on a real bus; the frame below is a
proposal that has to be agreed with the firmware, and it is tested with a simulated bus (python-can `virtual`, or a stand-in).

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
  "repeat": 3
}
```

Every field is optional (the values above are the defaults, except `enabled`, which is `false`); an unknown field or a wrong value stops the start. `estop_id` must be lower than
`heartbeat_id` (the lowest identifier wins arbitration, so the stop comes first), `repeat` is how many times a stop frame is sent in a row (a bus may drop one) and `state_max_age_ms`
is the oldest verdict that still counts as healthy. `interface: "virtual"` is python-can's simulator. python-can is an optional dependency (`pip install python-can`).

## The frames

Both have 8 data bytes.

| Byte | Stop request | Safe heartbeat |
|---|---|---|
| 0 | `0x01` | `0x02` |
| 1 | version, `1` | version, `1` |
| 2-3 | sequence, little-endian, wraps at 65535 | sequence |
| 4-5 | 16-bit FNV-1a of the first danger zone that was breached | `0` |
| 6 | number of danger zones breached (at most 255) | `0` |
| 7 | CRC-8 (polynomial `0x07`) of bytes 0 to 6 | CRC-8 |

## What makes it fail safe

* The heartbeat goes out every `heartbeat_period_ms` ONLY while the last verdict of `POST /check` is `READY` and not older than `state_max_age_ms`. A service that crashed, froze,
  lost its observer or turned `INHIBITED` or `SAFE_STOP` stops sending it, and the board that stops seeing it must stop the machine: the stop needs no frame to be sent.
* A stop request that cannot be sent (the bus is off, python-can is missing) is never reported as sent: the answer carries `estopTransportError`, `estopRequests` stays empty and the
  state is still `SAFE_STOP`.
* The service refuses to start if the configuration is wrong or the bus cannot be opened.

The board must (1) stop on a valid stop frame, (2) stop when no valid heartbeat arrives within a few periods, and (3) ignore a frame whose check byte or version is wrong. None of this
exists in the firmware yet.
