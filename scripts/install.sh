#!/usr/bin/env bash
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

prepare=0
if [[ "${1:-}" == "--prepare" ]]; then
    prepare=1
    HAB_SKIP_SERVICE=1
    HAB_SKIP_AGY_CHECK=1
elif [[ $# -gt 0 ]]; then
    hab_die "usage: install.sh [--prepare]"
fi

[[ -n "$HAB_PYTHON_BIN" && -x "$HAB_PYTHON_BIN" ]] || hab_die "python3 is required"
hab_safe_bridge_path "$HAB_INSTALL_ROOT" || hab_die "unsafe install root: $HAB_INSTALL_ROOT"
hab_safe_bridge_path "$HAB_CONFIG_ROOT" || hab_die "unsafe config root: $HAB_CONFIG_ROOT"

version="$(PYTHONPATH="$HAB_PROJECT_ROOT/src" "$HAB_PYTHON_BIN" -c 'from hermes_antigravity_bridge import __version__; print(__version__)')"
release_id="${version}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
release_dir="$HAB_RELEASES_DIR/$release_id"
old_current="$(hab_current_target || true)"

umask 077
mkdir -p "$HAB_RELEASES_DIR" "$HAB_CONFIG_ROOT" "$HAB_RUNTIME_DIR"
[[ ! -e "$release_dir" ]] || hab_die "release already exists: $release_dir"
mkdir -p "$release_dir"
completed=0
cleanup_incomplete() {
    if [[ "$completed" != "1" ]]; then
        rm -rf "$release_dir"
    fi
}
trap cleanup_incomplete EXIT

if [[ -n "$HAB_UV_BIN" && -x "$HAB_UV_BIN" ]]; then
    "$HAB_UV_BIN" venv --python "$HAB_PYTHON_BIN" "$release_dir/venv"
    uv_args=(pip install --python "$release_dir/venv/bin/python")
    if [[ "$HAB_PIP_NO_DEPS" == "1" ]]; then
        uv_args+=(--no-deps)
    fi
    "$HAB_UV_BIN" "${uv_args[@]}" "$HAB_PROJECT_ROOT"
else
    if ! "$HAB_PYTHON_BIN" -m venv "$release_dir/venv"; then
        hab_die "python venv support is unavailable; install python3-venv or uv"
    fi
    pip_args=(install)
    if [[ "$HAB_PIP_NO_DEPS" == "1" ]]; then
        pip_args+=(--no-deps)
    fi
    "$release_dir/venv/bin/python" -m pip "${pip_args[@]}" "$HAB_PROJECT_ROOT"
fi

if [[ ! -f "$HAB_TOKEN_FILE" ]]; then
    "$HAB_PYTHON_BIN" - "$HAB_TOKEN_FILE" <<'PY'
from pathlib import Path
import secrets
import sys
path = Path(sys.argv[1])
path.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
fi
chmod 600 "$HAB_TOKEN_FILE"
if [[ ! -f "$HAB_CONFIG_FILE" ]]; then
    cp "$HAB_PROJECT_ROOT/config/config.example.toml" "$HAB_CONFIG_FILE"
    chmod 600 "$HAB_CONFIG_FILE"
fi
if [[ ! -f "$HAB_AGY_SETTINGS" ]]; then
    mkdir -p "$(dirname "$HAB_AGY_SETTINGS")"
    cat > "$HAB_AGY_SETTINGS" <<'JSON_SETTINGS'
{
  "artifactReviewPolicy": "asks-for-review",
  "permissions": {"allow": []},
  "toolPermission": "strict",
  "trustedWorkspaces": []
}
JSON_SETTINGS
    chmod 600 "$HAB_AGY_SETTINGS"
fi

if [[ "$HAB_SKIP_AGY_CHECK" != "1" ]]; then
    "$release_dir/venv/bin/hermes-antigravity-bridge" --config "$HAB_CONFIG_FILE" check >/dev/null
fi

if [[ -n "$old_current" ]]; then
    hab_atomic_link "$old_current" "$HAB_PREVIOUS_LINK"
fi
hab_atomic_link "$release_dir" "$HAB_CURRENT_LINK"
hab_render_unit "$HAB_CURRENT_LINK/venv/bin/hermes-antigravity-bridge"

if [[ "$HAB_SKIP_SERVICE" != "1" ]]; then
    hab_systemctl daemon-reload
    hab_systemctl enable "$HAB_APP_NAME.service"
    if ! hab_systemctl restart "$HAB_APP_NAME.service" || ! hab_service_health; then
        if [[ -n "$old_current" ]]; then
            hab_atomic_link "$old_current" "$HAB_CURRENT_LINK"
            hab_systemctl restart "$HAB_APP_NAME.service" || true
        else
            rm -f "$HAB_CURRENT_LINK"
            hab_systemctl stop "$HAB_APP_NAME.service" || true
        fi
        hab_die "new release failed health/readiness validation; previous release restored"
    fi
fi
completed=1
trap - EXIT
printf 'installed_version=%s\nrelease=%s\nconfig=%s\ntoken_file=%s\n' \
    "$version" "$release_dir" "$HAB_CONFIG_FILE" "$HAB_TOKEN_FILE"
printf '%s\n' 'Hermes configuration is not changed automatically. See docs/hermes-setup.md.'
if [[ "$prepare" == "1" ]]; then
    printf 'Next authenticate the dedicated Antigravity home, then rerun install.sh:\n  HOME=%q agy\n' "$HAB_AGY_HOME"
fi
