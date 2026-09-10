@echo off
title Stop Hermes-Antigravity Bridge
echo Stopping Hermes-Antigravity Bridge daemon...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo Bridge daemon stopped.
timeout /t 2 /nobreak >nul
