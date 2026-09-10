#!/usr/bin/env bash
# ==============================================================================
# Hermes Agent - Android / Termux Connector & Standalone Installer
# ==============================================================================
set -euo pipefail

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
PURPLE='\033[0;35m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${CYAN}================================================================${NC}"
echo -e "${GREEN}   📱 Hermes Agent: Android / Termux Superintelligence          ${NC}"
echo -e "${CYAN}================================================================${NC}"

# Parse flags
STANDALONE=0
ENDPOINT=""
TOKEN=""
MODEL="gemini-3.8-flash"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --standalone)
            STANDALONE=1
            shift
            ;;
        --remote)
            STANDALONE=0
            shift
            ;;
        --endpoint)
            ENDPOINT="$2"
            shift 2
            ;;
        --token)
            TOKEN="$2"
            shift 2
            ;;
        --model)
            MODEL="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

# If no mode specified and no endpoint provided, prompt user interactively
if [[ $STANDALONE -eq 0 && -z "$ENDPOINT" ]]; then
    echo ""
    echo -e "${PURPLE}Choose Installation Mode for Android:${NC}"
    echo -e "  ${GREEN}1) [100% STANDALONE ON MOBILE] No PC Needed! Run everything inside Termux.${NC}"
    echo -e "  ${BLUE}2) [REMOTE CLIENT] Connect to a Bridge running on your PC or Cloud VPS.${NC}"
    echo ""
    read -rp "Enter choice [1/2, default: 1]: " MODE_CHOICE
    MODE_CHOICE="${MODE_CHOICE:-1}"

    if [[ "$MODE_CHOICE" == "1" ]]; then
        STANDALONE=1
    fi
fi

# ==============================================================================
# MODE 1: 100% STANDALONE ON ANDROID (NO PC NEEDED)
# ==============================================================================
if [[ $STANDALONE -eq 1 ]]; then
    echo ""
    echo -e "${CYAN}================================================================${NC}"
    echo -e "${GREEN}[*] Setting up 100% Standalone Hermes + Antigravity on Android...${NC}"
    echo -e "${CYAN}================================================================${NC}"

    # Step 1: Install PRoot-Distro in Termux
    echo -e "${YELLOW}[1/5] Checking Termux environment & PRoot Linux...${NC}"
    if command -v pkg >/dev/null 2>&1; then
        pkg update -y || true
        pkg install -y proot-distro git curl python || true
    fi

    if ! proot-distro list 2>/dev/null | grep -q "ubuntu.*installed"; then
        echo -e "${YELLOW}[2/5] Installing Ubuntu Linux environment in Termux (no root needed)...${NC}"
        proot-distro install ubuntu
    else
        echo -e "${GREEN}[+] Ubuntu Linux environment already ready in Termux.${NC}"
    fi

    # Step 2: Install tools inside Ubuntu
    echo -e "${YELLOW}[3/5] Setting up Antigravity CLI and Python environment...${NC}"
    proot-distro login ubuntu -- bash -c '
        set -e
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -y >/dev/null 2>&1
        apt-get install -y python3 python3-pip python3-venv git curl ca-certificates >/dev/null 2>&1

        # Install Antigravity CLI
        if ! command -v agy >/dev/null 2>&1; then
            echo "[*] Downloading Google Antigravity CLI for ARM64..."
            curl -fsSL https://antigravity.google/cli/install.sh | bash || true
        fi

        export PATH="$HOME/.local/bin:$PATH"

        # Clone and configure bridge
        if [ ! -d "$HOME/hermes-antigravity-bridge" ]; then
            git clone https://github.com/usright003-cmyk/hermes-antigravity-bridge.git "$HOME/hermes-antigravity-bridge"
        else
            cd "$HOME/hermes-antigravity-bridge" && git pull || true
        fi

        cd "$HOME/hermes-antigravity-bridge"
        python3 -m pip install -e . --break-system-packages >/dev/null 2>&1 || python3 -m pip install -e . >/dev/null 2>&1 || true

        # Install Hermes Agent
        python3 -m pip install hermes-agent pyyaml requests --break-system-packages >/dev/null 2>&1 || python3 -m pip install hermes-agent pyyaml requests >/dev/null 2>&1 || true

        # Connect Hermes and Bridge
        python3 connect_hermes.py
    '

    # Step 3: Create one-command mobile launcher
    echo -e "${YELLOW}[4/5] Creating 1-command mobile launcher: 'start-hermes'...${NC}"
    LAUNCHER="/data/data/com.termux/files/usr/bin/start-hermes"
    if [ ! -d "/data/data/com.termux/files/usr/bin" ]; then
        LAUNCHER="$HOME/start-hermes"
    fi

    cat << 'EOF' > "$LAUNCHER"
