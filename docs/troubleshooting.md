# Troubleshooting

## `/ready` rejects permission settings

Use the dedicated Antigravity home and keep its settings strict:

```json
{
  "artifactReviewPolicy": "asks-for-review",
  "permissions": {"allow": []},
  "toolPermission": "strict",
  "trustedWorkspaces": []
}
```

Do not weaken this check to make installation pass.

## Dedicated account is not authenticated

```bash
HOME="$HOME/.local/state/hermes-antigravity-bridge/agy-home" agy
```

Complete the normal Antigravity sign-in, then rerun `scripts/install.sh`.

## CLI version is not validated

Install the documented `agy` version or wait for a bridge release that tests the newer version. Do not bypass the allowlist without repeating normal and adversarial isolation tests.

## Empty or false-success response

The bridge treats `SUCCESS` plus empty response and zero usage/duration as an upstream protocol error. Inspect Antigravity's dedicated-home logs; do not read an old conversation as a fallback response.

## Hermes receives an older topic

Check that the current request appears once under `CURRENT_USER_REQUEST_JSON`, inspect prompt lengths rather than prompt contents, and run the golden/context tests. Do not increase the 64,000-character cap as a substitute for fixing prioritization.

## `/ready` returns HTTP 401 Unauthorized

The `/ready` endpoint requires a valid Bearer token for authorization. An unauthenticated `curl -s http://127.0.0.1:8765/ready` will return `HTTP 401 Unauthorized`.

Pass the bearer token generated at installation:
```bash
TOKEN="$(tr -d '\r\n' < "$HOME/.config/hermes-antigravity-bridge/bridge.token")"
curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/ready
unset TOKEN
```

## `rollback.sh` fails: "cannot rollback: no previous release found"

The rollback script requires at least two installed releases (`current` and `previous`). On an initial release before any update has been applied, `previous` does not exist yet.

When you install or update to a new release via `./scripts/install.sh`, the installer automatically moves the old `current` release to `previous`. Rollback is then immediately available.

