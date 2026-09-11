@echo off
title Register Hermes-Antigravity Bridge Auto-Start
echo Registering auto-start on logon via Task Scheduler...
schtasks /create /tn "HermesAntigravityBridge" /tr "wscript.exe \"%~dp0run-bridge-background.vbs\"" /sc onlogon /f
if %ERRORLEVEL% EQU 0 (
    echo Successfully registered Hermes-Antigravity Bridge auto-start!
) else (
    echo Failed to register auto-start task.
)
timeout /t 3 /nobreak >nul
