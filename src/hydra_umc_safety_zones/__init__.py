# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - package init: src/hydra_umc_safety_zones/__init__.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""HYDRA-UMC-SAFETY-ZONES - real-time 3D intrusion detection and E-STOP
orchestration for robotic safe-working areas, running as a subsystem of
HYDRA-UMC-VISION-NODE (the integration parent of this project).

This project only detects and requests an E-STOP - it never asserts the
physical stop signal itself. Actually cutting motor power over CAN is
the firmware's (HYDRA-UMC's) responsibility, on hardware certified for
that role; keeping the boundary there means a bug in this Python service
can request a stop but can never fail to deliver one the firmware
doesn't also independently enforce. No `hardware/`/`firmware/`/`os/`/
`models/` folder here for the same reasons as the rest of the Vision AI
Node family: CM5 + Hailo-8 is off-the-shelf hardware, and the shared OS
image / served models live only in the integration parent.

The installed package version is the single source of truth in
pyproject.toml (read at runtime via importlib.metadata), never duplicated
here, so bump_version.py only ever has one place to edit.
"""
