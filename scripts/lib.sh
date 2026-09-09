#!/usr/bin/env bash
# shellcheck disable=SC2034  # Shared variables are consumed by sourced lifecycle scripts.
set -euo pipefail

HAB_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HAB_PROJECT_ROOT="$(cd "$HAB_SCRIPT_DIR/.." && pwd)"
HAB_APP_NAME="hermes-antigravity-bridge"
HAB_INSTALL_ROOT="${HAB_INSTALL_ROOT:-$HOME/.local/share/$HAB_APP_NAME}"
HAB_CONFIG_ROOT="${HAB_CONFIG_ROOT:-$HOME/.config/$HAB_APP_NAME}"
HAB_SYSTEMD_USER_DIR="${HAB_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
HAB_UNIT_DEST="$HAB_SYSTEMD_USER_DIR/$HAB_APP_NAME.service"
HAB_CONFIG_FILE="$HAB_CONFIG_ROOT/config.toml"
HAB_TOKEN_FILE="$HAB_CONFIG_ROOT/bridge.token"
HAB_CURRENT_LINK="$HAB_INSTALL_ROOT/current"
HAB_PREVIOUS_LINK="$HAB_INSTALL_ROOT/previous"
HAB_RELEASES_DIR="$HAB_INSTALL_ROOT/releases"
HAB_RUNTIME_DIR="$HAB_INSTALL_ROOT/runtime"
HAB_AGY_HOME="${HAB_AGY_HOME:-$HOME/.local/state/$HAB_APP_NAME/agy-home}"
HAB_AGY_SETTINGS="$HAB_AGY_HOME/.gemini/antigravity-cli/settings.json"
HAB_PYTHON_BIN="${HAB_PYTHON_BIN:-$(command -v python3 || true)}"
hab_detect_uv() {
    if [[ -n "${HAB_UV_BIN:-}" ]]; then
        printf '%s\n' "$HAB_UV_BIN"
        return 0
    fi
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    local candidate
    for candidate in "$HOME/.cargo/bin/uv" "$HOME/.local/bin/uv" "$HOME/.hermes/bin/uv" "/home/azureuser/.hermes/bin/uv"; do
        if [[ -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}
HAB_UV_BIN="$(hab_detect_uv || true)"
HAB_SKIP_SERVICE="${HAB_SKIP_SERVICE:-0}"
HAB_SKIP_AGY_CHECK="${HAB_SKIP_AGY_CHECK:-0}"
HAB_PIP_NO_DEPS="${HAB_PIP_NO_DEPS:-0}"

hab_die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

hab_safe_bridge_path() {
    local path="$1"
    [[ -n "$path" && "$path" != "/" && "$path" != "$HOME" ]] || return 1
    [[ "$(basename "$path")" == "$HAB_APP_NAME" ]] || return 1
}

hab_atomic_link() {
    local target="$1" link="$2" temporary="${2}.tmp.$$"
    rm -f "$temporary"
    ln -s "$target" "$temporary"
    mv -Tf "$temporary" "$link"
}

hab_current_target() {
    if [[ -L "$HAB_CURRENT_LINK" ]]; then
        readlink -f "$HAB_CURRENT_LINK"
    fi
}

hab_previous_target() {
    if [[ -L "$HAB_PREVIOUS_LINK" ]]; then
        readlink -f "$HAB_PREVIOUS_LINK"
    fi
}

hab_systemctl() {
    systemctl --user "$@"
}

hab_service_health() {
    local token host port
    token="$(tr -d '\r\n' < "$HAB_TOKEN_FILE")"
    read -r host port < <(
        "$HAB_CURRENT_LINK/venv/bin/python" - "$HAB_CONFIG_FILE" <<'PY'
from hermes_antigravity_bridge.config import BridgeConfig
import sys
config = BridgeConfig.load(sys.argv[1])
print(config.server.host, config.server.port)
PY
    )
    curl -fsS --max-time 10 "http://$host:$port/health" >/dev/null
    curl -fsS --max-time 35 \
        -H "Authorization: Bearer $token" \
        "http://$host:$port/ready" >/dev/null
}

hab_render_unit() {
    local executable="$1"
    mkdir -p "$HAB_SYSTEMD_USER_DIR" "$HAB_RUNTIME_DIR"
    "$HAB_PYTHON_BIN" - "$HAB_PROJECT_ROOT/deploy/systemd/$HAB_APP_NAME.service" "$HAB_UNIT_DEST" \
        "$executable" "$HAB_CONFIG_FILE" "$HAB_RUNTIME_DIR" "$HAB_AGY_HOME" "$HOME" <<'PY'
from pathlib import Path
import sys
source, destination, executable, config, runtime, agy_home, home = sys.argv[1:]
text = Path(source).read_text(encoding="utf-8")
for key, value in {
    "@EXECUTABLE@": executable,
    "@CONFIG_FILE@": config,
    "@RUNTIME_DIR@": runtime,
    "@AGY_HOME@": agy_home,
    "@HOME@": home,
}.items():
    if any(ch in value for ch in "\n\r"):
        raise SystemExit("unsafe newline in rendered systemd path")
    text = text.replace(key, value)
Path(destination).write_text(text, encoding="utf-8")
PY
    chmod 600 "$HAB_UNIT_DEST"
}
