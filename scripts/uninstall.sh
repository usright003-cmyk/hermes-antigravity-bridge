#!/usr/bin/env bash
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

purge=0
if [[ "${1:-}" == "--purge-bridge-files" ]]; then
    purge=1
elif [[ $# -gt 0 ]]; then
    hab_die "usage: uninstall.sh [--purge-bridge-files]"
fi
hab_safe_bridge_path "$HAB_INSTALL_ROOT" || hab_die "unsafe install root: $HAB_INSTALL_ROOT"
hab_safe_bridge_path "$HAB_CONFIG_ROOT" || hab_die "unsafe config root: $HAB_CONFIG_ROOT"

if [[ "$HAB_SKIP_SERVICE" != "1" ]]; then
    hab_systemctl disable --now "$HAB_APP_NAME.service" || true
fi
rm -f "$HAB_UNIT_DEST" "$HAB_CURRENT_LINK" "$HAB_PREVIOUS_LINK"
if [[ "$HAB_SKIP_SERVICE" != "1" ]]; then
    hab_systemctl daemon-reload
fi
if [[ "$purge" == "1" ]]; then
    rm -rf "$HAB_INSTALL_ROOT" "$HAB_CONFIG_ROOT"
    printf '%s\n' 'Removed bridge-owned releases and configuration.'
else
    printf '%s\n' 'Preserved bridge releases and configuration. Use --purge-bridge-files to remove only those bridge-owned paths.'
fi
printf '%s\n' 'Hermes and Antigravity data were not modified.'
