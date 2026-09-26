@echo off
REM Double-click to start Switchboard AI. Creates a venv on first run.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :error
)

cd ..
REM Open the UI once the server has had a moment to start.
start "" /b cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:8000/"
"%~dp0.venv\Scripts\python.exe" -m switchboard_ai server %*
goto :eof

:error
echo.
echo Setup failed. Make sure Python 3.10+ is installed and on PATH.
pause
