"""One-click automatic setup and connection between Hermes Agent and Antigravity Bridge."""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Ensure UTF-8 output on Windows terminal
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

try:
    import yaml
except ImportError:
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pyyaml"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        import yaml
    except (subprocess.SubprocessError, ImportError, OSError):
        yaml = None


def _dump_scalar_fallback(val: Any) -> str:
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    s = str(val)
    special_chars = (
        ":", "#", "[", "]", "{", "}", ",", "&", "*", "?", "|", "-",
        "<", ">", "=", "!", "%", "@", "`", '"', "'", " ",
    )
    if (
        not s
        or s.strip() != s
        or any(c in s for c in special_chars)
        or s.lower() in ("true", "false", "null", "yes", "no")
    ):
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def dump_yaml_fallback(data: Any, indent: int = 0) -> str:
    """Pure-Python YAML serializer fallback for simple dicts, lists, and scalars."""
    if indent == 0:
        if isinstance(data, dict) and not data:
            return "{}"
        if isinstance(data, list) and not data:
            return "[]"

    lines: list[str] = []
    prefix = "  " * indent
    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, dict):
                if not val:
                    lines.append(f"{prefix}{key}: {{}}")
                else:
                    lines.append(f"{prefix}{key}:")
                    sub = dump_yaml_fallback(val, indent + 1)
                    if sub:
                        lines.append(sub)
            elif isinstance(val, list):
                if not val:
                    lines.append(f"{prefix}{key}: []")
                else:
                    lines.append(f"{prefix}{key}:")
                    for item in val:
                        if isinstance(item, dict):
                            dict_items = list(item.items())
                            if not dict_items:
                                lines.append(f"{prefix}  - {{}}")
                            else:
                                first_k, first_v = dict_items[0]
                                if isinstance(first_v, dict):
                                    if not first_v:
                                        lines.append(f"{prefix}  - {first_k}: {{}}")
                                    else:
                                        lines.append(f"{prefix}  - {first_k}:")
                                        sub = dump_yaml_fallback(first_v, indent + 3)
                                        if sub:
                                            lines.append(sub)
                                elif isinstance(first_v, list):
                                    if not first_v:
                                        lines.append(f"{prefix}  - {first_k}: []")
                                    else:
                                        lines.append(f"{prefix}  - {first_k}:")
                                        sub = dump_yaml_fallback(first_v, indent + 3)
                                        if sub:
                                            lines.append(sub)
                                else:
                                    lines.append(
                                        f"{prefix}  - {first_k}: {_dump_scalar_fallback(first_v)}"
                                    )
                                for rem_k, rem_v in dict_items[1:]:
                                    if isinstance(rem_v, dict):
                                        if not rem_v:
                                            lines.append(f"{prefix}    {rem_k}: {{}}")
                                        else:
                                            lines.append(f"{prefix}    {rem_k}:")
                                            sub = dump_yaml_fallback(rem_v, indent + 3)
                                            if sub:
                                                lines.append(sub)
                                    elif isinstance(rem_v, list):
                                        if not rem_v:
                                            lines.append(f"{prefix}    {rem_k}: []")
                                        else:
                                            lines.append(f"{prefix}    {rem_k}:")
                                            sub = dump_yaml_fallback(rem_v, indent + 3)
                                            if sub:
                                                lines.append(sub)
                                    else:
                                        lines.append(
                                            f"{prefix}    {rem_k}: {_dump_scalar_fallback(rem_v)}"
                                        )
                        elif isinstance(item, list):
                            if not item:
                                lines.append(f"{prefix}  - []")
                            else:
                                lines.append(f"{prefix}  -")
                                sub = dump_yaml_fallback(item, indent + 2)
                                if sub:
                                    lines.append(sub)
                        else:
                            lines.append(f"{prefix}  - {_dump_scalar_fallback(item)}")
            else:
                lines.append(f"{prefix}{key}: {_dump_scalar_fallback(val)}")
    return "\n".join(lines)


def _strip_inline_comment(val: str) -> str:
    val = val.strip()
    if not val:
        return ""
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val
    # Unquoted scalar: strip inline comment starting with whitespace + '#'
    import re
    parts = re.split(r"\s+#", val, maxsplit=1)
    return parts[0].strip()


