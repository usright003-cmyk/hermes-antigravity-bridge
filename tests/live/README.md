# Opt-in live validation

These tests require a running bridge backed by an authenticated, dedicated Antigravity home. They are never run by public CI.

```bash
export HAB_LIVE_BASE_URL=http://127.0.0.1:8765
export HAB_LIVE_TOKEN_FILE=$HOME/.config/hermes-antigravity-bridge/bridge.token
python3 tests/live/run_live_validation.py
```

The harness creates a temporary `HERMES_HOME` containing synthetic memory and removes it afterward. It does not read production Hermes memory or Antigravity databases.
