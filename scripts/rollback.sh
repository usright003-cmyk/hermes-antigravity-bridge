#!/usr/bin/env bash
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

hab_safe_bridge_path "$HAB_INSTALL_ROOT" || hab_die "unsafe install root: $HAB_INSTALL_ROOT"
current="$(hab_current_target || true)"
previous="$(hab_previous_target || true)"

if [[ -z "$current" ]]; then
    hab_die "no active release found at $HAB_CURRENT_LINK"
fi
if [[ -z "$previous" ]]; then
    hab_die "cannot rollback: no previous release found at $HAB_PREVIOUS_LINK. Rollback requires at least two installed releases (current is the initial release)."
fi
if [[ ! -d "$previous" ]]; then
    hab_die "cannot rollback: previous release directory does not exist: $previous"
fi
if [[ ! -x "$previous/venv/bin/hermes-antigravity-bridge" ]]; then
    hab_die "cannot rollback: previous release binary missing or not executable: $previous/venv/bin/hermes-antigravity-bridge"
fi

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