def _parse_scalar_fallback(val: str) -> Any:
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
    if (val.startswith('"') and val.endswith('"')) or (
        val.startswith("'") and val.endswith("'")
    ):
        return val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    try:
        if "." in val:
            return float(val)
        return int(val)
    except ValueError:
        return val


def _split_mapping_line(line: str) -> tuple[str, str | None] | None:
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
        if c == ":" and (i == len(line) - 1 or line[i + 1] in (" ", "\t")):
            colon_idx = i
            break
    if colon_idx == -1:
        return None

    key = line[:colon_idx].strip()
    val_part = line[colon_idx + 1:].strip()
    val_clean = _strip_inline_comment(val_part) if val_part else None
    return key, val_clean if val_clean else None


def parse_yaml_fallback(text: str) -> dict[str, Any]:
    """Lightweight fallback YAML parser for simple dictionary, list and scalar structures."""
    lines = text.splitlines()
    idx = 0
    total = len(lines)

    def parse_block(current_indent: int, expect_list: bool = False) -> Any:
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
            result_list: list[Any] = []
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
                    item_dict: dict[str, Any] = {}
                    if v is not None:
                        item_dict[k] = _parse_scalar_fallback(v)
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
                                item_dict[sub_k] = _parse_scalar_fallback(sub_v)
                            else:
                                item_dict[sub_k] = parse_block(n_indent + 2)
                        else:
                            idx += 1
                    result_list.append(item_dict)
                else:
                    result_list.append(_parse_scalar_fallback(content))
            return result_list

        result_dict: dict[str, Any] = {}
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
                result_dict[k] = _parse_scalar_fallback(v)
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


def get_desktop_dir() -> Path:
    """Return the active desktop path, respecting Windows OneDrive redirection."""
    if sys.platform == "win32":
        onedrive = os.environ.get("OneDrive")
        if onedrive and (Path(onedrive) / "Desktop").is_dir():
            return Path(onedrive) / "Desktop"
        user_profile = os.environ.get("USERPROFILE")
        if user_profile and (Path(user_profile) / "OneDrive" / "Desktop").is_dir():
            return Path(user_profile) / "OneDrive" / "Desktop"
    return Path.home() / "Desktop"


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


def setup_agy_isolation_home(sync_credentials: bool = True) -> Path:
    agy_home = Path.home() / ".local" / "state" / "hermes-antigravity-bridge" / "agy-home"
    cli_dir = agy_home / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True, exist_ok=True)

    # Set fail-closed strict tool isolation settings
    settings_file = cli_dir / "settings.json"
    settings_file.write_text(json.dumps({
        "artifactReviewPolicy": "asks-for-review",
        "permissions": {"allow": []},
        "toolPermission": "strict",
        "trustedWorkspaces": []
    }, indent=2), encoding="utf-8")

    # Inherit existing CLI authentication if sync_credentials is True
    if sync_credentials:
        user_cli = Path.home() / ".gemini" / "antigravity-cli"
        if user_cli.exists() and user_cli.resolve() != cli_dir.resolve():
            for filename in ("installation_id", "jetski_state.pbtxt"):
                src = user_cli / filename
                if src.is_file() and src.stat().st_size > 0:
                    try:
                        shutil.copy2(src, cli_dir / filename)
                        if os.name == "posix":
                            try:
                                (cli_dir / filename).chmod(0o600)
                            except OSError:
                                pass
                    except OSError:
                        pass

    return agy_home


def setup_bridge(sync_credentials: bool = True) -> tuple[Path, str]:
    config_dir = Path.home() / ".config" / "hermes-antigravity-bridge"
    config_dir.mkdir(parents=True, exist_ok=True)

    token_file = config_dir / "bridge.token"
    if token_file.exists() and token_file.read_text(encoding="utf-8").strip():
        token = token_file.read_text(encoding="utf-8").strip()
    else:
        token = secrets.token_urlsafe(32)
        token_file.write_text(token, encoding="utf-8")

    setup_agy_isolation_home(sync_credentials=sync_credentials)

    config_file = config_dir / "config.toml"
    sync_str = "true" if sync_credentials else "false"
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
validated_versions = ["1.1.17", "1.1.28", "1.2.0", "1.2.1", "1.2.2"]
allow_unvalidated_versions = false
sync_user_credentials = {sync_str}
tool_call_mode = "compatible"

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


