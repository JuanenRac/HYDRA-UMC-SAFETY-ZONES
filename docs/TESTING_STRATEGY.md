# Testing strategy: property-based tests and mutation checks of the safety decision and the signature of the safety state

The services that act on the cell read its verdict (`sdkSafetyState`) and its signature, and treat anything that is not a valid, fresh, newer signed `READY` as `INHIBITED`. The decision (`safety_state.py`) and the signature (`signing.py`) are tested with property-based tests besides the hand-written cases.

## Property-based tests

`tests/test_property_safety.py` states rules that must hold for every input and checks them on generated cases. The cases come from a small generator with a fixed seed (`random.Random`), so a run is the same on every machine, nothing extra is installed to run it, and the whole file takes about one second. The rules:

| Area | Rule checked on generated inputs |
| --- | --- |
| Signature | a verdict whose state, source, counter, signature, key id, algorithm or fields were changed, or that lost a field, is never read as signed (`INHIBITED`); a verdict signed with the other algorithm is never accepted by a consumer that expects one |
| Malformed | any odd value in place of the state or the signature is `INHIBITED` |
| Freshness | the age limit is exact and symmetric (an old verdict and one from the future) |
| Replay | an older counter is a replay; an equal one only when repeats are allowed; the counter of a signer only goes up, whatever the clock does |
| Keys | a key shorter than 32 characters and a key id outside 1 to 64 letters, digits and `. _ -` are refused |
| Decision | `READY` needs a valid calibration, a healthy, active and fresh observer, no breach, and the separation distance evaluated when the zones file requires it; with less than that the state is never `READY`; an observer error is a fault |
| K, T and C | a zone whose separation distance was not evaluated (no K, T and C) releases nothing when the zones file requires it, and the same cell with the figures given is released |
| SDK state | the translation to the SDK vocabulary is conservative: `READY` only for an internal `ready` with no fault, a warning is `INHIBITED`, a danger is `SAFE_STOP`, a fault is `FAULT` |

## Mutation checks (local only)

Mutation testing changes the code of a module in small ways (a `<` for a `<=`, a constant, a condition) and runs the tests: a change the tests do not notice is a behaviour nobody checks. It is a measure of the tests, not a part of the build: it is **not** run in the CI, and a run takes several minutes. To repeat it, in a throwaway virtual environment (mutmut 2 counts only the exit code 1 of the test command as a kill, so use a runner that maps every failing exit code to 1):

```
python -m pip install "mutmut<3" pytest cryptography
mutmut run --paths-to-mutate src/hydra_umc_safety_zones/signing.py --runner "python runner.py tests/test_property_safety.py tests/test_signing_and_can.py tests/test_signing_ed25519.py"
mutmut results
```

Two runs, one per module. In `signing.py` only the message, the signing of both algorithms, the reading of the time, `verify` and `effective_state` were mutated (the key files and the key generation are marked `# pragma: no mutate` in a scratch copy); in `safety_state.py` only `evaluate_safety` and the translation to the SDK state.

| Target | Mutants | Killed | Left |
| --- | --- | --- | --- |
| `signing.py` (message, signing, reading of the time, `verify`, `effective_state`) | 115 | 106 (92.2 %) | 9 |
| `safety_state.py` (`evaluate_safety`, translation to the SDK state) | 65 | 63 (96.9 %) | 2 |

Mutants that survived while the tests were being written showed that the text under the signature was not pinned (the signer and the verifier agreed with any format), so the tests now compare it with the exact text the consumers rebuild and with an independent HMAC; they also check the reasons of the refusals, the exact step of the counter, the ways of writing a time and the defaults of the functions. The mutants left in the last measured run are the replacement of a trailing `Z` (Python 3.11 and later read it anyway), the reasons of the Ed25519 refusals, the real clock used when none is given, the default of `allow_repeat` in `effective_state`, a signed state that is not text, the annotation of a variable and the text of a success; the tests for the reasons, the clock and the two defaults were added after that run and were not measured again.
