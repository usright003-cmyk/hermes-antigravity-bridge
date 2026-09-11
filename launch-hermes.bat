@echo off
title Hermes AI Assistant
curl -s -m 1 http://127.0.0.1:8765/health >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [*] Starting Hermes-Antigravity Bridge in background...
    start "" wscript "%~dp0run-bridge-background.vbs"
    timeout /t 3 /nobreak >nul
)
echo Launching Hermes...
hermes %*
