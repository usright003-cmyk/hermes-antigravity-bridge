@echo off
title Stop Hermes-Antigravity Bridge
echo Stopping Hermes-Antigravity Bridge daemon...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
echo Bridge daemon stopped.
timeout /t 2 /nobreak >nul
