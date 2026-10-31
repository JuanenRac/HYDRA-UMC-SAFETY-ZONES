# Stateful mode: heartbeats and a pushed state

By default the service keeps no state about the observer. Whoever calls
`POST /check` brings the `observation` that says the detections are fresh, so
the observer vouches for its own freshness. The stateful mode moves that
evidence to the service.

**It has not been tried with a real observer, a real broker or a real
consumer.** It is tested with a simulated clock and local HTTP clients only.

## The switch

Off unless asked. Any of these turns it on:

- `serve --stateful`
- the environment variable `SAFETY_ZONES_STATEFUL=1`
- `"stateful": { "enabled": true }` in the zones file

With it off, `POST /heartbeat` and `GET /events` do not exist (404, also with a
token set) and `POST /check` behaves exactly as before.

The tuning values live in the `stateful` object of the zones file; nothing is
hard-coded:

| field | default | meaning |
|-------|---------|---------|
| `enabled` | `false` | the switch above |
| `heartbeat_ttl_s` | `2.0` | how long a heartbeat counts after it arrives |
| `state_ttl_s` | `2.0` | how long a published verdict is valid |
| `max_subscribers` | `8` | simultaneous `GET /events` streams (1 to 16) |
| `keepalive_s` | `5.0` | a comment line on a quiet stream |

An unknown field, or a value that is not a finite number above zero, makes
`serve` refuse to start.

## What changes

- **`POST /heartbeat`** takes the same object as `observation` (`active`,
  `observedAt`, `maxAgeSeconds`, `error`). The service stamps it with its own
  monotonic clock when it arrives. A heartbeat counts for `heartbeat_ttl_s`
  seconds from then, whatever time the observer wrote in it, and the age the
  observer claims for its evidence is capped to that TTL. An invalid heartbeat
  renews nothing. Same bearer token as `POST /check`.
- **`POST /check`** reads its observation from the last heartbeat. With none, or
  an expired one, the answer is INHIBITED. A request that brings its own
  `observation` is refused with 400, like a server that holds its own zones
  refuses zones.
- **`GET /events`** (Server-Sent Events, same token) sends the held state first,
  then every change of state, then again at half of `state_ttl_s` while verdicts
  keep coming. Each event carries the SDK safety state, the reason, a
  `sequence`, `validForSeconds` and the configuration hash when there is one.
  When a verdict is older than `state_ttl_s`, or the heartbeat is older than
  `heartbeat_ttl_s`, the service publishes INHIBITED by itself, once. A comment
  line (`: keepalive`) is sent when nothing else is.

## What a consumer must do

- Treat an event as valid only for `validForSeconds` after it is received, and
  treat the silence of the stream (no event and no keep-alive for longer than
  that) as not safe.
- Treat a closed stream as not safe and reconnect.

## Limits and what this does NOT cover

- Everything is in memory: a restart forgets the heartbeat and the verdict, and
  the first state is INHIBITED.
- The push is Server-Sent Events over the same HTTP port. Nothing is published
  to MQTT or WebSocket; that needs a broker and a client that was not part of
  this work.
- The detections still come from the caller of `POST /check`. A heartbeat proves
  that an observer is alive, not that the detections the caller sends are the
  ones that observer produced. Binding them together needs a signature, which
  this does not provide.
- A subscriber that does not read keeps only the newest sixteen events; each
  stream holds one of the 32 connection slots of the service.
- This is not a certified safety function. The service requests an emergency
  stop; it does not perform it.
