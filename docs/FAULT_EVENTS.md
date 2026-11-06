# Fault events (`fault-event/1`)

HYDRA-UMC-SERVER reads `fault-event/1` from the broker (`hydra/faults/#`), keeps the active faults and the history, and the MTConnect and OPC UA adapters show them to the plant. This service publishes the one fault it knows best: **the machine is no longer cleared to start**.

It is **off by default**. Switch it on in the zones file (it needs `pip install "hydra-umc-safety-zones[mqtt]"`):

```json
{ "faultEvents": { "enabled": true, "robotId": "arm-1", "topic": "hydra/faults/safety-zones", "retain": true, "minIntervalS": 1.0,
  "broker": { "host": "127.0.0.1", "port": 1883, "usernameFile": "/etc/hydra-umc/faults.user", "passwordFile": "/etc/hydra-umc/faults.pass" } } }
```

Every value is in the file, none in the code. A broker that is not on this machine needs `"internal": true` (the installation says that network is its own); `tls` and `caFile` are available. The credentials are read from files and never printed.

## What is published

One fault, `code` `safety_state`, `source` `safety-zones` (both configurable), on a change of the SDK safety state of a verdict:

| SDK state | `state` | `severity` |
| --- | --- | --- |
| `READY` | `cleared` | `info` |
| `INHIBITED` | `raised` | `warning` |
| `FAULT` (the observer itself failed) | `raised` | `error` |
| `SAFE_STOP` | `raised` | `critical` |

The `text` is the reason of the verdict (240 characters at most). A change from one raised state to another updates the same fault. The first verdict being `READY` publishes nothing (there is nothing to clear).

## Rules

- Only a **change** is published, and not more often than `minIntervalS`; a state that flaps does not flood the broker, and the newest state goes out with the first verdict after the interval.
- The message is retained by default (`retain`), so a server that starts later reads the present state at once. Quality of service 1.
- It only observes: it never changes a verdict, never blocks `POST /check` and never raises into the service. A publish that fails is counted, announced once on standard error and tried again with the next verdict.
- It reads nothing from the broker. The events are published when `POST /check` produces a verdict; the expiry of a stateful verdict (which is pushed on `GET /events`) is not a verdict of `POST /check` and does not publish one.
- Not tried with a real broker or a real server.
