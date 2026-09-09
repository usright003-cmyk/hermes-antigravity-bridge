"""One-click automatic setup and connection between Hermes Agent and Antigravity Bridge."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows terminal
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import yaml
except ImportError:
    try:
        import subprocess
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pyyaml"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        import yaml
    except Exception:
        yaml = None


def find_hermes_config_path() -> Path | None:
    candidates = []
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "hermes" / "config.yaml")
    candidates.append(Path.home() / ".hermes" / "config.yaml")
    candidates.append(Path.home() / "AppData" / "Local" / "hermes" / "config.yaml")
    for p in candidates:
        if p.exists():
            return p
    return candidates[0] if candidates else None


def setup_agy_isolation_home() -> Path:
    agy_home = Path.home() / ".local" / "state" / "hermes-antigravity-bridge" / "agy-home"
    cli_dir = agy_home / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True, exist_ok=True)

    # Set fail-closed strict tool isolation settings
    settings_file = cli_dir / "settings.json"
    settings_file.write_text(json.dumps({
        "artifactReviewPolicy": "request-review",
        "permissions": {"allow": []},
        "toolPermission": "strict",
        "trustedWorkspaces": []
    }, indent=2), encoding="utf-8")

    # Inherit existing CLI authentication & onboarding state from user profile
    user_cli = Path.home() / ".gemini" / "antigravity-cli"
    if user_cli.exists():
        for filename in ("installation_id", "jetski_state.pbtxt"):
            src = user_cli / filename
            if src.exists():
                shutil.copy2(src, cli_dir / filename)

    return agy_home


def setup_bridge() -> tuple[Path, str]:
    config_dir = Path.home() / ".config" / "hermes-antigravity-bridge"
    config_dir.mkdir(parents=True, exist_ok=True)

    token_file = config_dir / "bridge.token"
    if token_file.exists() and token_file.read_text(encoding="utf-8").strip():
        token = token_file.read_text(encoding="utf-8").strip()
    else:
        token = secrets.token_urlsafe(32)
        token_file.write_text(token, encoding="utf-8")

    setup_agy_isolation_home()

    config_file = config_dir / "config.toml"
    toml_content = f"""[server]
host = "127.0.0.1"
port = 8765
token_file = "{token_file.as_posix()}"
allow_remote = false
request_body_limit_bytes = 16777216
max_concurrent_requests = 4

[antigravity]
binary = "agy"
default_model = "gemini-3.8-flash-high"
timeout_seconds = 300
sandbox = true
mode = "plan"
enforce_tool_isolation = true
validated_versions = ["1.1.17", "1.1.28"]
allow_unvalidated_versions = false

[prompt]
max_chars = 4000000
output_token_reserve = 8192
chars_per_token = 4

[logging]
level = "INFO"
include_prompt_content = false
"""
    config_file.write_text(toml_content, encoding="utf-8")
    return config_file, token


def update_hermes_config(hermes_config_path: Path, token: str) -> None:
    hermes_config_path.parent.mkdir(parents=True, exist_ok=True)
    if hermes_config_path.exists():
        backup_path = hermes_config_path.with_suffix(".yaml.bak")
        shutil.copy2(hermes_config_path, backup_path)

    cfg = {}
    if hermes_config_path.exists() and yaml is not None:
        try:
            with hermes_config_path.open("r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}

    if not isinstance(cfg, dict):
        cfg = {}

    if "model" not in cfg or not isinstance(cfg["model"], dict):
        cfg["model"] = {}

    cfg["model"]["default"] = "gemini-3.8-flash"
    cfg["model"]["provider"] = "custom:antigravity"
    cfg["model"]["base_url"] = "http://127.0.0.1:8765/v1"

    custom_entry = {
        "name": "antigravity",
        "base_url": "http://127.0.0.1:8765/v1",
        "api_key": token,
        "api_mode": "chat_completions",
    }

    providers_list = cfg.get("custom_providers")
    if not isinstance(providers_list, list):
        providers_list = []

    existing_names = [
        str(p.get("name"))
        for p in providers_list
        if isinstance(p, dict) and p.get("name") and p.get("name") != "antigravity"
    ]

    updated = False
    for i, p in enumerate(providers_list):
        if isinstance(p, dict) and p.get("name") == "antigravity":
            providers_list[i] = custom_entry
            updated = True
            break
    if not updated:
        providers_list.append(custom_entry)

    cfg["custom_providers"] = providers_list

    if yaml is not None:
        with hermes_config_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)

    return existing_names


def create_launcher_batch(repo_root: Path) -> Path:
    batch_file = repo_root / "run-bridge.bat"
    content = f"""@echo off
title Hermes-Antigravity Bridge (Port 8765)
echo ====================================================================
echo   HERMES - ANTIGRAVITY BRIDGE
echo   1,000,000 Token Native Context ^| Gemini 3.8 Flash (High)
echo   Listening at: http://127.0.0.1:8765
echo ====================================================================
echo.
cd /d "{repo_root}"
set PYTHONPATH=src
python -m hermes_antigravity_bridge.cli --config "%USERPROFILE%\\.config\\hermes-antigravity-bridge\\config.toml" serve
pause
"""
    batch_file.write_text(content, encoding="utf-8")
    return batch_file


def ensure_antigravity_auth(agy_bin: str) -> None:
    user_cli = Path.home() / ".gemini" / "antigravity-cli"
    jetski = user_cli / "jetski_state.pbtxt"
    if not jetski.exists():
        print("[*] Antigravity Google authentication not detected on this machine.")
        print("[*] Launching Google authentication in your browser...")
        try:
            import subprocess
            subprocess.run([agy_bin], check=False)
        except Exception as e:
            print(f"[!] Please run '{agy_bin}' in your terminal to complete Google sign-in: {e}")


def main() -> int:
    repo_root = Path(__file__).resolve().parent
    print("=" * 65)
    print("[*] Hermes-Antigravity Bridge — One-Click Auto Setup...")
    print("=" * 65)

    agy_bin = shutil.which("agy")
    default_agy = Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe"
    if not agy_bin and default_agy.exists():
        agy_bin = str(default_agy)

    if not agy_bin:
        print("[!] Antigravity CLI ('agy') not found on system.")
        print("[*] Please install Antigravity first so 'agy' is available.")
        return 1
    print(f"[+] Antigravity CLI found: {agy_bin}")

    ensure_antigravity_auth(agy_bin)

    config_file, token = setup_bridge()
    print(f"[+] Bridge config created: {config_file}")
    print("[+] Bridge secret token generated automatically")
    print("[+] Isolated Antigravity sandbox environment configured")

    hermes_cfg = find_hermes_config_path()
    if hermes_cfg:
        existing = update_hermes_config(hermes_cfg, token)
        print(f"[+] Hermes Agent configured: {hermes_cfg}")
        print("    Model    : gemini-3.8-flash (1,000,000 Token Context)")
        print("    Provider : custom:antigravity")
        print("    Endpoint : http://127.0.0.1:8765/v1")
        if existing:
            print(f"[+] Preserved your existing providers: {', '.join(existing)}")
            print("    (You can switch between providers anytime in Hermes with /model)")
    else:
        print("[!] Hermes config directory not found.")

    batch_path = create_launcher_batch(repo_root)
    print(f"[+] Launcher created: {batch_path}")

    print("=" * 65)
    print("SUCCESS! Hermes Agent is now fully connected to Antigravity!")
    print("=" * 65)
    print("\nHow to run:")
    print(f"  1. Double click '{batch_path.name}' to start the bridge.")
    print("  2. Open any terminal and run 'hermes'. Enjoy!\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
