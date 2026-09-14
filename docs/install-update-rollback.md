# Install, Update, Rollback, and Uninstall

## Production Deployment & Durable Repository Path

Always run lifecycle operations from a **durable repository path** (e.g., `~/hermes-antigravity-bridge`, `/opt/hermes-antigravity-bridge`, or `~/.local/src/hermes-antigravity-bridge`).

> [!WARNING]
> **Never use temporary directories** (such as `/tmp/...`) for production deployment. Temporary directories are subject to OS cleanup or reboot removal, which breaks relative source references during updates.

```bash
# Clone or navigate to your durable repository path:
cd ~/hermes-antigravity-bridge
git fetch origin
git checkout main
```

## Prepare and Authenticate (First-Time Setup Only)

```bash
./scripts/install.sh --prepare
HOME="$HOME/.local/state/hermes-antigravity-bridge/agy-home" agy
./scripts/install.sh
```

The dedicated home (`~/.local/state/hermes-antigravity-bridge/agy-home`) separates bridge permissions and Antigravity retention from a user's normal Antigravity workspace. The installer creates strict settings but never copies OAuth credentials; authentication remains a user-controlled action.

> [!NOTE]
> **Existing Authenticated Sessions**: Upgrades and rollbacks preserve `AGY_HOME` completely. If `agy` was already authenticated in `AGY_HOME`, no new Google login, OAuth code, password, or credential is required.

## Paths & Release Pointer Architecture

- Releases store: `~/.local/share/hermes-antigravity-bridge/releases/`
- Active pointer: `~/.local/share/hermes-antigravity-bridge/current`
- Previous pointer: `~/.local/share/hermes-antigravity-bridge/previous`
- Non-secret config: `~/.config/hermes-antigravity-bridge/config.toml`
- Bearer token: `~/.config/hermes-antigravity-bridge/bridge.token`
- Dedicated Antigravity state: `~/.local/state/hermes-antigravity-bridge/agy-home`
- User systemd unit: `~/.config/systemd/user/hermes-antigravity-bridge.service`

### How Release Pointers Transition

1. **Initial Install**: Only `current` is created (e.g. pointing to release `73c3f84`). At this point, `previous` is absent because exactly one release exists.
2. **First Upgrade** (e.g. to commit `9e2e336`):
   - The installer automatically shifts the existing `current` release to become `previous` (`previous -> 73c3f84`).
   - The new build is linked to `current` (`current -> 9e2e336`).
   - Automated health and authenticated readiness checks validate the new release.
   - If validation passes, both `current` and `previous` symlinks remain established. Rollback is now immediately available.
   - If validation fails, `current` is safely restored back to `73c3f84`, and the incomplete `previous` pointer is cleaned up.

## Systemd Services

On production Linux VMs, the bridge and Hermes operate under two distinct systemd user services:

| Service | Name | Command to Inspect |
| :--- | :--- | :--- |
| **Bridge Service** | `hermes-antigravity-bridge.service` | `systemctl --user status hermes-antigravity-bridge.service` |
| **Hermes Gateway** | `hermes-gateway.service` | `systemctl --user status hermes-gateway.service` |

## Production Upgrade Workflow

To upgrade an existing production installation (e.g. from `73c3f84` to `main` containing `9e2e336` and deployment fixes):

```bash
# 1. Enter the durable repository path
cd ~/hermes-antigravity-bridge

# 2. Fetch the latest release commit on main
git fetch origin
git checkout main

# 3. Run the installer (or ./scripts/update.sh)
./scripts/install.sh

# 4. Verify authenticated bridge readiness
TOKEN="$(tr -d '\r\n' < "$HOME/.config/hermes-antigravity-bridge/bridge.token")"
curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/ready
unset TOKEN

# 5. Restart Hermes Gateway to refresh model cache and sessions
systemctl --user restart hermes-gateway.service

# 6. Verify Hermes Gateway status
systemctl --user status hermes-gateway.service
```

> [!TIP]
> `main` contains commit `9e2e336` (the tool-call, soft-denial, and multimodal fixes verified in staging) along with the hardened authenticated readiness health check and rollback protections. Always deploy from `main` rather than checking out raw commit `9e2e336` directly so the deployment fixes are active during installation.

## Authenticated Readiness Verification

The bridge `/ready` endpoint enforces Bearer token authentication. Unauthenticated requests (`curl -s http://127.0.0.1:8765/ready`) will return **HTTP 401 Unauthorized**.

To verify readiness safely without leaking the bearer token in shell history or `/proc` process listings:

```bash
TOKEN="$(tr -d '\r\n' < "$HOME/.config/hermes-antigravity-bridge/bridge.token")"
curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/ready
unset TOKEN
```

Expected response:
```json
{
  "status": "ready",
  "authenticated": true,
  "agy_version": "1.2.2",
  "models": [
    "gemini-3.8-flash-high",
    "gemini-3-pro-low"
  ],
  "model_source": "discovered",
  "sandbox": true,
  "mode": "plan",
  "stateless": true
}
```

The installer's internal healthcheck (`hab_service_health`) automatically performs this authenticated check by passing the token via stdin config (`curl -K -`), preventing the credential from ever appearing in process tables or systemd logs, and validates that `status` is `"ready"` (rejecting degraded states).

## Rollback

```bash
# 1. Enter the durable repository path
cd ~/hermes-antigravity-bridge

# 2. Execute rollback (swaps current and previous, restarts bridge service, validates health)
./scripts/rollback.sh

# 3. Verify authenticated bridge readiness on rolled-back release
TOKEN="$(tr -d '\r\n' < "$HOME/.config/hermes-antigravity-bridge/bridge.token")"
curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/ready
unset TOKEN

# 4. Restart Hermes Gateway
systemctl --user restart hermes-gateway.service

# 5. Verify Hermes Gateway status
systemctl --user status hermes-gateway.service
```

- **Prerequisite**: At least two releases must exist (`current` and `previous`).
- If attempted on an initial release before any update, `rollback.sh` will exit safely with:
  `error: cannot rollback: no previous release found at ~/.local/share/hermes-antigravity-bridge/previous. Rollback requires at least two installed releases (current is the initial release).`
- When executed with two valid releases, `rollback.sh` atomically swaps `current` and `previous`, restarts `hermes-antigravity-bridge.service`, verifies health and authenticated readiness, and reports the swapped release paths.
- If health validation fails on the rollback candidate, the original release pointers are restored automatically.

## Uninstall

```bash
./scripts/uninstall.sh
```

Default uninstall stops and disables `hermes-antigravity-bridge.service`, removes the user systemd unit and symlinks, but preserves releases, configuration, token, and dedicated Antigravity state.

To remove bridge-owned releases and configuration as well:
```bash
./scripts/uninstall.sh --purge-bridge-files
```

No lifecycle command removes Hermes files, global Antigravity files, or dedicated Antigravity conversation databases.

