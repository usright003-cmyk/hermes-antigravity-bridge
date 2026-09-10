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

    Write-Host "[+] Desktop launcher created: $DesktopBat" -ForegroundColor Green
    Write-Host "[+] Desktop LAN launcher created: $DesktopLanBat" -ForegroundColor Green
    Write-Host "[+] Desktop background/silent launcher created: $DesktopSilentVbs" -ForegroundColor Green
}

# Also ensure repository root has all launchers and sync to user profile
Copy-Item "$InstallDir\run-bridge.bat" "$HOME\run-bridge.bat" -Force -ErrorAction SilentlyContinue
Copy-Item "$InstallDir\run-bridge-lan.bat" "$HOME\run-bridge-lan.bat" -Force -ErrorAction SilentlyContinue
if (Test-Path "$InstallDir\run-bridge-background.vbs") {
    Copy-Item "$InstallDir\run-bridge-background.vbs" "$HOME\run-bridge-background.vbs" -Force -ErrorAction SilentlyContinue
}

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " Setup Complete! You can now run:" -ForegroundColor Green
Write-Host " 1. Double click 'Run-Antigravity-Bridge.bat' on Desktop (console window)" -ForegroundColor White
Write-Host " 2. OR double click 'Run-Antigravity-Bridge-Background.vbs' (silent/windowless)" -ForegroundColor White
Write-Host " 3. (Optional) Run 'Run-Antigravity-Bridge-LAN.bat' to connect from Android/Termux" -ForegroundColor White
Write-Host " 4. Type 'hermes' in any terminal to start chatting!" -ForegroundColor White
Write-Host "==========================================================" -ForegroundColor Cyan
