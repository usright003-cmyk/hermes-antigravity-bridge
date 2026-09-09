#!/usr/bin/env bash
# ==============================================================================
# Hermes Agent - Android / Termux Connector for Antigravity Bridge
# ==============================================================================
set -euo pipefail

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
PURPLE='\033[0;35m'
NC='\033[0m' # No Color

echo -e "${CYAN}================================================================${NC}"
echo -e "${GREEN}   📱 Hermes Agent: Android / Termux Antigravity Connector      ${NC}"
echo -e "${CYAN}================================================================${NC}"

# 1. Ensure basic tools in Termux / Linux
echo -e "${YELLOW}[*] Checking Termux prerequisites...${NC}"
if command -v pkg >/dev/null 2>&1; then
    pkg install -y python git curl || true
elif command -v apt-get >/dev/null 2>&1; then
    apt-get update -y && apt-get install -y python3 python3-pip git curl || true
fi

# Ensure pip & pyyaml
python3 -m pip install --upgrade pip pyyaml requests >/dev/null 2>&1 || true

# Parse command line flags
ENDPOINT=""
TOKEN=""
MODEL="gemini-3.8-flash"

while [[ $# -gt 0 ]]; do
    case "$1" in
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

# If arguments were not provided, ask interactively
if [[ -z "$ENDPOINT" ]]; then
    echo ""
    echo -e "${PURPLE}Choose Setup Mode:${NC}"
    echo "1) Connect to PC / Server Bridge over Wi-Fi or Tailscale (Recommended)"
    echo "2) Run Standalone Bridge locally on Android"
    read -rp "Enter choice [1/2, default: 1]: " MODE_CHOICE
    MODE_CHOICE="${MODE_CHOICE:-1}"

    if [[ "$MODE_CHOICE" == "2" ]]; then
        echo -e "${YELLOW}[*] Setting up local bridge on Termux...${NC}"
        git clone https://github.com/usright003-cmyk/hermes-antigravity-bridge.git "$HOME/hermes-antigravity-bridge" 2>/dev/null || (cd "$HOME/hermes-antigravity-bridge" && git pull)
        cd "$HOME/hermes-antigravity-bridge"
        python3 -m pip install -e .
        echo -e "${GREEN}[+] Local bridge installed on Termux!${NC}"
        echo -e "    Run: hermes-antigravity-bridge serve"
        exit 0
    fi

    echo ""
    echo -e "${YELLOW}[*] Remote PC Bridge Configuration:${NC}"
    read -rp "Enter PC / Bridge IP & Port [e.g. http://192.168.0.5:8765/v1]: " ENDPOINT
    read -rp "Enter Bridge Bearer Token: " TOKEN
fi

if [[ -z "$ENDPOINT" || -z "$TOKEN" ]]; then
    echo -e "\033[0;31m[!] Error: Both endpoint and token are required.\033[0m"
    exit 1
fi

# Clean endpoint url (ensure it ends with /v1)
ENDPOINT="${ENDPOINT%/}"
if [[ "$ENDPOINT" != */v1 ]]; then
    ENDPOINT="${ENDPOINT}/v1"
fi

# Configure Hermes config.yaml
echo -e "${YELLOW}[*] Configuring Hermes Agent profile...${NC}"
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
    # Fallback to pure yaml generation if pyyaml is missing
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

# Check and advise on termux-api
if command -v termux-battery-status >/dev/null 2>&1; then
    echo -e "${GREEN}[+] Termux:API detected! Hermes can control Android hardware tools.${NC}"
else
    echo -e "${PURPLE}[💡 TIP] Run 'pkg install termux-api' to let Hermes check battery, send SMS & alerts on your phone!${NC}"
fi

echo ""
echo -e "${CYAN}================================================================${NC}"
echo -e "${GREEN}🎉 CONGRATULATIONS! Your Android Termux is Connected!          ${NC}"
echo -e "${CYAN}================================================================${NC}"
echo -e " Endpoint : ${YELLOW}${ENDPOINT}${NC}"
echo -e " Model    : ${YELLOW}${MODEL} (1,000,000 Token Native Context)${NC}"
echo ""
echo -e "👉 Simply type ${GREEN}hermes${NC} in Termux to start chatting!"
echo -e "${CYAN}================================================================${NC}"
