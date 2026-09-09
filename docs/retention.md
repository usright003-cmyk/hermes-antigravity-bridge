# Antigravity Retention

Each bridge request starts a fresh Antigravity print conversation. No conversation is resumed, but `agy` may persist:

- conversation databases
- Step 0 prompt content
- model responses
- logs and brain artifacts
- usage metadata

These artifacts can contain Hermes-supplied context, including memory selected by Hermes.

The bridge never reads historical Antigravity conversations to construct future requests. Continuity comes only from Hermes.

Install, update, rollback, and uninstall never purge Antigravity state. Retention cleanup is intentionally outside Phase 1 because it requires separate user consent, version-specific path validation, and a reviewed policy.
