@echo off
setlocal
cd /d "%~dp0"

if not exist ".env" (
    echo No .env file found.
    echo Copy .env.example to .env and fill it in first:
    echo     copy .env.example .env
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv .venv || python -m venv .venv
    if errorlevel 1 (
        echo Could not create a virtual environment. Is Python 3.12+ installed?
        pause
        exit /b 1
    )
    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
    ".venv\Scripts\python.exe" -m pip install -e . --quiet
    if errorlevel 1 (
        echo Dependency install failed.
        pause
        exit /b 1
    )
)

echo Starting banbot. Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" -m banbot
set EXITCODE=%ERRORLEVEL%

echo.
echo Bot stopped ^(exit code %EXITCODE%^).
pause
exit /b %EXITCODE%
