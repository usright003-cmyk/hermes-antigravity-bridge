"""Translate tagged model output into validated OpenAI tool calls."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Collection
from typing import Any

from .contracts import ParsedAssistantOutput
from .errors import InvalidToolCall

_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(?:```(?:json)?\s*)?(\{.*?\})\s*(?:```\s*)?</tool_call>",
    re.DOTALL,
)
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_MAX_TOOL_CALLS = 16
_MAX_BLOCK_CHARS = 65_536
_MAX_ARGUMENT_CHARS = 65_536


def parse_tool_calls(
    text: str,
    *,
    allowed_tool_names: Collection[str],
    id_factory: Callable[[], str] | None = None,
) -> ParsedAssistantOutput:
    if not isinstance(text, str) or not text.strip():
        return ParsedAssistantOutput(text="")
    make_id = id_factory or (lambda: f"call_{uuid.uuid4().hex}")
    calls: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []

    for match in _TOOL_CALL_RE.finditer(text):
        if len(calls) >= _MAX_TOOL_CALLS:
            raise InvalidToolCall(f"model emitted more than {_MAX_TOOL_CALLS} tool calls")
        raw = match.group(1).strip()
        if len(raw) > _MAX_BLOCK_CHARS:
            raise InvalidToolCall("tool-call payload exceeds the configured limit")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvalidToolCall("model emitted a malformed tool-call payload") from exc
        if not isinstance(payload, dict):
            raise InvalidToolCall("tool-call payload must be a JSON object")

        fn_value = payload.get("function")
        function = fn_value if isinstance(fn_value, dict) else payload
        name = function.get("name")
        if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name.strip()):
            raise InvalidToolCall("tool-call name is missing or invalid")
        name = name.strip()
        if name not in allowed_tool_names:
            raise InvalidToolCall(f"tool '{name}' was not advertised by Hermes")

        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            if len(arguments) > _MAX_ARGUMENT_CHARS:
                raise InvalidToolCall("tool-call arguments exceed the configured limit")
            try:
                parsed_arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise InvalidToolCall("tool-call arguments are not valid JSON") from exc
            if not isinstance(parsed_arguments, dict):
                raise InvalidToolCall("tool-call arguments must decode to a JSON object")
            argument_text = arguments
        elif isinstance(arguments, dict):
            argument_text = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            if len(argument_text) > _MAX_ARGUMENT_CHARS:
                raise InvalidToolCall("tool-call arguments exceed the configured limit")
        else:
            raise InvalidToolCall("tool-call arguments must be a JSON object or object string")

        call_id = payload.get("id") or make_id()
        if not isinstance(call_id, str) or not call_id.strip() or len(call_id) > 128:
            raise InvalidToolCall("tool-call id is invalid")
        calls.append(
            {
                "index": len(calls),
                "id": call_id.strip(),
                "type": "function",
                "function": {"name": name, "arguments": argument_text},
            }
        )
        spans.append((match.start(), match.end()))

    if not calls:
        if "<tool_call>" in text or "</tool_call>" in text:
            raise InvalidToolCall("model emitted a malformed tool-call block")
        return ParsedAssistantOutput(text=text.strip())

    cleaned = text
    for start, end in reversed(spans):
        cleaned = cleaned[:start] + cleaned[end:]
    return ParsedAssistantOutput(text=cleaned.strip(), tool_calls=tuple(calls))
