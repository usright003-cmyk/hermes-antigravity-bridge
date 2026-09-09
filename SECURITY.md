# Security Policy

## Supported version

Only the latest tagged release receives security fixes. Version 0.1.0 validates `agy` 1.1.28; newer CLI versions require a new compatibility review because permission, sandbox, event, and print-mode behavior may change.

## Threat model

The bridge protects an authenticated Antigravity account from untrusted local callers and prevents Antigravity from becoming a second owner of Hermes memory, sessions, or tools.

Primary risks:

- A local process spends account quota through the bridge.
- A model invokes Antigravity's internal filesystem or terminal tools outside Hermes approval.
- Hermes memory or tool output appears in local Antigravity logs/conversation records.
- Oversized or concurrent requests exhaust CPU, memory, subprocesses, or quota.
- Model output fabricates a tool not supplied by Hermes.
- Secrets or raw upstream errors reach logs or HTTP clients.

## Enforced controls

- Loopback-only binding by default; remote binding needs an explicit opt-in.
- Random bearer token, stored outside the repository in a mode-0600 file.
- Constant-time bearer comparison.
- Authentication on completion, readiness, and model endpoints.
- Strict request/body/concurrency limits.
- Dedicated Antigravity home and empty per-request working directory.
- Strict Antigravity permissions, no allow rules, no trusted workspaces.
- `--sandbox`, `--mode plan`, and disabled slash-command expansion.
- No `--continue` or `--conversation` flags.
- Process-group termination on timeout or isolation failures.
- Tool calls accepted only when the exact tool name was supplied by Hermes.
- Prompt bodies, memory, tool results, and secrets are never logged.
- Installer/update/rollback/uninstall touch only enumerated bridge-owned paths and never purge Antigravity or Hermes state.

## Important boundary

Antigravity itself communicates with remote model services and may retain local request artifacts. Strict permission mode was adversarially tested against `agy` 1.1.28, but this project cannot provide a formal sandbox guarantee for proprietary upstream behavior. Do not run it with sensitive context you would not send to the selected provider.

## Secrets

Never commit:

- bridge tokens
- `.env`
- Hermes configuration, `auth.json`, memory, or `state.db`
- Antigravity OAuth tokens, settings containing personal paths, or databases
- Telegram or other gateway credentials

## Reporting

Before a public repository exists, report security issues privately to the maintainer. After publication, use GitHub's private vulnerability-reporting feature. Do not open a public issue containing credentials, memory content, or proof-of-concept data from a real account.
