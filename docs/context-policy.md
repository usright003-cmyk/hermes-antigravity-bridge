# Hermes Context Policy

Hermes supplies the entire request context. The bridge does not independently retrieve memory or history.

Priority order:

1. Fixed ownership/tool instructions.
2. Complete latest user request.
3. Required Hermes system/developer context, including memory Hermes already injected.
4. Compact, complete tool-schema JSON records.
5. Newest complete history/tool-result JSON records.
6. Final current-request guard.

The 64,000-character default is an operational bridge cap, not a claim about provider token limits. Optional documented model limits may reduce it. They never increase it.

The latest request is serialized without low-entropy normalization. If it cannot fit, the bridge fails explicitly. Non-latest repeated-character runs of 512 or more non-whitespace characters are represented as:

```text
[bridge compacted repeated character U+XXXX x COUNT]
```

Every tool/history record is JSON-encoded before budget selection. Oversized records are clipped inside their `content` string and re-encoded, so no structural JSON object is cut mid-object.
