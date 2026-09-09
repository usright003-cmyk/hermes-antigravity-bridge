# Contributing

Phase 1 is intentionally limited to Hermes Agent. Do not add OpenClaw or a generic plugin registry without a separately approved design based on a real second integration.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
PYTHONPATH=src python -m unittest discover -s tests -v
bash -n scripts/*.sh
python -m compileall -q src tests
```

## Rules

- Write a failing behavior test before changing runtime behavior.
- Preserve the golden prompt fixtures unless a deliberate, reviewed policy change requires regeneration.
- Never use production Hermes memory, sessions, credentials, or Antigravity databases in tests.
- Keep live-account tests opt-in and synthetic.
- Do not weaken strict tool isolation, latest-request preservation, endpoint authentication, or lifecycle non-purge guarantees.
- Avoid new dependencies unless the standard library cannot safely implement the requirement.
- Never commit secrets or machine-specific paths.

## Pull requests

Include:

- Problem and threat model
- Tests that failed before and pass after
- Compatibility impact for Hermes and `agy`
- Security/retention impact
- Rollback instructions