def update_hermes_config(hermes_config_path: Path, token: str) -> list[str]:
    hermes_config_path.parent.mkdir(parents=True, exist_ok=True)
    if hermes_config_path.exists():
        backup_path = hermes_config_path.with_suffix(".yaml.bak")
        try:
            shutil.copy2(hermes_config_path, backup_path)
        except OSError:
            pass

    # Securely store token in .env with 0600 permissions
    env_file = hermes_config_path.parent / ".env"
    try:
        env_lines = []
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if not line.startswith("HERMES_ANTIGRAVITY_BRIDGE_TOKEN="):
                    env_lines.append(line)
        env_lines.append(f"HERMES_ANTIGRAVITY_BRIDGE_TOKEN={token}")
        env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        if os.name == "posix":
            try:
                os.chmod(env_file, 0o600)
            except OSError:
                pass
    except OSError as exc:
        print(f"Notice: Could not write token to .env: {exc}")

    cfg: dict[str, Any] = {}
    if hermes_config_path.exists():
        try:
            raw_text = hermes_config_path.read_text(encoding="utf-8")
            if yaml is not None:
                loaded = yaml.safe_load(raw_text)
                if isinstance(loaded, dict):
                    cfg = loaded
            else:
                cfg = parse_yaml_fallback(raw_text)
        except Exception:  # noqa: BLE001
            cfg = {}

    if not isinstance(cfg, dict):
        cfg = {}

    preserved_descriptions: list[str] = []

    # Non-destructive preservation: Migrate existing model provider into fallback_providers
    fallback_list = cfg.get("fallback_providers")
    if not isinstance(fallback_list, list):
        fallback_list = []

    old_model = cfg.get("model")
    if isinstance(old_model, dict):
        old_provider = old_model.get("provider")
        old_model_name = old_model.get("default") or old_model.get("model")
        if old_provider and old_provider not in ("custom:antigravity", "antigravity"):
            fallback_entry: dict[str, Any] = dict(old_model)
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
            desc = f"{old_provider} ({old_model_name})" if old_model_name else str(old_provider)
            preserved_descriptions.append(f"fallback: {desc}")
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
            desc = f"{old_provider} ({old_model_name})"
            preserved_descriptions.append(f"fallback: {desc}")

    # Preserve and register custom providers
    custom_entry = {
        "name": "antigravity",
        "base_url": "http://127.0.0.1:8765/v1",
        "key_env": "HERMES_ANTIGRAVITY_BRIDGE_TOKEN",
        "api_mode": "chat_completions",
    }

    raw_custom = cfg.get("custom_providers")
    providers_list: list[dict[str, Any]] = []
    if isinstance(raw_custom, list):
        for item in raw_custom:
            if isinstance(item, dict):
                providers_list.append(dict(item))
    elif isinstance(raw_custom, dict):
        for name, details in raw_custom.items():
            if isinstance(details, dict):
                entry = dict(details)
                entry.setdefault("name", str(name))
                providers_list.append(entry)
            elif isinstance(details, str):
                providers_list.append({"name": str(name), "base_url": details})

    for p in providers_list:
        if p.get("name") and p.get("name") != "antigravity":
            preserved_descriptions.append(f"custom:{p.get('name')}")

    updated = False
    for i, p in enumerate(providers_list):
        if p.get("name") == "antigravity":
            providers_list[i] = custom_entry
            updated = True
            break
    if not updated:
        providers_list.append(custom_entry)

    cfg["custom_providers"] = providers_list

    # Update active model
    if "model" not in cfg or not isinstance(cfg["model"], dict):
        cfg["model"] = {}

    cfg["model"]["default"] = "gemini-3.8-flash"
    cfg["model"]["provider"] = "custom:antigravity"
    cfg["model"]["base_url"] = "http://127.0.0.1:8765/v1"

    # Write back to config file
    if yaml is not None:
        with hermes_config_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
    else:
        text = dump_yaml_fallback(cfg)
        hermes_config_path.write_text(text + "\n", encoding="utf-8")

    if os.name == "posix":
        try:
            hermes_config_path.chmod(0o600)
        except OSError:
            pass

    return preserved_descriptions


