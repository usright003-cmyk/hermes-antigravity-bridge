# Hermes-Antigravity Bridge: 1-Click Automated Setup for Windows
$ErrorActionPreference = "Stop"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "   Hermes - Antigravity Bridge Automated Installer" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Locate Python executable
$PythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCmd) {
    $PythonCmd = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $PythonCmd) {
    Write-Error "Python is not installed or not found in PATH. Please install Python 3.10+ from https://python.org"
    exit 1
}
$PythonExe = $PythonCmd.Source
Write-Host "[+] Python executable: $PythonExe" -ForegroundColor Green

# 2. Check Antigravity CLI ('agy') and auto-install if missing
$AgyCmd = Get-Command agy -ErrorAction SilentlyContinue
$DefaultAgy = "$HOME\AppData\Local\agy\bin\agy.exe"
$DefaultAgyCmd = "$HOME\AppData\Local\agy\bin\agy.cmd"
if (-not $AgyCmd -and (Test-Path $DefaultAgy)) {
    $AgyPath = $DefaultAgy
} elseif (-not $AgyCmd -and (Test-Path $DefaultAgyCmd)) {
    $AgyPath = $DefaultAgyCmd
} elseif ($AgyCmd) {
    $AgyPath = $AgyCmd.Source
} else {
    $AgyPath = $null
}

if (-not $AgyPath) {
    Write-Host "[*] Antigravity CLI ('agy') not found on system." -ForegroundColor Yellow
    Write-Host "[*] Installing Antigravity CLI via official Google script (irm https://antigravity.google/cli/install.ps1 | iex)..." -ForegroundColor Yellow
    try {
        irm https://antigravity.google/cli/install.ps1 | iex
        $NewAgyCmd = Get-Command agy -ErrorAction SilentlyContinue
        if ($NewAgyCmd) {
            $AgyPath = $NewAgyCmd.Source
        } elseif (Test-Path $DefaultAgy) {
            $AgyPath = $DefaultAgy
        } elseif (Test-Path $DefaultAgyCmd) {
            $AgyPath = $DefaultAgyCmd
        }
        $AgyBinDir = "$HOME\AppData\Local\agy\bin"
        if ((Test-Path $AgyBinDir) -and ($env:Path -notlike "*$AgyBinDir*")) {
            $env:Path = "$AgyBinDir;$env:Path"
        }
    } catch {
        Write-Host "[!] Auto-installation of Antigravity CLI encountered an issue: $_" -ForegroundColor Yellow
        Write-Host "[*] If needed, install manually from https://antigravity.google" -ForegroundColor Yellow
    }
}
if ($AgyPath) {
    Write-Host "[+] Antigravity CLI ready: $AgyPath" -ForegroundColor Green
}

# 3. Determine installation directory
$InstallDir = "$HOME\.local\share\hermes-antigravity-bridge"
if (Test-Path "$HOME\hermes-antigravity-bridge") {
    $InstallDir = "$HOME\hermes-antigravity-bridge"
}
if ((Test-Path ".\connect_hermes.py") -and (Test-Path ".\pyproject.toml")) {
    $InstallDir = (Get-Item .).FullName
}

if (-not (Test-Path $InstallDir)) {
    Write-Host "[*] Downloading hermes-antigravity-bridge repository to $InstallDir..." -ForegroundColor Yellow
    git clone https://github.com/usright003-cmyk/hermes-antigravity-bridge.git $InstallDir
} else {
    Write-Host "[*] Using installation at $InstallDir" -ForegroundColor Yellow
}

Set-Location $InstallDir

# 4. Install bridge package into active Python environment
Write-Host "[*] Installing hermes-antigravity-bridge into active Python environment..." -ForegroundColor Yellow
& $PythonExe -m pip install -e .

# 5. Check Hermes CLI and install if missing
$HermesCmd = Get-Command hermes -ErrorAction SilentlyContinue
if (-not $HermesCmd) {
    Write-Host "[*] Hermes Agent CLI ('hermes') not found. Installing hermes-agent..." -ForegroundColor Yellow
    & $PythonExe -m pip install -U hermes-agent
    $HermesCmd = Get-Command hermes -ErrorAction SilentlyContinue
}
if ($HermesCmd) {
    Write-Host "[+] Hermes Agent CLI ready: $($HermesCmd.Source)" -ForegroundColor Green
}

