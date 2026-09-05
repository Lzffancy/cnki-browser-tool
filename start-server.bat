@echo off
setlocal
cd /d "%~dp0"

set "VPY=backend\.venv\Scripts\python.exe"
if not exist "%VPY%" (
    echo [ERROR] virtualenv not found. Please run setup.bat first.
    pause
    exit /b 1
)

echo Starting cnki-local-bridge at http://127.0.0.1:8765 ...
start "cnki-local-bridge" "%VPY%" "backend\bridge_server.py" --mode http

timeout /t 2 /nobreak >nul
echo.
echo Health check:
curl -s http://127.0.0.1:8765/health
echo.
echo.
echo To stop the server, close the "cnki-local-bridge" window
echo or run: taskkill /F /FI "WINDOWTITLE eq cnki-local-bridge*"
echo.
pause