def create_launcher_batch(repo_root: Path) -> Path:
    batch_file = repo_root / "run-bridge.bat"
    python_exe = sys.executable
    content = f"""@echo off
title Hermes-Antigravity Bridge (Port 8765)
echo ====================================================================
echo   HERMES - ANTIGRAVITY BRIDGE
echo   1,000,000 Token Native Context ^| Gemini 3.8 Flash (High)
echo   Listening at: http://127.0.0.1:8765
echo ====================================================================
echo.
cd /d "{repo_root}"
set PYTHONPATH={repo_root}\\src;%PYTHONPATH%
"{python_exe}" -m hermes_antigravity_bridge.cli --config "%USERPROFILE%\\.config\\hermes-antigravity-bridge\\config.toml" serve
pause
"""
    batch_file.write_text(content, encoding="utf-8")
    return batch_file


def create_lan_launcher_batch(repo_root: Path) -> Path:
    batch_file = repo_root / "run-bridge-lan.bat"
    python_exe = sys.executable
    content = f"""@echo off
title Hermes-Antigravity Bridge (LAN / Mobile Mode - Port 8765)
echo ====================================================================
echo   HERMES - ANTIGRAVITY BRIDGE (LAN & MOBILE TERMUX MODE)
echo   1,000,000 Token Native Context ^| Multi-Device Network Mode
echo   Listening at: http://0.0.0.0:8765
echo ====================================================================
echo.
cd /d "{repo_root}"
set PYTHONPATH={repo_root}\\src;%PYTHONPATH%
set AGY_BRIDGE_HOST=0.0.0.0
set AGY_BRIDGE_ALLOW_REMOTE=true
"{python_exe}" -m hermes_antigravity_bridge.cli --config "%USERPROFILE%\\.config\\hermes-antigravity-bridge\\config.toml" serve
pause
"""
    batch_file.write_text(content, encoding="utf-8")
    return batch_file


def create_background_launcher_vbs(repo_root: Path) -> Path:
    vbs_file = repo_root / "run-bridge-background.vbs"
    python_exe = sys.executable
    content = f"""' Silent background launcher for Hermes-Antigravity Bridge
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "{repo_root}"
Set WshProcessEnv = WshShell.Environment("Process")
WshProcessEnv("PYTHONPATH") = "{repo_root}\\src"
UserProfile = WshShell.ExpandEnvironmentStrings("%USERPROFILE%")
ConfigFile = UserProfile & "\\.config\\hermes-antigravity-bridge\\config.toml"
PythonCmd = Chr(34) & "{python_exe}" & Chr(34) & " -m hermes_antigravity_bridge.cli --config " & Chr(34) & ConfigFile & Chr(34) & " serve"
WshShell.Run PythonCmd, 0, False
"""
    vbs_file.write_text(content, encoding="utf-8")
    return vbs_file


def create_desktop_launchers(repo_root: Path) -> list[Path]:
    desktop_dir = get_desktop_dir()
    if not desktop_dir.exists():
        return []

    created: list[Path] = []
    bat = create_launcher_batch(repo_root)
    lan_bat = create_lan_launcher_batch(repo_root)
    bg_vbs = create_background_launcher_vbs(repo_root)

    for src, name in [
        (bat, "Run-Antigravity-Bridge.bat"),
        (lan_bat, "Run-Antigravity-Bridge-LAN.bat"),
        (bg_vbs, "Run-Antigravity-Bridge-Background.vbs"),
    ]:
        dest = desktop_dir / name
        try:
            shutil.copy2(src, dest)
            created.append(dest)
        except OSError:
            pass
    return created


def get_lan_ip() -> str:
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def is_antigravity_authenticated() -> bool:
    user_cli = Path.home() / ".gemini" / "antigravity-cli"
    jetski = user_cli / "jetski_state.pbtxt"
    try:
        return jetski.is_file() and jetski.stat().st_size > 0
    except OSError:
        return False


