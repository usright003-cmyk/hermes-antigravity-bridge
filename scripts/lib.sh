#!/usr/bin/env bash
# shellcheck disable=SC2034  # Shared variables are consumed by sourced lifecycle scripts.
set -euo pipefail

HAB_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HAB_PROJECT_ROOT="$(cd "$HAB_SCRIPT_DIR/.." && pwd)"
HAB_APP_NAME="hermes-antigravity-bridge"
HAB_INSTALL_ROOT="${HAB_INSTALL_ROOT:-$HOME/.local/share/$HAB_APP_NAME}"
HAB_CONFIG_ROOT="${HAB_CONFIG_ROOT:-$HOME/.config/$HAB_APP_NAME}"
HAB_SYSTEMD_USER_DIR="${HAB_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
HAB_UNIT_DEST="${HAB_UNIT_DEST:-$HAB_SYSTEMD_USER_DIR/$HAB_APP_NAME.service}"
HAB_CONFIG_FILE="${HAB_CONFIG_FILE:-$HAB_CONFIG_ROOT/config.toml}"
HAB_TOKEN_FILE="${HAB_TOKEN_FILE:-$HAB_CONFIG_ROOT/bridge.token}"
HAB_CURRENT_LINK="${HAB_CURRENT_LINK:-$HAB_INSTALL_ROOT/current}"
HAB_PREVIOUS_LINK="${HAB_PREVIOUS_LINK:-$HAB_INSTALL_ROOT/previous}"
HAB_RELEASES_DIR="${HAB_RELEASES_DIR:-$HAB_INSTALL_ROOT/releases}"
HAB_RUNTIME_DIR="${HAB_RUNTIME_DIR:-$HAB_INSTALL_ROOT/runtime}"
HAB_AGY_HOME="${HAB_AGY_HOME:-$HOME/.local/state/$HAB_APP_NAME/agy-home}"
HAB_AGY_SETTINGS="${HAB_AGY_SETTINGS:-$HAB_AGY_HOME/.gemini/antigravity-cli/settings.json}"
if [[ -n "${HAB_PYTHON_BIN:-}" ]]; then
    if command -v "$HAB_PYTHON_BIN" >/dev/null 2>&1; then
        HAB_PYTHON_BIN="$(command -v "$HAB_PYTHON_BIN")"
    fi
else
    HAB_PYTHON_BIN="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
fi
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
    elif [[ -d "$HAB_CURRENT_LINK" ]]; then
        (cd "$HAB_CURRENT_LINK" && pwd -P)
    fi
}

hab_previous_target() {
    if [[ -L "$HAB_PREVIOUS_LINK" ]]; then
        readlink -f "$HAB_PREVIOUS_LINK"
    elif [[ -d "$HAB_PREVIOUS_LINK" ]]; then
        (cd "$HAB_PREVIOUS_LINK" && pwd -P)
    fi
}

hab_systemctl() {
    systemctl --user "$@"
}

hab_service_health() {
    local token host port restore_xtrace=0
    [[ -f "$HAB_TOKEN_FILE" ]] || hab_die "token file missing: $HAB_TOKEN_FILE"
    [[ -r "$HAB_TOKEN_FILE" ]] || hab_die "token file is not readable: $HAB_TOKEN_FILE"

    if [[ "$-" == *x* ]]; then
        restore_xtrace=1
        set +x
    fi

    token="$(tr -d '\r\n' < "$HAB_TOKEN_FILE")"
    token="${token//\"/}"
    token="$(printf '%s' "$token" | xargs)"
    if [[ -z "$token" ]]; then
        [[ "$restore_xtrace" == "1" ]] && set -x
        hab_die "token file is empty: $HAB_TOKEN_FILE"
    fi

    local py_bin="$HAB_CURRENT_LINK/venv/bin/python"
    if [[ ! -x "$py_bin" ]]; then
        if [[ -n "$HAB_PYTHON_BIN" ]] && command -v "$HAB_PYTHON_BIN" >/dev/null 2>&1; then
            py_bin="$(command -v "$HAB_PYTHON_BIN")"
        else
            py_bin=""
        fi
    fi
    if [[ -z "$py_bin" || ! -x "$py_bin" ]]; then
        unset token
        [[ "$restore_xtrace" == "1" ]] && set -x
        hab_die "python interpreter not found for health check"
    fi

    [[ -f "$HAB_CONFIG_FILE" ]] || {
        unset token
        [[ "$restore_xtrace" == "1" ]] && set -x
        hab_die "config file missing: $HAB_CONFIG_FILE"
    }

    if ! read -r host port < <(
        "$py_bin" - "$HAB_CONFIG_FILE" <<'PY'
import sys
host, port = "127.0.0.1", 8765
try:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    with open(sys.argv[1], "rb") as f:
        data = tomllib.load(f)
    server = data.get("server", {})
    host = server.get("host", host)
    port = server.get("port", port)
except Exception:
    try:
        from hermes_antigravity_bridge.config import BridgeConfig
        config = BridgeConfig.load(sys.argv[1])
        host, port = config.server.host, config.server.port
    except Exception:
        try:
            in_server = False
            with open(sys.argv[1], "r", encoding="utf-8") as f:
                for line in f:
                    s = line.split("#", 1)[0].strip()
                    if s.startswith("[") and s.endswith("]"):
                        in_server = (s == "[server]")
                    elif in_server and "=" in s:
                        k, v = [x.strip() for x in s.split("=", 1)]
                        if k == "host":
                            host = v.strip("\"'")
                        elif k == "port":
                            port = v.strip("\"'")
        except Exception:
            pass
print(host, port)
PY
    ); then
        unset token
        [[ "$restore_xtrace" == "1" ]] && set -x
        return 1
    fi

    host="${host//$'\r'/}"
    port="${port//$'\r'/}"

    if [[ -z "${host:-}" || -z "${port:-}" ]]; then
        unset token
        [[ "$restore_xtrace" == "1" ]] && set -x
        hab_die "failed to resolve bridge host and port from $HAB_CONFIG_FILE"
    fi

    if [[ "$host" == "0.0.0.0" || "$host" == "::" ]]; then
        host="127.0.0.1"
    fi

    local healthy=0
    for _ in {1..15}; do
        if curl -fsS --max-time 2 "http://$host:$port/health" >/dev/null 2>&1; then
            healthy=1
            break
        fi
        sleep 1
    done
    if [[ "$healthy" != "1" ]]; then
        unset token
        [[ "$restore_xtrace" == "1" ]] && set -x
        return 1
    fi

    local ready_output healthy_ready=0
    for _ in {1..3}; do
        if ready_output="$(printf 'header = "Authorization: Bearer %s"\n' "$token" | \
            curl -fsS --max-time 35 -K - "http://$host:$port/ready" 2>/dev/null)"; then
            if [[ "$ready_output" == *'"status":"ready"'* || "$ready_output" == *'"status": "ready"'* ]]; then
                healthy_ready=1
                break
            fi
        fi
        sleep 1
    done
    if [[ "$healthy_ready" != "1" ]]; then
        if [[ -n "${ready_output:-}" ]]; then
            printf 'error: readiness check failed (service status not ready): %s\n' "$ready_output" >&2
        else
            printf 'error: readiness check failed (endpoint unreachable or unauthorized)\n' >&2
        fi
        unset token
        [[ "$restore_xtrace" == "1" ]] && set -x
        return 1
    fi

    unset token
    [[ "$restore_xtrace" == "1" ]] && set -x
    return 0
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
