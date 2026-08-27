@echo off
REM =============================================================================
REM HYDRA-UMC-SAFETY-ZONES - build.bat
REM Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
REM GPL-3.0 - see LICENSE
REM =============================================================================
REM Builds HYDRA-UMC-SAFETY-ZONES: creates/activates a venv, installs the
REM project (editable, with dev extras), verifies it compiles/imports
REM cleanly, and runs the real test suite. Run this before run.bat.
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo  ===============================================================
echo   H Y D R A - U M C - S A F E T Y - Z O N E S  -  build
echo  ===============================================================
echo   Real-time 3D intrusion detection ^& E-STOP orchestrator
echo   Author:  JuanenRac (Electro Hobby 3D)
echo   License: GPL-3.0 (see LICENSE.md)
echo  ===============================================================
echo.

echo [1/5] Bumping version number (odometer bump, see bump_version.py)...
python bump_version.py
if errorlevel 1 ( echo NATIVE VERSION BUMP FAILED. & pause & exit /b 1 )
python "%~dp0bump_manifest_version.py" --sync
if errorlevel 1 ( echo VERSION SYNCHRONIZATION FAILED. & pause & exit /b 1 )
if errorlevel 1 goto :error
echo       Done.
echo.

echo [2/5] Creating/activating virtual environment...
if not exist .venv (
    python -m venv .venv
    if errorlevel 1 goto :error
)
call .venv\Scripts\activate.bat
if errorlevel 1 goto :error
echo       Done.
echo.

echo [3/5] Installing project (editable, with dev extras) into the venv...
python -m pip install --upgrade pip >nul
if errorlevel 1 goto :error
python -m pip install -e ".[dev]"
if errorlevel 1 goto :error
echo       Done.
echo.

echo [4/5] Verifying the package compiles/imports without errors...
python -m compileall -q src
if errorlevel 1 goto :error
python -c "import hydra_umc_safety_zones; print('import OK')"
if errorlevel 1 goto :error
echo       Done.
echo.

echo [5/5] Running the real test suite (pytest)...
python -m pytest tests/ -q
if errorlevel 1 goto :error
echo       Done.
echo.

echo  ===============================================================
echo   Build complete. Run run.bat to execute the entry point.
echo  ===============================================================
echo.
pause
exit /b 0

:error
echo.
echo   BUILD FAILED - see the output above.
pause
exit /b 1