# 6. Run connect_hermes.py with credentials sync
Write-Host "[*] Running bridge connector..." -ForegroundColor Yellow
& $PythonExe connect_hermes.py --sync-credentials

# 7. Create Desktop Shortcuts & Launchers with absolute paths to avoid ModuleNotFoundError
$Desktop = [Environment]::GetFolderPath("Desktop")
if (-not (Test-Path $Desktop) -and (Test-Path "$HOME\Desktop")) {
    $Desktop = "$HOME\Desktop"
}
if (-not (Test-Path $Desktop) -and $env:OneDrive -and (Test-Path "$env:OneDrive\Desktop")) {
    $Desktop = "$env:OneDrive\Desktop"
}

if (Test-Path $Desktop) {
    $DesktopBat = "$Desktop\Run-Antigravity-Bridge.bat"
    $DesktopLanBat = "$Desktop\Run-Antigravity-Bridge-LAN.bat"
    $DesktopSilentVbs = "$Desktop\Run-Antigravity-Bridge-Background.vbs"
    $DesktopLaunchHermes = "$Desktop\Launch-Hermes.bat"
    $DesktopStopBat = "$Desktop\Stop-Antigravity-Bridge.bat"

    # Standard Console Launcher
    $batContent = @"
@echo off
title Hermes-Antigravity Bridge (Port 8765)
echo ====================================================================
echo   HERMES - ANTIGRAVITY BRIDGE
echo   1,000,000 Token Native Context | Gemini 3.8 Flash (High)
echo   Listening at: http://127.0.0.1:8765
echo ====================================================================
echo.
cd /d "$InstallDir"
set PYTHONPATH=$InstallDir\src;%PYTHONPATH%
"$PythonExe" -m hermes_antigravity_bridge.cli --config "%USERPROFILE%\.config\hermes-antigravity-bridge\config.toml" serve
pause
"@
    Set-Content -Path $DesktopBat -Value $batContent -Encoding ASCII

    # LAN Launcher
    $lanBatContent = @"
@echo off
title Hermes-Antigravity Bridge (LAN / Mobile Mode - Port 8765)
echo ====================================================================
echo   HERMES - ANTIGRAVITY BRIDGE (LAN & MOBILE TERMUX MODE)
echo   1,000,000 Token Native Context | Multi-Device Network Mode
echo   Listening at: http://0.0.0.0:8765
echo ====================================================================
echo.
cd /d "$InstallDir"
set PYTHONPATH=$InstallDir\src;%PYTHONPATH%
set AGY_BRIDGE_HOST=0.0.0.0
set AGY_BRIDGE_ALLOW_REMOTE=true
"$PythonExe" -m hermes_antigravity_bridge.cli --config "%USERPROFILE%\.config\hermes-antigravity-bridge\config.toml" serve
pause
"@
    Set-Content -Path $DesktopLanBat -Value $lanBatContent -Encoding ASCII

    # Silent / Windowless Background Launcher (.vbs)
    $vbsContent = @"
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "$InstallDir"
Set WshProcessEnv = WshShell.Environment("Process")
WshProcessEnv("PYTHONPATH") = "$InstallDir\src"
UserProfile = WshShell.ExpandEnvironmentStrings("%USERPROFILE%")
ConfigFile = UserProfile & "\.config\hermes-antigravity-bridge\config.toml"
PythonCmd = Chr(34) & "$PythonExe" & Chr(34) & " -m hermes_antigravity_bridge.cli --config " & Chr(34) & ConfigFile & Chr(34) & " serve"
WshShell.Run PythonCmd, 0, False
"@
    Set-Content -Path $DesktopSilentVbs -Value $vbsContent -Encoding ASCII

    # Smart 1-Click Hermes Launcher (auto-starts bridge if needed)
    $launchHermesContent = @"
@echo off
title Hermes AI Assistant
curl -s -m 1 http://127.0.0.1:8765/health >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [*] Starting Hermes-Antigravity Bridge in background...
    start "" wscript "%USERPROFILE%\Desktop\Run-Antigravity-Bridge-Background.vbs"
    timeout /t 3 /nobreak >nul
)
echo Launching Hermes...
hermes %*
"@
    Set-Content -Path $DesktopLaunchHermes -Value $launchHermesContent -Encoding ASCII

    # Clean Teardown Script for Background Bridge
    $stopBatContent = @"
