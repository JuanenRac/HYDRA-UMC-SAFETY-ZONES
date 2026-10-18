# Contributing to HYDRA-UMC-SAFETY-ZONES 🦾

We welcome contributions to the critical safety subsystem of the Vision AI Node.

## Technology Stack
- **Language**: Python 3.10 or newer, standard library only (no third-party runtime dependency); `pytest` for the tests.
- **Standards**: ISO 13849-1 (Functional Safety) is the aspiration this project is written towards; it has not been assessed against it.
- **Geometry**: axis-aligned boxes and spheres, in plain floats (`geometry.py`).
- **Hardware**: none in this repository. The Hailo-8 perception and the CAN E-STOP live elsewhere (see the README).

## Guidelines
1. **Safety First**: Any change to the E-STOP logic must be Peer Reviewed by at least two senior developers.
2. **Fail closed**: an input that is missing, malformed or not finite must end as `INHIBITED` (or an error), never as `READY`. Add a test that proves it.
3. **Deterministic Logic**: Avoid non-deterministic algorithms in the critical safety path.
4. **Validation**: `python -m pytest tests/` must pass; there is no simulator involved.
