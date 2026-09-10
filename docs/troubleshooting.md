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
