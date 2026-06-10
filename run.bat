@echo off
cd /d "%~dp0"

echo Gmail Auto Sender
echo =================

python --version >nul 2>&1
if errorlevel 1 (
    echo Python is not installed or not on PATH.
    echo Install Python 3.10+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Installing dependencies...
python -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)

echo Starting app...
python main.py

if errorlevel 1 (
    echo.
    echo The app exited with an error.
    pause
)
