# Architecture

Hermes is the stateful agent. The bridge is a stateless provider adapter.

```text
Hermes OpenAI request
  -> HTTP authentication and request limits
  -> ChatCompletionService
  -> HermesPromptBuilder
  -> PromptBudget + generic prompt primitives
  -> AntigravityBackend
  -> isolated agy print turn
  -> validated text / tagged tool calls
  -> OpenAI-compatible response
  -> Hermes executes tools and persists the turn
```

## Module boundaries

- `contracts.py`: transport-neutral values and backend protocol.
- `prompt/`: generic clipping, normalization, JSONL serialization, and limits.
- `integrations/hermes.py`: Hermes ownership language, section allocation, and latest-request guard.
- `backends/antigravity.py`: CLI discovery, compatibility checks, isolation checks, process lifecycle, and stream protocol.
- `tool_calls.py`: validates model-emitted calls against Hermes-advertised tools.
- `service.py`: composes request validation, prompt creation, backend generation, and response normalization.
- `openai_http.py`: local HTTP transport only.
- `cli.py`: configuration and process composition root.

No module reads Hermes memory or session storage. No Antigravity conversation ID is passed between requests.

## Future adapter seam

A future integration can map its request into the transport-neutral contracts and provide a separate prompt policy. Phase 1 exports no dynamic integration registry and contains no OpenClaw code.
