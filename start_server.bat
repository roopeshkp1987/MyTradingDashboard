@echo off
title NSE Market Dashboard
color 0A
cls

echo.
echo  ╔══════════════════════════════════════════════════╗
echo  ║        NSE Market Dashboard — Launcher           ║
echo  ╚══════════════════════════════════════════════════╝
echo.

:: Change to the script's own directory (works from anywhere)
cd /d "%~dp0"

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found. Please install Python 3.8+
    echo.
    pause
    exit /b 1
)

:: Check server.py exists
if not exist "server.py" (
    echo  [ERROR] server.py not found in %CD%
    echo.
    pause
    exit /b 1
)

echo  [OK]   Python found
echo  [OK]   server.py found
echo  [INFO] Starting server on http://localhost:8080
echo  [INFO] Opening browser in 2 seconds...
echo  [INFO] Press Ctrl+C to stop the server
echo.

:: Open browser after a short delay (runs in background)
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:8080"

:: Start the server (foreground)
python server.py

echo.
echo  [INFO] Server stopped.
pause
