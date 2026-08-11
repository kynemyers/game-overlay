@echo off
rem Runs GameOverlay directly with Python - no packed .exe, so antivirus has
rem nothing to flag. Needs Python 3.10+ from https://python.org
rem (tick "Add python.exe to PATH" during install).
rem
rem Launches with pythonw and exits, so no console window is left behind.
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found.
    echo.
    echo Install it from https://www.python.org/downloads/ and be sure to tick
    echo   "Add python.exe to PATH"
    echo on the first screen of the installer, then run this again.
    echo.
    pause
    exit /b 1
)

rem Install psutil only if it is missing, so normal launches are instant.
python -c "import psutil" >nul 2>&1
if errorlevel 1 (
    echo First run: installing the one dependency ^(psutil^)...
    python -m pip install --quiet --user psutil
)

rem pythonw = no console window. The app raises its own admin (UAC) prompt;
rem accept it, or CPU temperature, FPS and ping detection will not work.
start "" pythonw overlay.py
exit
