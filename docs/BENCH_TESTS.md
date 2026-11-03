# Bench tests

Every test below is **not executed**. The emergency-stop request and the safe heartbeat over CAN (`docs/CAN_ESTOP.md`) have run only against a simulated bus; the frame has not been agreed with the firmware or tried on a real CAN bus. Write a result only after a person ran the step.

## What the software already covers (no bus needed)

- The unit tests run the requester and the heartbeat sender against a simulated bus: the heartbeat leaves only while the last verdict is READY and fresh, and a failed send is never counted as sent (`estopTransportError`).
- The identifiers, the period and the 8-byte layout are in `can_estop.json`.

## Setup

- A CAN interface visible to the host (`can0` or a USB-CAN adapter) wired to a bus with two 120 ohm terminations and one receiver that can log frames (a second adapter with `candump` is enough). Nothing that moves may be connected for steps 1 to 5.
- `pip install python-can`, the zones file with the CAN request switched on, and the matching frame in `can_estop.json`.
- For step 6, the firmware of the controller that is meant to stop on the frame, with the actuators disabled.

## Steps

| # | Step | Expected result | Result |
|---|------|-----------------|--------|
| 1 | Start the service with the heartbeat on and a READY verdict. | The logger sees one frame per period with the identifier and the 8 bytes of `can_estop.json`. | not executed |
| 2 | Make the verdict stale (stop the source of the state). | The heartbeat stops within one period after the verdict ages out. | not executed |
| 3 | Make the verdict not READY. | The heartbeat stops at once and the E-STOP request frame is sent. | not executed |
| 4 | Unplug the adapter while the heartbeat runs. | The send is reported as `estopTransportError`, never as sent; the service stays up. | not executed |
| 5 | Restore the bus and the verdict. | The heartbeat resumes only after a fresh READY. | not executed |
| 6 | With the firmware listening, repeat steps 1 to 3. | The firmware stops its actuators when the heartbeat stops and on the request frame; record the delay measured on the bus. | not executed |

## What this bench cannot show

The delay of the whole chain on a loaded bus, the effect of another node using the same identifier and what the firmware does after a reset are properties of the real wiring and firmware; the frame layout must be agreed with the firmware owner before step 6.
