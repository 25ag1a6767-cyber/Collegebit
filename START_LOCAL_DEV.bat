@echo off
setlocal
title ACE Canteen - LOCAL DEVELOPMENT server
cd /d "%~dp0"

echo ==========================================================================
echo  ACE Canteen - LOCAL DEVELOPMENT ONLY
echo  Do not use this launcher for the live canteen. See docs\GO-LIVE.md.
echo ==========================================================================

set "PYTHON_EXE=.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] .venv not found. Run: py -3 -m venv .venv ^&^& .venv\Scripts\pip install -r backend\requirements.txt
    pause
    exit /b 1
)

if not exist ".env" (
    echo [*] Creating .env from .env.example ...
    copy ".env.example" ".env" >nul
)

set "PYTHONPATH=backend"
echo [*] Applying database migrations ...
"%PYTHON_EXE%" -m alembic -c backend\alembic.ini upgrade head
if errorlevel 1 (
    echo [ERROR] Migration failed. Read the message above.
    pause
    exit /b 1
)

echo [*] Ensuring local development sample data (new accounts print their one-time passwords) ...
"%PYTHON_EXE%" scripts\seed_dev_demo.py

echo.
echo [*] Starting http://127.0.0.1:8000  (Ctrl+C to stop)
start "" "http://127.0.0.1:8000"
"%PYTHON_EXE%" -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
pause
