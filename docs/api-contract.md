# HTTP API Contract

This project implements the subset Hermes needs; it is not a complete OpenAI API.

## Authentication

Except for `/health`, endpoints require:

```text
Authorization: Bearer <generated bridge token>
```

## Endpoints

### `GET /health`

Process liveness only. It never invokes `agy`.

### `GET /ready`

Validates strict Antigravity settings, required CLI flags, tested CLI version, account-backed model discovery, and stateless/sandbox mode.

### `GET /v1/models`

Returns the authenticated `agy models` catalog.

### `GET /v1/models/{id}`

Returns one known model or 404. Arbitrary IDs are not accepted.

### Hermes discovery probes

Authenticated compatibility routes `/api/v1/models`, `/api/tags`, `/api/show`, `/version`, and `/api/version` prevent avoidable Hermes discovery/title warnings. They expose no additional generation surface.

### `POST /v1/chat/completions`

Required fields:

- `model`
- `messages`

Supported fields:

- `tools`
- `stream`
- `max_tokens` for conservative budget reservation
- `temperature`, `top_p`, `stop`, `tool_choice`, `user`, `reasoning_effort`, and `stream_options` as accepted Hermes compatibility metadata; these are not forwarded because the CLI exposes no equivalent
- `response_format` as accepted Hermes title-generation metadata; the model is still guided by the supplied prompt because `agy` structured-output support is not used in Phase 1
- `n`, only when omitted or equal to 1

Unknown fields are rejected. Supported roles are `system`, `developer`, `user`, `assistant`, and `tool`.

`stream=true` uses buffered SSE compatibility: generation completes upstream before response chunks are emitted.

Responses include the requested model in `model` and the resolved model in `x_antigravity_model`.

Typed 4xx errors cover authentication, malformed requests, unknown models, unsupported fields, body limits, and concurrency. Upstream details are sanitized from 5xx responses.
