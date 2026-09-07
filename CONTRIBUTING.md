# Contributing to HYDRA-UMC-SAFETY-ZONES 🦾

We welcome contributions to the critical safety subsystem of the Vision AI Node.

## Technology Stack
- **Language**: Python 3.12, C++20.
- **Standards**: ISO 13849-1 (Functional Safety).
- **Hardware**: Hailo-8 NPU, STM32H7 (CAN Gateway).
- **Geometry**: OpenCV, Shapely (for 2D/3D projection).

## Guidelines
1. **Safety First**: Any change to the E-STOP logic must be Peer Reviewed by at least two senior developers.
2. **Latency Constraints**: The entire safety loop must remain under 5ms.
3. **Deterministic Logic**: Avoid non-deterministic algorithms in the critical safety path.
4. **Validation**: Test all new safety zones in the `HYDRA-UMC-TWIN` simulator before physical deployment.
