#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

if len(sys.argv) > 1 and sys.argv[1] == "models":
    print("gemini-test-high\tGemini Test High")
    print("gemini-test-low\tGemini Test Low")
    raise SystemExit(0)
if "--help" in sys.argv:
    print("--input-format --output-format --disable-slash-commands --sandbox --mode --print-timeout --model", file=sys.stderr)
    raise SystemExit(0)
if "--version" in sys.argv:
    print(os.environ.get("FAKE_AGY_VERSION", "1.1.28"))
    raise SystemExit(0)

prompt = sys.stdin.read()
record = os.environ.get("FAKE_AGY_RECORD")
if record:
    Path(record).write_text(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "home": os.environ.get("HOME"), "prompt": prompt}), encoding="utf-8")
if "FAKE_SLEEP" in prompt:
    time.sleep(5)
if "FAKE_TOOL_EVENT" in prompt:
    print(json.dumps({"event": "tool_call", "tool": {"name": "terminal", "arguments": {"command": "touch forbidden"}}}), flush=True)
if "FAKE_EMPTY" in prompt:
    result = {
        "status": "SUCCESS",
        "response": "",
        "duration_seconds": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 0},
        "conversation_id": "fake-empty",
    }
else:
    result = {
        "status": "SUCCESS",
        "response": "FAKE_OK",
        "duration_seconds": 0.01,
        "usage": {"input_tokens": 11, "output_tokens": 2, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 13},
        "conversation_id": "fake-success",
    }
print(json.dumps({"event": "result", "result": result}), flush=True)
