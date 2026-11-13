# Simulation bench

The safety service can run without a cell, a camera or a person. The simulated cell is a warning zone around a danger zone; a person walks through it on a path drawn from a fixed seed (steps, turns, standing still, running) and the observer that watches the cell sends a heartbeat every step, or fails to. Time is simulated: the stateful monitor and the decision take the clock as an argument, so a shift takes a few milliseconds. The bench keeps its own picture of where the person is and requires the state to be the one that picture demands, and above all never READY when it cannot be.

## Run it

```
python -m pytest -q tests/test_simulation_bench.py
```

## What the scenarios check

| Scenario | What it shows |
|----------|---------------|
| twenty walks of three hundred steps with a healthy observer | the state is exactly the one the position demands, step by step |
| a person walking straight in | the states go ready, warning, danger and back the same way |
| an observer that is stale, inactive, failing, never seen, from the future or missing | never READY, whatever the person does |
| an empty cell | READY only with a live observer |
| a calibration that expires during the shift | the cell leaves READY on the day it expires |
| a shift published through the monitor | every change of state is published, the numbers only grow and the validity is never negative |
| an observer that goes silent | after the heartbeat limit nothing but INHIBITED, and the cell is back to the real state when it returns |
| a verdict nobody renews | it expires into INHIBITED on its own, once |
| a clock that goes backwards, an observer that claims a long life | the heartbeat is never extended |
| many readers and a slow one | the limit of streams is kept, a slow reader keeps only the newest events and does not block the service |
| the same seed twice, another seed | the shift is reproducible and really depends on the seed |

The workflow named "Simulation bench" runs the bench on every change, so the simulated path has its own visible job.

## What it cannot show

The real detector, the real cameras, the real stop circuit and the real timing of a cell need the cell. The status of the repository therefore stays at "simulated" for the hardware row until a person has run the service on the bench.
