# Hermes Context Policy

Hermes supplies the entire request context. The bridge does not independently retrieve memory or history.

Priority order:

1. Fixed ownership/tool instructions.
2. Complete latest user request.
3. Required Hermes system/developer context, including memory Hermes already injected.
4. Compact, complete tool-schema JSON records.
5. Newest complete history/tool-result JSON records.
6. Final current-request guard.

The 4,000,000-character (~1,000,000 tokens at 4 chars/token) default aligns with Google Gemini's native context window while maintaining deterministic character/token budgeting. Configured model limits (e.g. 200k tokens for Claude, 128k tokens for GPT) or custom caps (`AGY_MAX_PROMPT_CHARS`) may adjust or constrain it.

The latest request is serialized without low-entropy normalization. If it cannot fit, the bridge fails explicitly. Non-latest repeated-character runs of 512 or more non-whitespace characters are represented as:

```text
[bridge compacted repeated character U+XXXX x COUNT]
```

Every tool/history record is JSON-encoded before budget selection. Oversized records are clipped inside their `content` string and re-encoded, so no structural JSON object is cut mid-object.
