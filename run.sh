#!/usr/bin/env bash
# HYDRA-UMC-SAFETY-ZONES - run.sh: runs the entry point from the local venv
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
#
# Runs the project entry point from the local venv created by build.sh.
# Forwards all arguments (e.g. "./run.sh check --zones z.json --detections d.json").
set -euo pipefail
cd "$(dirname "$0")"

if [ -f ".venv/Scripts/python.exe" ]; then
  VENV_PY=".venv/Scripts/python.exe"
elif [ -f ".venv/bin/python" ]; then
  VENV_PY=".venv/bin/python"
else
  echo "No .venv found - run build.sh first." >&2
  exit 1
fi

exec "$VENV_PY" -m hydra_umc_safety_zones.main "$@"
