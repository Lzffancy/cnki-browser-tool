@echo off
setlocal

cd /d "%~dp0"

echo.
echo  CNKI Local Research Assistant - one-click setup
echo  ------------------------------------------------
echo.

REM --- 1. locate python ---
set "PYTHON=python"
where python >nul 2>nul
if errorlevel 1 set "PYTHON=py"
where %PYTHON% >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo         Install Python 3.9+ from https://www.python.org/ and check "Add python.exe to PATH".
    pause
    exit /b 1
)
echo [1/4] Using Python: %PYTHON%

REM --- 2. create venv ---
set "VPY=backend\.venv\Scripts\python.exe"
if not exist "%VPY%" (
    echo [2/4] Creating virtualenv at backend\.venv ...
    %PYTHON% -m venv backend\.venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtualenv.
        pause
        exit /b 1
    )
) else (
    echo [2/4] Virtualenv already exists, skip.
)

REM --- 3. install deps (http mode needs none; install only to enable --mode mcp later) ---
echo [3/4] Installing dependencies ...
"%VPY%" -m pip install --quiet --upgrade pip >nul 2>nul
"%VPY%" -m pip install --quiet -r backend\requirements.txt
if errorlevel 1 (
    echo         [WARN] dependency install failed; HTTP mode still works without "mcp".
)

REM --- 4. start bridge server in a separate window ---
echo [4/4] Starting bridge server in a new window ^(http://127.0.0.1:8765^) ...
start "cnki-local-bridge" "%VPY%" backend\bridge_server.py --mode http

REM --- 5. health check ---
timeout /t 2 /nobreak >nul
echo.
echo  Health check ^(http://127.0.0.1:8765/health^):
curl -s http://127.0.0.1:8765/health
echo.
echo.
echo  ------------------------------------------------------------
echo  NEXT STEPS ^(manual, cannot be automated^):
echo   1. Open chrome://extensions -^> enable "Developer mode"
echo      -^> "Load unpacked" -^> select the "extension" folder.
echo   2. Log in to CNKI ^(https://kns.cnki.net^) and keep a CNKI tab open.
echo   3. Re-check:  curl http://127.0.0.1:8765/health
echo      -^> confirm "extension.connected" becomes true ^(within ~30s^).
echo  ------------------------------------------------------------
echo.
pause