def ensure_antigravity_auth(agy_bin: str, poll_timeout_seconds: int = 30) -> bool:
    if is_antigravity_authenticated():
        print("[+] Antigravity Google authentication confirmed (~/.gemini/antigravity-cli/jetski_state.pbtxt)")
        return True

    print("[*] Antigravity Google authentication not detected on this machine.")
    print("[*] Launching Google authentication flow...")
    print("[*] If your browser does not open automatically, copy the URL displayed")
    print("    below into your browser, sign in with Google, and paste the authorization code here.")
    resolved_bin = shutil.which(agy_bin) or agy_bin
    try:
        subprocess.run([resolved_bin], check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[!] Error launching '{resolved_bin}': {exc}")
        print(f"[*] Please run '{resolved_bin}' manually in your terminal to complete sign-in.")

    if is_antigravity_authenticated():
        print("[+] Antigravity Google authentication confirmed!")
        return True

    print("[*] Polling for authentication completion...")
    start = time.time()
    while time.time() - start < poll_timeout_seconds:
        time.sleep(1)
        if is_antigravity_authenticated():
            print("[+] Antigravity Google authentication confirmed!")
            return True

    print("[!] Notice: jetski_state.pbtxt not detected yet. Proceeding with setup...")
    return False


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Hermes Agent Antigravity Connector")
    parser.add_argument(
        "--no-sync-credentials",
        dest="sync_credentials",
        action="store_false",
        help="Do not synchronize Google authentication tokens into the isolated profile",
    )
    parser.add_argument(
        "--sync-credentials",
        dest="sync_credentials",
        action="store_true",
        default=True,
        help="Synchronize Google authentication tokens into the isolated profile (default: True)",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent
    print("=" * 65)
    print("[*] Hermes-Antigravity Bridge — One-Click Auto Setup...")
    print("=" * 65)

    agy_bin = shutil.which("agy")
    default_agy = Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe"
    default_agy_cmd = Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.cmd"
    if not agy_bin:
        if default_agy.exists():
            agy_bin = str(default_agy)
        elif default_agy_cmd.exists():
            agy_bin = str(default_agy_cmd)

    if not agy_bin:
        print("[!] Antigravity CLI ('agy') not found on system.")
        print("[*] Please install Antigravity first so 'agy' is available.")
        return 1
    print(f"[+] Antigravity CLI found: {agy_bin}")

    ensure_antigravity_auth(agy_bin)

    config_file, token = setup_bridge(sync_credentials=args.sync_credentials)
    print(f"[+] Bridge config created: {config_file}")
    if args.sync_credentials:
        print("[+] User Google credentials synchronized into isolated profile (--sync-credentials enabled)")
    else:
        print("[+] Preserved strict profile isolation (credentials NOT synchronized)")
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
    lan_batch_path = create_lan_launcher_batch(repo_root)
    bg_vbs_path = create_background_launcher_vbs(repo_root)
    desktop_launchers = create_desktop_launchers(repo_root)
    lan_ip = get_lan_ip()

    print(f"[+] PC Launcher created: {batch_path}")
    print(f"[+] LAN/Mobile Launcher created: {lan_batch_path}")
    print(f"[+] Silent/Background Launcher created: {bg_vbs_path}")
    if desktop_launchers:
        print("[+] Desktop Shortcuts created:")
        for dl in desktop_launchers:
            print(f"    - {dl}")

    print("=" * 65)
    print("SUCCESS! Hermes Agent is now fully connected to Antigravity!")
    print("=" * 65)
    print("\nHow to run:")
    print(f"  1. Double click '{batch_path.name}' to start the bridge on this PC (console).")
    print(f"     OR double click '{bg_vbs_path.name}' to run silently in the background.")
    print("  2. Open any terminal and run 'hermes'. Enjoy!")
    if lan_ip != "127.0.0.1":
        print("\n" + "-" * 65)
        print("📱 Android / Termux Quick Connect:")
        print(f"  1. Double click '{lan_batch_path.name}' to allow phone connections.")
        print("  2. In Termux on your Android phone (same Wi-Fi), run:")
        print(f"     curl -sSL https://raw.githubusercontent.com/usright003-cmyk/hermes-antigravity-bridge/main/setup-termux.sh | bash -s -- --endpoint http://{lan_ip}:8765/v1 --token {token}")
        print("-" * 65 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

