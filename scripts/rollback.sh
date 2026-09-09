#!/usr/bin/env bash
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

hab_safe_bridge_path "$HAB_INSTALL_ROOT" || hab_die "unsafe install root: $HAB_INSTALL_ROOT"
current="$(hab_current_target || true)"
previous="$(hab_previous_target || true)"
[[ -n "$current" && -n "$previous" ]] || hab_die "both current and previous releases are required"
[[ -x "$previous/venv/bin/hermes-antigravity-bridge" ]] || hab_die "previous release is incomplete"

hab_atomic_link "$previous" "$HAB_CURRENT_LINK"
hab_atomic_link "$current" "$HAB_PREVIOUS_LINK"
if [[ "$HAB_SKIP_SERVICE" != "1" ]]; then
    if ! hab_systemctl restart "$HAB_APP_NAME.service" || ! hab_service_health; then
        hab_atomic_link "$current" "$HAB_CURRENT_LINK"
        hab_atomic_link "$previous" "$HAB_PREVIOUS_LINK"
        hab_systemctl restart "$HAB_APP_NAME.service" || true
        hab_die "rollback candidate failed validation; original release restored"
    fi
fi
printf 'current=%s\nprevious=%s\n' "$previous" "$current"
