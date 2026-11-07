# Zone occupancy

The zone interlock of HYDRA-UMC-BRIDGE-AMR pauses a vehicle whose next node lies in an occupied zone. This service knows which zones have something inside them, so it can publish that. It publishes **which zones are occupied** and nothing else.

It is **off by default**. Switch it on in the zones file (it needs `pip install "hydra-umc-safety-zones[mqtt]"`):

```json
{ "zoneOccupancy": { "enabled": true, "topic": "hydra/safety/zones/occupied", "refreshS": 1.0,
  "broker": { "host": "127.0.0.1", "port": 1883, "usernameFile": "/etc/hydra-umc/zones.user", "passwordFile": "/etc/hydra-umc/zones.pass" } } }
```

The message is `{"occupied": ["press-1"], "observed_at": <milliseconds>}`, the format the AMR bridge reads. A zone is occupied while a breach of any level names it in a verdict of `POST /check`.

- It is **not retained**: an occupancy left on the broker by a service that has died would be believed for ever. A reader treats a message older than its own maximum age as unknown, so the message is repeated every `refreshS` seconds while the service runs, and a change goes out at once.
- A verdict whose breaches cannot be read publishes nothing: an unknown occupancy is never published as free.
- It only observes. It never changes a verdict, never blocks a request, and a broker that does not answer costs nothing (the failure is counted and printed once).
- Every value is in the file, none in the code. A broker that is not on this machine needs `"internal": true`; `tls` and `caFile` are available. The credentials are read from files and never printed. The topic must be under `hydra/safety/`.

It has been tried with a fake MQTT client and the running service, not with a real broker and a vehicle behind it.