@echo off
title Stop Hermes-Antigravity Bridge
echo Stopping Hermes-Antigravity Bridge daemon...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo Bridge daemon stopped.
timeout /t 2 /nobreak >nul
"@
    Set-Content -Path $DesktopStopBat -Value $stopBatContent -Encoding ASCII

    Write-Host "[+] Desktop launcher created: $DesktopBat" -ForegroundColor Green
    Write-Host "[+] Desktop LAN launcher created: $DesktopLanBat" -ForegroundColor Green
    Write-Host "[+] Desktop background/silent launcher created: $DesktopSilentVbs" -ForegroundColor Green
    Write-Host "[+] Desktop Smart Hermes launcher created: $DesktopLaunchHermes" -ForegroundColor Green
    Write-Host "[+] Desktop Stop Bridge launcher created: $DesktopStopBat" -ForegroundColor Green
}

# Also ensure repository root has all launchers and sync to user profile
Copy-Item "$InstallDir\run-bridge.bat" "$HOME\run-bridge.bat" -Force -ErrorAction SilentlyContinue
Copy-Item "$InstallDir\run-bridge-lan.bat" "$HOME\run-bridge-lan.bat" -Force -ErrorAction SilentlyContinue
if (Test-Path "$InstallDir\run-bridge-background.vbs") {
    Copy-Item "$InstallDir\run-bridge-background.vbs" "$HOME\run-bridge-background.vbs" -Force -ErrorAction SilentlyContinue
}
if ($DesktopLaunchHermes -and (Test-Path $DesktopLaunchHermes)) {
    Copy-Item $DesktopLaunchHermes "$HOME\Launch-Hermes.bat" -Force -ErrorAction SilentlyContinue
}
if ($DesktopStopBat -and (Test-Path $DesktopStopBat)) {
    Copy-Item $DesktopStopBat "$HOME\Stop-Antigravity-Bridge.bat" -Force -ErrorAction SilentlyContinue
}

# 6. Auto-start background bridge daemon immediately
Write-Host "[*] Ensuring Hermes-Antigravity Bridge background daemon is active..." -ForegroundColor Cyan
$bridgeListening = $false
try {
    $res = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 1 -ErrorAction SilentlyContinue
    if ($res.status -eq "ok") { $bridgeListening = $true }
} catch {}

if (-not $bridgeListening -and $DesktopSilentVbs -and (Test-Path $DesktopSilentVbs)) {
    Start-Process -FilePath "wscript.exe" -ArgumentList "`"$DesktopSilentVbs`""
    $retries = 10
    while ($retries -gt 0 -and -not $bridgeListening) {
        Start-Sleep -Seconds 1
        try {
            $res = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 2 -ErrorAction Stop
            if ($res.status -eq "ok") { $bridgeListening = $true }
        } catch {}
        $retries--
    }
}

if ($bridgeListening) {
    Write-Host "[+] Bridge daemon is ACTIVE and listening on http://127.0.0.1:8765" -ForegroundColor Green
} else {
    Write-Host "[*] Bridge daemon starting in background. Use 'Launch-Hermes.bat' to start chatting." -ForegroundColor Yellow
}

# 7. Register persistent auto-start on Windows login (Startup folder)
$StartupDir = [Environment]::GetFolderPath("Startup")
if ($DesktopSilentVbs -and (Test-Path $StartupDir) -and (Test-Path $DesktopSilentVbs)) {
    Copy-Item -Path $DesktopSilentVbs -Destination "$StartupDir\hermes-antigravity-bridge.vbs" -Force -ErrorAction SilentlyContinue
    Write-Host "[+] Configured auto-start on Windows boot: $StartupDir\hermes-antigravity-bridge.vbs" -ForegroundColor Green
}

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " Setup Complete! Hermes is now connected and ready to chat." -ForegroundColor Green
Write-Host " -> Bridge daemon is running in the background (Port 8765)." -ForegroundColor White
Write-Host " -> Type 'hermes' in any terminal to start chatting immediately!" -ForegroundColor White
Write-Host " -> Or double-click 'Launch-Hermes.bat' on your Desktop." -ForegroundColor White
Write-Host " -> Automatically starts whenever Windows boots." -ForegroundColor White
Write-Host "==========================================================" -ForegroundColor Cyan
