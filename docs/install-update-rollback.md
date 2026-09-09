# Install, Update, Rollback, and Uninstall

## Prepare and authenticate

```bash
./scripts/install.sh --prepare
HOME="$HOME/.local/state/hermes-antigravity-bridge/agy-home" agy
./scripts/install.sh
```

The dedicated home separates bridge permissions and Antigravity retention from a user's normal Antigravity workspace. The installer creates strict settings but never copies OAuth credentials; authentication remains a user-controlled action.

## Paths

- Releases: `~/.local/share/hermes-antigravity-bridge/releases/`
- Active pointer: `~/.local/share/hermes-antigravity-bridge/current`
- Previous pointer: `~/.local/share/hermes-antigravity-bridge/previous`
- Non-secret config: `~/.config/hermes-antigravity-bridge/config.toml`
- Bearer token: `~/.config/hermes-antigravity-bridge/bridge.token`
- Dedicated Antigravity state: `~/.local/state/hermes-antigravity-bridge/agy-home`
- User unit: `~/.config/systemd/user/hermes-antigravity-bridge.service`

## Update

```bash
./scripts/update.sh
```

A new release is installed and checked before the `current` symlink is switched. If restart/readiness fails, the previous release is restored.

## Rollback

```bash
./scripts/rollback.sh
```

Only `current`, `previous`, and the bridge service are changed.

## Uninstall

```bash
./scripts/uninstall.sh
```

Default uninstall preserves releases, configuration, token, and dedicated Antigravity state. `--purge-bridge-files` removes only the bridge install/config roots; dedicated Antigravity state is still preserved.

No lifecycle command removes Hermes files, global Antigravity files, or dedicated Antigravity conversation databases.
