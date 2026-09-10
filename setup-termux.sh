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
TOKEN="${HERMES_ANTIGRAVITY_BRIDGE_TOKEN:-}"
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

        # Connect Hermes and Bridge with credentials synchronization
        python3 connect_hermes.py --sync-credentials
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
    if [ ! -s "$HOME/.gemini/antigravity-cli/jetski_state.pbtxt" ]; then
        echo "[*] First time Google Antigravity login required."
        echo "[*] Tap or copy the Google authorization link below to sign in on your phone browser:"
        agy || true
    fi

    # Ensure credentials are synchronized into isolated sandbox profile
    AGY_HOME_CLI="$HOME/.local/state/hermes-antigravity-bridge/agy-home/.gemini/antigravity-cli"
    if [ -s "$HOME/.gemini/antigravity-cli/jetski_state.pbtxt" ]; then
        if [ ! -s "$AGY_HOME_CLI/jetski_state.pbtxt" ] || [ "$HOME/.gemini/antigravity-cli/jetski_state.pbtxt" -nt "$AGY_HOME_CLI/jetski_state.pbtxt" ]; then
            mkdir -p "$AGY_HOME_CLI"
            cp -f "$HOME/.gemini/antigravity-cli/jetski_state.pbtxt" "$AGY_HOME_CLI/" 2>/dev/null || true
            [ -f "$HOME/.gemini/antigravity-cli/installation_id" ] && cp -f "$HOME/.gemini/antigravity-cli/installation_id" "$AGY_HOME_CLI/" 2>/dev/null || true
        fi
    fi

    # Start bridge server daemon in background
    pkill -f "hermes_antigravity_bridge.cli serve" >/dev/null 2>&1 || true
    python3 -m hermes_antigravity_bridge.cli --config "$HOME/.config/hermes-antigravity-bridge/config.toml" serve > /tmp/bridge.log 2>&1 &
    
    TOKEN_FILE="$HOME/.config/hermes-antigravity-bridge/bridge.token"
    BRIDGE_TOKEN=""
    if [ -f "$TOKEN_FILE" ]; then
        BRIDGE_TOKEN="$(cat "$TOKEN_FILE" 2>/dev/null | tr -d "\r\n")"
    fi

    BRIDGE_READY=0
    for i in 1 2 3 4 5 6 7 8 9 10; do
        sleep 1
        if [ -n "$BRIDGE_TOKEN" ]; then
            READY_RESP="$(curl -s -f -H "Authorization: Bearer $BRIDGE_TOKEN" http://127.0.0.1:8765/ready 2>/dev/null || true)"
            if echo "$READY_RESP" | grep -q '"status"[[:space:]]*:[[:space:]]*"ready"'; then
                BRIDGE_READY=1
                break
            fi
        elif curl -s -f http://127.0.0.1:8765/health >/dev/null 2>&1; then
            BRIDGE_READY=1
            break
        fi
    done

    if [ "$BRIDGE_READY" -ne 1 ]; then
        echo -e "\033[0;31m[!] Error: Bridge daemon failed to start or pass readiness checks on http://127.0.0.1:8765\033[0m"
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
fi

if [[ -z "$TOKEN" || "$TOKEN" == "<YOUR_TOKEN>" ]]; then
    read -rp "Enter Bridge Bearer Token: " TOKEN
fi

if [[ -z "$ENDPOINT" || -z "$TOKEN" || "$TOKEN" == "<YOUR_TOKEN>" ]]; then
    echo -e "\033[0;31m[!] Error: Both endpoint and valid token are required for remote mode.\033[0m"
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
import re
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


