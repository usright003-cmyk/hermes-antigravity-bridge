# Security and Privacy

## Why strict Antigravity permissions are mandatory

Antigravity is an agent runtime, not a model-only API. Testing showed `--sandbox` and `--mode plan` did not prevent internal terminal use when Antigravity was configured with `always-proceed`. With a dedicated home configured for `toolPermission: strict`, no allow rules, no trusted workspaces, and no always-proceed artifact policy, the same adversarial request was denied and the protected file remained absent on `agy` 1.1.28.

The bridge checks these settings during readiness and before every generation. A configuration change fails closed.

## Remaining trust boundary

This is defense in depth around a proprietary CLI, not a formal proof. Antigravity still communicates with remote providers and may change behavior in future versions. This is why the version allowlist is fail-closed.

## Network exposure

Keep the bridge on loopback. If remote access is explicitly enabled, use an authenticated TLS reverse proxy, firewall rules, and a separately reviewed deployment. The built-in HTTP server does not provide TLS.

## Data handling

Hermes memory, history, system instructions, and tool results can be sent to the selected remote model and written to local Antigravity artifacts. The bridge logs only model ID, message count, prompt length, and limit.
