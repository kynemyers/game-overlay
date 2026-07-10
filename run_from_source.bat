@echo off
rem Runs GameOverlay directly with Python - no packed .exe, so antivirus
rem has nothing to flag. Needs Python 3.10+ from https://python.org
rem (tick "Add python.exe to PATH" during install).
cd /d "%~dp0"
echo Installing the one dependency (psutil)...
python -m pip install --quiet --user psutil
echo Starting GameOverlay (accept the admin prompt)...
python overlay.py
pause