def _format_scalar(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    special_chars = (
        ":", "#", "[", "]", "{", "}", ",", "&", "*", "?", "|", "-",
        "<", ">", "=", "!", "%", "@", "`", '"', "'", " ",
    )
    if (
        not s
        or s.strip() != s
        or any(ch in s for ch in special_chars)
        or s.lower() in ("true", "false", "null", "yes", "no")
    ):
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def dump_yaml_simple(data, indent=0):
    if indent == 0:
        if isinstance(data, dict) and not data:
            return "{}"
        if isinstance(data, list) and not data:
            return "[]"

    lines = []
    prefix = "  " * indent
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                if not v:
                    lines.append(f"{prefix}{k}: {{}}")
                else:
                    lines.append(f"{prefix}{k}:")
                    sub = dump_yaml_simple(v, indent + 1)
                    if sub:
                        lines.append(sub)
            elif isinstance(v, list):
                if not v:
                    lines.append(f"{prefix}{k}: []")
                else:
                    lines.append(f"{prefix}{k}:")
                    for item in v:
                        if isinstance(item, dict):
                            items = list(item.items())
                            if not items:
                                lines.append(f"{prefix}  - {{}}")
                            else:
                                first_k, first_v = items[0]
                                if isinstance(first_v, dict):
                                    if not first_v:
                                        lines.append(f"{prefix}  - {first_k}: {{}}")
                                    else:
                                        lines.append(f"{prefix}  - {first_k}:")
                                        sub = dump_yaml_simple(first_v, indent + 3)
                                        if sub:
                                            lines.append(sub)
                                elif isinstance(first_v, list):
                                    if not first_v:
                                        lines.append(f"{prefix}  - {first_k}: []")
                                    else:
                                        lines.append(f"{prefix}  - {first_k}:")
                                        sub = dump_yaml_simple(first_v, indent + 3)
                                        if sub:
                                            lines.append(sub)
                                else:
                                    lines.append(f"{prefix}  - {first_k}: {_format_scalar(first_v)}")
                                for rk, rv in items[1:]:
                                    if isinstance(rv, dict):
                                        if not rv:
                                            lines.append(f"{prefix}    {rk}: {{}}")
                                        else:
                                            lines.append(f"{prefix}    {rk}:")
                                            sub = dump_yaml_simple(rv, indent + 3)
                                            if sub:
                                                lines.append(sub)
                                    elif isinstance(rv, list):
                                        if not rv:
                                            lines.append(f"{prefix}    {rk}: []")
                                        else:
                                            lines.append(f"{prefix}    {rk}:")
                                            sub = dump_yaml_simple(rv, indent + 3)
                                            if sub:
                                                lines.append(sub)
                                    else:
                                        lines.append(f"{prefix}    {rk}: {_format_scalar(rv)}")
                        elif isinstance(item, list):
                            if not item:
                                lines.append(f"{prefix}  - []")
                            else:
                                lines.append(f"{prefix}  -")
                                sub = dump_yaml_simple(item, indent + 2)
                                if sub:
                                    lines.append(sub)
                        else:
                            lines.append(f"{prefix}  - {_format_scalar(item)}")
            else:
                lines.append(f"{prefix}{k}: {_format_scalar(v)}")
    return "\n".join(lines)


def _strip_inline_comment(val):
    val = val.strip()
    if not val:
        return ""
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val
    parts = re.split(r"\s+#", val, maxsplit=1)
    return parts[0].strip()


def _parse_scalar_simple(val):
    val = _strip_inline_comment(val)
    if not val:
        return ""
    if val == "{}":
        return {}
    if val == "[]":
        return []
    if val.lower() in ("true", "yes"):
        return True
    if val.lower() in ("false", "no"):
        return False
    if val.lower() in ("null", "none", "~"):
        return None
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    try:
        if "." in val:
            return float(val)
        return int(val)
    except ValueError:
        return val


def _split_mapping_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith(('"', "'")):
        quote = line[0]
        end_quote = -1
        escaped = False
        for i in range(1, len(line)):
            if escaped:
                escaped = False
                continue
            if line[i] == "\\":
                escaped = True
                continue
            if line[i] == quote:
                end_quote = i
                break
        if end_quote != -1:
            rest = line[end_quote + 1:].lstrip()
            if rest.startswith(":"):
                key = line[1:end_quote].replace('\\"', '"').replace("\\\\", "\\")
                val_part = rest[1:].strip()
                val_clean = _strip_inline_comment(val_part) if val_part else None
                return key, val_clean if val_clean else None
        return None

    colon_idx = -1
    for i, c in enumerate(line):
        if c == ":":
            if i == len(line) - 1 or line[i + 1] in (" ", "\t"):
                colon_idx = i
                break
    if colon_idx == -1:
        return None

    key = line[:colon_idx].strip()
    val_part = line[colon_idx + 1:].strip()
    val_clean = _strip_inline_comment(val_part) if val_part else None
    return key, val_clean if val_clean else None


def parse_yaml_simple(text):
    lines = text.splitlines()
    idx = 0
    total = len(lines)

    def parse_block(current_indent, expect_list=False):
        nonlocal idx
        is_list = expect_list
        temp_idx = idx
        while not is_list and temp_idx < total:
            raw = lines[temp_idx]
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                temp_idx += 1
                continue
            line_indent = len(raw) - len(raw.lstrip(" "))
            if line_indent < current_indent:
                break
            if stripped.startswith("- "):
                is_list = True
            break

        if is_list:
            result_list = []
            while idx < total:
                raw = lines[idx]
                stripped = raw.strip()
                if not stripped or stripped.startswith("#"):
                    idx += 1
                    continue
                line_indent = len(raw) - len(raw.lstrip(" "))
                if line_indent < current_indent:
                    break
                if not stripped.startswith("- "):
                    break

                content = stripped[2:].strip()
                idx += 1
                mapping_split = _split_mapping_line(content)
                if not content:
                    item_val = parse_block(line_indent + 2)
                    result_list.append(item_val)
                elif mapping_split is not None:
                    k, v = mapping_split
                    item_dict = {}
                    if v is not None:
                        item_dict[k] = _parse_scalar_simple(v)
                    else:
                        item_dict[k] = parse_block(line_indent + 4)

                    while idx < total:
                        n_raw = lines[idx]
                        n_stripped = n_raw.strip()
                        if not n_stripped or n_stripped.startswith("#"):
                            idx += 1
                            continue
                        n_indent = len(n_raw) - len(n_raw.lstrip(" "))
                        if n_indent <= line_indent:
                            break
                        if n_stripped.startswith("- "):
                            break
                        n_split = _split_mapping_line(n_stripped)
                        if n_split is not None:
                            sub_k, sub_v = n_split
                            idx += 1
                            if sub_v is not None:
                                item_dict[sub_k] = _parse_scalar_simple(sub_v)
                            else:
                                item_dict[sub_k] = parse_block(n_indent + 2)
                        else:
                            idx += 1
                    result_list.append(item_dict)
                else:
                    result_list.append(_parse_scalar_simple(content))
            return result_list

        result_dict = {}
        while idx < total:
            raw = lines[idx]
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                idx += 1
                continue
            line_indent = len(raw) - len(raw.lstrip(" "))
            if line_indent < current_indent:
                break
            split_res = _split_mapping_line(stripped)
            if split_res is None:
                idx += 1
                continue
            k, v = split_res
            idx += 1
            if v is not None:
                result_dict[k] = _parse_scalar_simple(v)
            else:
                child_indent = line_indent + 2
                peek_is_list = False
                lookahead = idx
                while lookahead < total:
                    peek = lines[lookahead]
                    p_strip = peek.strip()
                    if p_strip and not p_strip.startswith("#"):
                        child_indent = len(peek) - len(peek.lstrip(" "))
                        peek_is_list = p_strip.startswith("- ")
                        break
                    lookahead += 1
                if child_indent > line_indent or (child_indent == line_indent and peek_is_list):
                    result_dict[k] = parse_block(child_indent, expect_list=peek_is_list)
                else:
                    result_dict[k] = None
        return result_dict

    parsed = parse_block(0)
    return parsed if isinstance(parsed, dict) else {}


cfg = {}
if config_file.exists():
    if yaml:
        try:
            with config_file.open("r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, dict):
                    cfg = loaded
        except Exception:
            cfg = {}
    else:
        try:
            raw_text = config_file.read_text(encoding="utf-8")
            cfg = parse_yaml_simple(raw_text)
        except Exception:
            cfg = {}

if not isinstance(cfg, dict):
    cfg = {}

# Non-destructive preservation: Migrate existing model provider into fallback_providers
fallback_list = cfg.get("fallback_providers")
if not isinstance(fallback_list, list):
    fallback_list = []

old_model = cfg.get("model")
if isinstance(old_model, dict):
    old_provider = old_model.get("provider")
    old_model_name = old_model.get("default") or old_model.get("model")
    if old_provider and old_provider not in ("custom:antigravity", "antigravity"):
        fallback_entry = dict(old_model)
        fallback_entry["provider"] = old_provider
        if old_model_name:
            fallback_entry["model"] = old_model_name
        fallback_entry.pop("default", None)

        if not any(
            isinstance(fb, dict)
            and fb.get("provider") == old_provider
            and fb.get("model") == old_model_name
            for fb in fallback_list
        ):
            fallback_list.append(fallback_entry)
        cfg["fallback_providers"] = fallback_list
elif isinstance(old_model, str) and old_model.strip():
    old_model_name = old_model.strip()
    old_provider = cfg.pop("provider", None) or "openai"
    if old_provider not in ("custom:antigravity", "antigravity"):
        fallback_entry = {
            "provider": old_provider,
            "model": old_model_name,
        }
        if "base_url" in cfg and "custom:antigravity" not in str(cfg.get("base_url", "")):
            fallback_entry["base_url"] = cfg.pop("base_url")
        if "api_key" in cfg:
            fallback_entry["api_key"] = cfg.pop("api_key")

        if not any(
            isinstance(fb, dict)
            and fb.get("provider") == old_provider
            and fb.get("model") == old_model_name
            for fb in fallback_list
        ):
            fallback_list.append(fallback_entry)
        cfg["fallback_providers"] = fallback_list

if "model" not in cfg or not isinstance(cfg["model"], dict):
    cfg["model"] = {}

cfg["model"]["default"] = model
cfg["model"]["provider"] = "custom:antigravity"
cfg["model"]["base_url"] = endpoint

custom_entry = {
    "name": "antigravity",
    "base_url": endpoint,
    "api_key": token,
    "api_mode": "chat_completions",
}

raw_custom = cfg.get("custom_providers")
providers = []
if isinstance(raw_custom, list):
    for item in raw_custom:
        if isinstance(item, dict):
            providers.append(dict(item))
elif isinstance(raw_custom, dict):
    for name, details in raw_custom.items():
        if isinstance(details, dict):
            entry = dict(details)
            entry.setdefault("name", str(name))
            providers.append(entry)
        elif isinstance(details, str):
            providers.append({"name": str(name), "base_url": details})

updated = False
for i, p in enumerate(providers):
    if p.get("name") == "antigravity":
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
    config_file.write_text(dump_yaml_simple(cfg) + "\n", encoding="utf-8")

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
