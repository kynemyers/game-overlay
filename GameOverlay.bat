@echo off
rem Launch GameOverlay from source (no console window).
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" overlay.py
) else (
    start "" pythonw overlay.py
)
