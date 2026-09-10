@echo off
title Hermes-Antigravity Bridge (LAN / Mobile Mode - Port 8765)
echo ====================================================================
echo   HERMES - ANTIGRAVITY BRIDGE (LAN & MOBILE TERMUX MODE)
echo   1,000,000 Token Native Context | Multi-Device Network Mode
echo   Listening at: http://0.0.0.0:8765
echo ====================================================================
echo.
cd /d "%~dp0"
set PYTHONPATH=%~dp0src;%PYTHONPATH%
set AGY_BRIDGE_HOST=0.0.0.0
set AGY_BRIDGE_ALLOW_REMOTE=true
python -m hermes_antigravity_bridge.cli --config "%USERPROFILE%\.config\hermes-antigravity-bridge\config.toml" serve
pause
