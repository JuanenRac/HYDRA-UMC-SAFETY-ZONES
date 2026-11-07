# Record trigger

The camera streamer keeps a ring of the last frames and writes nothing until a safety service tells it to. This service is that safety service: when a verdict becomes `SAFE_STOP` it sends `POST <camera>/record/trigger` with `{"reason": "safe stop: ...", "source": "safety-zones"}` to every camera in the list, once per stop (the state has to leave `SAFE_STOP` before the next trigger).

It is **off by default**. Switch it on in the zones file:

```json
{ "recordTrigger": { "enabled": true, "targets": ["http://127.0.0.1:8080/cam0/record/trigger"], "tokenFile": "/etc/hydra-umc/streamer.token", "timeoutS": 2.0, "minIntervalS": 5.0 } }
```

- A target that is not on this machine needs `"internal": true`. The bearer token is read from `tokenFile` and never printed.
- The request goes out from a thread of its own with a short timeout: a camera that does not answer costs the verdict nothing, and a failure is counted and printed once. A `429` (the streamer is inside its own minimum gap) is not a failure.
- The reason is printable ASCII of at most 120 characters, which is what the streamer accepts.

It has been tried against a stand-in streamer, not a real streamer with a real camera.
