@echo off
setlocal
cd /d "%~dp0python"

where python >nul 2>&1
if errorlevel 1 (
    echo Python is not available in PATH.
    echo Install Python first, then run this file again.
    pause
    exit /b 1
)

if not exist .venv (
    echo Creating Python virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if not exist .env (
    copy .env.example .env >nul
    echo.
    echo Created python\.env. Open it and enter your Telegram API values.
)

echo.
echo Python engine setup completed.
echo Python executable:
echo %CD%\.venv\Scripts\python.exe
pause
