# Hermes-Antigravity Bridge: 1-Click Automated Setup for Windows
$ErrorActionPreference = "Stop"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "   Hermes - Antigravity Bridge Automated Installer" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Cyan

$InstallDir = "$HOME\.local\share\hermes-antigravity-bridge"
if (Test-Path "$HOME\hermes-antigravity-bridge") {
    $InstallDir = "$HOME\hermes-antigravity-bridge"
}

if (-not (Test-Path $InstallDir)) {
    Write-Host "[*] Downloading hermes-antigravity-bridge repository..." -ForegroundColor Yellow
    git clone https://github.com/usright003-cmyk/hermes-antigravity-bridge.git $InstallDir
} else {
    Write-Host "[*] Using existing installation at $InstallDir" -ForegroundColor Yellow
}

cd $InstallDir
python connect_hermes.py

# Create Desktop Shortcut
$Desktop = [Environment]::GetFolderPath("Desktop")
if (Test-Path $Desktop) {
    Copy-Item "$InstallDir\run-bridge.bat" "$Desktop\Run-Antigravity-Bridge.bat" -Force
    Write-Host "[+] Desktop launcher created: $Desktop\Run-Antigravity-Bridge.bat" -ForegroundColor Green
}

# Create Global commands in user profile
Copy-Item "$InstallDir\connect_hermes.py" "$HOME\connect_hermes.py" -Force
Copy-Item "$InstallDir\run-bridge.bat" "$HOME\run-bridge.bat" -Force

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " Setup Complete! You can now run:" -ForegroundColor Green
Write-Host " 1. Double click 'Run-Antigravity-Bridge.bat' on your Desktop" -ForegroundColor White
Write-Host " 2. Type 'hermes' in any terminal to start chatting!" -ForegroundColor White
Write-Host "==========================================================" -ForegroundColor Cyan
