@echo off
title Unregister Hermes-Antigravity Bridge Auto-Start
echo Removing auto-start task...
schtasks /delete /tn "HermesAntigravityBridge" /f
echo Auto-start task removed.
timeout /t 3 /nobreak >nul
