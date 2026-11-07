@echo off
setlocal
title ACE Canteen - test suite and release gate
cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"
set "PYTHONPATH=backend"
echo Running backend tests, compilation and JavaScript syntax checks ...
"%PYTHON_EXE%" scripts\release_gate.py
pause
