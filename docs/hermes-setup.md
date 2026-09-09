# Hermes Setup

The installer deliberately does not modify Hermes configuration or memory.

Read the generated token without printing it into shell history, then place it in Hermes's secret environment file under a private variable name:

```text
HERMES_ANTIGRAVITY_BRIDGE_TOKEN=<contents of ~/.config/hermes-antigravity-bridge/bridge.token>
```

Configure the custom provider using Hermes commands and an environment reference rather than a literal credential:

```bash
hermes config set model.provider custom
hermes config set model.base_url http://127.0.0.1:8765/v1
hermes config set model.api_key '${HERMES_ANTIGRAVITY_BRIDGE_TOKEN}'
```

Register models through Hermes's model/provider setup UI or aliases. Model IDs come from:

```bash
hermes-antigravity-bridge --config ~/.config/hermes-antigravity-bridge/config.toml models
```

The bridge never reads `MEMORY.md`, `USER.md`, `state.db`, skills, or gateway configuration. Hermes injects memory and context into its request and executes any returned tool calls.