#!/data/data/com.termux/files/usr/bin/bash
echo -e "\033[0;36m=====================================================\033[0m"
echo -e "\033[0;32m   Starting Hermes & Antigravity on Android...       \033[0m"
echo -e "\033[0;36m=====================================================\033[0m"
proot-distro login ubuntu -- bash -c '
    export PATH="$HOME/.local/bin:$PATH"

    # Verify Antigravity Auth
    if [ ! -f "$HOME/.gemini/antigravity-cli/jetski_state.pbtxt" ]; then
        echo "[*] First time Google Antigravity login required."
        echo "[*] Tap or copy the Google authorization link below to sign in on your phone browser:"
        agy || true
    fi

    # Start bridge server daemon in background
    pkill -f "hermes_antigravity_bridge.cli serve" >/dev/null 2>&1 || true
    python3 -m hermes_antigravity_bridge.cli --config "$HOME/.config/hermes-antigravity-bridge/config.toml" serve > /tmp/bridge.log 2>&1 &
    
    BRIDGE_READY=0
    for i in 1 2 3 4 5; do
        sleep 1
        if curl -s -f http://127.0.0.1:8765/health >/dev/null 2>&1; then
            BRIDGE_READY=1
            break
        fi
    done

    if [ "$BRIDGE_READY" -ne 1 ]; then
        echo -e "\033[0;31m[!] Error: Bridge daemon failed to start on http://127.0.0.1:8765\033[0m"
        echo -e "\033[1;33m--- Daemon Log (/tmp/bridge.log) ---\033[0m"
        cat /tmp/bridge.log 2>/dev/null || true
        echo -e "\033[1;33m-------------------------------------\033[0m"
        exit 1
    fi

    # Launch Hermes interactive session
    hermes
'
EOF
    chmod +x "$LAUNCHER"

    # Step 4: Termux-API tip
    echo -e "${YELLOW}[5/5] Checking hardware integration...${NC}"
    if command -v termux-battery-status >/dev/null 2>&1; then
        echo -e "${GREEN}[+] Termux:API detected! Hermes can control Android phone sensors and SMS.${NC}"
    else
        echo -e "${PURPLE}[💡 TIP] Run 'pkg install termux-api' to let Hermes check battery, trigger vibration & send alerts!${NC}"
    fi

    echo ""
    echo -e "${CYAN}================================================================${NC}"
    echo -e "${GREEN}🎉 100% STANDALONE SETUP COMPLETE ON YOUR PHONE!                ${NC}"
    echo -e "${CYAN}================================================================${NC}"
    echo -e "You NEVER need a PC. Everything runs directly on your Android!"
    echo ""
    echo -e "👉 Whenever you want to chat, simply open Termux and type:"
    echo -e "   ${GREEN}start-hermes${NC}"
    echo -e "${CYAN}================================================================${NC}"
    exit 0
fi

# ==============================================================================
# MODE 2: REMOTE PC / SERVER BRIDGE CLIENT
# ==============================================================================
echo ""
echo -e "${YELLOW}[*] Remote PC / Server Bridge Configuration...${NC}"

if [[ -z "$ENDPOINT" ]]; then
    read -rp "Enter Bridge IP & Port [e.g. http://192.168.0.5:8765/v1]: " ENDPOINT
    read -rp "Enter Bridge Bearer Token: " TOKEN
fi

if [[ -z "$ENDPOINT" || -z "$TOKEN" ]]; then
    echo -e "\033[0;31m[!] Error: Both endpoint and token are required for remote mode.\033[0m"
    exit 1
fi

ENDPOINT="${ENDPOINT%/}"
if [[ "$ENDPOINT" != */v1 ]]; then
    ENDPOINT="${ENDPOINT}/v1"
fi

# Ensure basic packages in Termux
pkg install -y python git curl || true
python3 -m pip install --upgrade pip pyyaml requests >/dev/null 2>&1 || true

# Write Hermes config
python3 - "$ENDPOINT" "$TOKEN" "$MODEL" << 'PY_HERMES'
import sys
from pathlib import Path

endpoint, token, model = sys.argv[1], sys.argv[2], sys.argv[3]
hermes_dir = Path.home() / ".hermes"
hermes_dir.mkdir(parents=True, exist_ok=True)
config_file = hermes_dir / "config.yaml"

try:
    import yaml
except ImportError:
    yaml = None

cfg = {}
if config_file.exists() and yaml:
    try:
        with config_file.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}

if not isinstance(cfg, dict):
    cfg = {}

if "model" not in cfg or not isinstance(cfg["model"], dict):
    cfg["model"] = {}

cfg["model"]["default"] = model
cfg["model"]["provider"] = "custom:antigravity"
cfg["model"]["base_url"] = endpoint

custom_entry = {
    "name": "antigravity",
    "base_url": endpoint,
    "api_key": token,
    "api_mode": "chat_completions"
}

providers = cfg.get("custom_providers")
if not isinstance(providers, list):
    providers = []

updated = False
for i, p in enumerate(providers):
    if isinstance(p, dict) and p.get("name") == "antigravity":
        providers[i] = custom_entry
        updated = True
        break
if not updated:
    providers.append(custom_entry)

cfg["custom_providers"] = providers

if yaml:
    with config_file.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
else:
    yaml_text = f"""model:
  default: "{model}"
  provider: "custom:antigravity"
  base_url: "{endpoint}"

custom_providers:
  - name: "antigravity"
    base_url: "{endpoint}"
    api_key: "{token}"
    api_mode: "chat_completions"
"""
    config_file.write_text(yaml_text, encoding="utf-8")

print("[+] Configuration successfully written to:", config_file)
PY_HERMES

echo ""
echo -e "${CYAN}================================================================${NC}"
echo -e "${GREEN}🎉 Remote Connection Configured!                                 ${NC}"
echo -e "${CYAN}================================================================${NC}"
echo -e " Endpoint : ${YELLOW}${ENDPOINT}${NC}"
echo -e " Model    : ${YELLOW}${MODEL}${NC}"
echo ""
echo -e "👉 Type ${GREEN}hermes${NC} in Termux to start chatting!"
echo -e "${CYAN}================================================================${NC}"
