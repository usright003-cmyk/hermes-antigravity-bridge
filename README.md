# Hermes Antigravity Bridge

A local, authenticated compatibility bridge that lets Hermes Agent use models exposed by the Antigravity CLI while Hermes remains the source of truth for memory, sessions, skills, tools, context selection, model switching, and agent behavior.

Phase 1 supports Hermes Agent only. OpenClaw and other agent integrations are intentionally out of scope.

## Status

Version 0.1.0 is alpha software. The prompt policy is frozen against a behaviorally validated reference bridge, but external users should read the security and retention sections before installing.

Validated runtime compatibility:

- Linux with systemd user services
- Python 3.10+
- Antigravity CLI `agy` 1.1.28
- Hermes custom OpenAI-compatible providers

The bridge fails readiness checks on unvalidated `agy` versions instead of assuming security-sensitive CLI behavior is unchanged.

## Ownership model

```text
Telegram / CLI / Web
        |
        v
Hermes Agent
  owns USER.md / MEMORY.md
  owns session state and conversation continuity
  selects relevant context and skills
  executes all approved tools
        |
        | authenticated OpenAI-style request
        v
Hermes Antigravity Bridge
  validates the request
  applies deterministic context budgets
  anchors the complete latest user request
  starts one stateless, sandboxed agy print turn
  validates tagged tool calls against Hermes schemas
        |
        v
Antigravity CLI -> selected model
```

The bridge never opens Hermes memory files or `state.db`, never creates a competing memory store, and never resumes an Antigravity conversation for continuity. Hermes supplies continuity on every request.

## Security gate

Antigravity is an agent runtime with its own tools. `--sandbox` and plan mode alone are not enough to prevent autonomous tool execution. This project therefore uses all of these controls:

- A dedicated Antigravity `HOME`
- `toolPermission: strict`
- No Antigravity permission allow rules
- No trusted workspaces
- No `always-proceed` artifact policy
- `--sandbox`
- `--mode plan`
- A new empty working directory for each request
- Fail-closed detection of tool activity exposed by the stream protocol
- systemd filesystem hardening

`/ready` and every generation re-check strict permission settings. Unsafe settings stop the bridge.

See [SECURITY.md](SECURITY.md) and [docs/security-and-privacy.md](docs/security-and-privacy.md).

## Quick start

Prerequisites:

- Hermes Agent
- An installed `agy` CLI
- Python 3.10+
- `uv`, or Python with working `venv`/`pip`
- Linux/systemd for managed service installation

Prepare the isolated installation without starting a service:

```bash
./scripts/install.sh --prepare
```

The installer prints the dedicated Antigravity home. Authenticate it once:

```bash
HOME="$HOME/.local/state/hermes-antigravity-bridge/agy-home" agy
```

Then install and validate the service:

```bash
./scripts/install.sh
```

The installer generates a random bearer token and does not modify Hermes configuration. Complete Hermes setup using [docs/hermes-setup.md](docs/hermes-setup.md).

## Commands

```bash
hermes-antigravity-bridge --version
hermes-antigravity-bridge --config ~/.config/hermes-antigravity-bridge/config.toml check
hermes-antigravity-bridge --config ~/.config/hermes-antigravity-bridge/config.toml models
hermes-antigravity-bridge --config ~/.config/hermes-antigravity-bridge/config.toml serve
```

## Lifecycle

```bash
./scripts/update.sh
./scripts/rollback.sh
./scripts/uninstall.sh
```

Updates install into immutable versioned release directories and atomically move a `current` symlink. Rollback swaps `current` and `previous`. Default uninstall preserves releases, configuration, the generated token, and the dedicated Antigravity state.

No lifecycle script deletes Hermes data or Antigravity conversations/databases. See [docs/install-update-rollback.md](docs/install-update-rollback.md).

## API subset

- `GET /health` — public liveness only
- `GET /ready` — authenticated Antigravity/version/isolation readiness
- `GET /v1/models` — authenticated account-backed model catalog
- `GET /v1/models/{id}` — authenticated model lookup
- `POST /v1/chat/completions` — authenticated buffered Chat Completions subset

This is not a complete OpenAI API implementation. See [docs/api-contract.md](docs/api-contract.md).

## Context guarantees

- Operational prompt cap remains 64,000 characters by default.
- The complete latest user request is never normalized or silently clipped.
- If the latest request cannot fit with required instructions, the request fails explicitly.
- Non-latest low-entropy runs may be compacted deterministically.
- Tool schemas and history are serialized as complete JSONL records.
- Historical context is separated from `CURRENT_USER_REQUEST_JSON`.
- The final current-request guard makes the active task unambiguous.

See [docs/context-policy.md](docs/context-policy.md).

## Testing

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
bash -n scripts/*.sh
python3 -m compileall -q src tests
```

Public CI uses a fake `agy` executable and never requires account credentials. Real-account tests are opt-in and must use synthetic memory and a dedicated Antigravity home.

## Retention

`agy` may create local conversation records, logs, and brain artifacts for each independent print call. The bridge does not read those records for continuity and never purges them automatically. See [docs/retention.md](docs/retention.md).

## Non-affiliation

This project is an independent compatibility integration. It is not affiliated with or endorsed by Google, Antigravity, Anthropic, or the vendors of models exposed by `agy`. It does not redistribute Antigravity binaries, credentials, databases, or proprietary assets.

## License

MIT. See [LICENSE](LICENSE).
