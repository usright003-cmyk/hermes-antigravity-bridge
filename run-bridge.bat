@echo off
title Hermes-Antigravity Bridge (Port 8765)
echo ====================================================================
echo   HERMES - ANTIGRAVITY BRIDGE
echo   1,000,000 Token Native Context ^| Gemini 3.8 Flash (High)
echo   Listening at: http://127.0.0.1:8765
echo ====================================================================
echo.
cd /d "E:\hermes-antigravity-bridge"
set PYTHONPATH=src
python -m hermes_antigravity_bridge.cli --config "%USERPROFILE%\.config\hermes-antigravity-bridge\config.toml" serve
pause
