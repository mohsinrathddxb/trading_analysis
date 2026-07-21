@echo off
cd /d "%~dp0python"
call .venv\Scripts\activate.bat
python live_strike_monitor.py --today-and-live
pause
