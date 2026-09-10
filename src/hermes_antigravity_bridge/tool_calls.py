"""Translate tagged model output into validated OpenAI tool calls."""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable, Collection
from typing import Any

from .contracts import ParsedAssistantOutput
from .errors import InvalidToolCall

_LOG = logging.getLogger(__name__)

_TOOL_TAG_START_RE = re.compile(r"<tool_call(?:\s+[^>]*)?>", re.IGNORECASE)
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_MAX_TOOL_CALLS = 16
_MAX_BLOCK_CHARS = 65_536
_MAX_ARGUMENT_CHARS = 65_536


def _repair_json_string(raw: str) -> str:
    """Best-effort cleanup of common JSON malformations from LLM output."""
    s = raw.strip()
    # Strip markdown code fences if present: ```json ... ```
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
        s = s.strip()
    # Remove trailing commas before closing braces/brackets
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def _extract_json_object(text: str, start_index: int = 0) -> tuple[dict[str, Any] | None, int]:
    """Find and parse the first balanced JSON object starting at or after start_index.

    Returns (parsed_dict, end_offset) or (None, start_index).
    Uses JSONDecoder.raw_decode for nested bracket accuracy.
    """
    first_brace = text.find("{", start_index)
    if first_brace == -1:
        return None, start_index

    decoder = json.JSONDecoder()
    candidate_slice = text[first_brace:]

    # 1. Direct raw_decode attempt
    try:
        obj, offset = decoder.raw_decode(candidate_slice)
        if isinstance(obj, dict):
            return obj, first_brace + offset
    except json.JSONDecodeError:
        pass

    # 2. Repair attempt: clean fences and trailing commas in candidate slice
    repaired = _repair_json_string(candidate_slice)
    try:
        obj, offset = decoder.raw_decode(repaired)
        if isinstance(obj, dict):
            return obj, len(text)
    except json.JSONDecodeError:
        pass

    # 3. Fallback: bracket-counting scan to extract balanced { ... } block
    depth = 0
    in_string = False
    escape = False
    start_pos = -1

    for i in range(first_brace, len(text)):
        char = text[i]
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue

        if char == "{":
            if depth == 0:
                start_pos = i
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start_pos != -1:
                block = text[start_pos : i + 1]
                try:
                    obj = json.loads(block)
                    if isinstance(obj, dict):
                        return obj, i + 1
                except json.JSONDecodeError:
                    repaired_block = _repair_json_string(block)
                    try:
                        obj = json.loads(repaired_block)
                        if isinstance(obj, dict):
                            return obj, i + 1
                    except json.JSONDecodeError:
                        pass
                break
    return None, start_index


def parse_tool_calls(
    text: str,
    *,
    allowed_tool_names: Collection[str],
    id_factory: Callable[[], str] | None = None,
    mode: str = "compatible",
) -> ParsedAssistantOutput:
    if not isinstance(text, str) or not text.strip():
        return ParsedAssistantOutput(text="")

    make_id = id_factory or (lambda: f"call_{uuid.uuid4().hex}")
    calls: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []
    has_tag = False

    for tag_match in _TOOL_TAG_START_RE.finditer(text):
        has_tag = True
        if len(calls) >= _MAX_TOOL_CALLS:
            if mode == "strict":
                raise InvalidToolCall(f"model emitted more than {_MAX_TOOL_CALLS} tool calls")
            _LOG.warning("Model emitted more than %d tool calls; ignoring surplus", _MAX_TOOL_CALLS)
            break

        tag_start = tag_match.start()
        inner_start = tag_match.end()

        # Find closing </tool_call>
        close_idx = text.lower().find("</tool_call>", inner_start)
        if close_idx != -1:
            block_content = text[inner_start:close_idx]
            tag_end = close_idx + len("</tool_call>")
        else:
            # Unclosed tag at end of message or before next tag
            next_tag_match = _TOOL_TAG_START_RE.search(text, inner_start)
            if next_tag_match is not None:
                block_content = text[inner_start:next_tag_match.start()]
                tag_end = next_tag_match.start()
            else:
                block_content = text[inner_start:]
                tag_end = len(text)

        if len(block_content) > _MAX_BLOCK_CHARS:
            if mode == "strict":
                raise InvalidToolCall("tool-call payload exceeds the configured limit")
            _LOG.warning("Tool call block length (%d) exceeds limit; skipping", len(block_content))
            continue

        payload, _ = _extract_json_object(block_content, 0)
        if payload is None:
            if mode == "strict":
                raise InvalidToolCall("model emitted a malformed tool-call payload")
            _LOG.warning(
                "Malformed tool-call block payload (length=%d, mode=compatible); keeping as assistant text",
                len(block_content),
            )
            continue

        # Extract function object or flat payload
        fn_value = payload.get("function")
        function = fn_value if isinstance(fn_value, dict) else payload
        name = function.get("name")
        if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name.strip()):
            if mode == "strict":
                raise InvalidToolCall("tool-call name is missing or invalid")
            _LOG.warning("Tool-call name is missing or invalid; skipping call")
            continue

        clean_name = name.strip()
        if clean_name not in allowed_tool_names and clean_name.startswith("functions."):
            stripped = clean_name.removeprefix("functions.")
            if stripped in allowed_tool_names:
                clean_name = stripped

        if clean_name not in allowed_tool_names:
            if mode == "strict":
                raise InvalidToolCall(f"tool '{clean_name}' was not advertised by Hermes")
            _LOG.warning(
                "Model requested unadvertised tool '%s' (mode=compatible); skipping", clean_name
            )
            continue

        # Extract arguments
        arguments = function.get("arguments", {})
        argument_text: str
        if isinstance(arguments, str):
            if len(arguments) > _MAX_ARGUMENT_CHARS:
                if mode == "strict":
                    raise InvalidToolCall("tool-call arguments exceed the configured limit")
                _LOG.warning("Tool-call arguments exceed size limit; skipping")
                continue
            try:
                parsed_arguments = json.loads(arguments)
            except json.JSONDecodeError:
                try:
                    parsed_arguments = json.loads(_repair_json_string(arguments))
                except json.JSONDecodeError as exc:
                    if mode == "strict":
                        raise InvalidToolCall("tool-call arguments are not valid JSON") from exc
                    _LOG.warning("Tool-call arguments are not valid JSON; skipping")
                    continue
            if not isinstance(parsed_arguments, dict):
                if mode == "strict":
                    raise InvalidToolCall("tool-call arguments must decode to a JSON object")
                _LOG.warning("Tool-call arguments do not decode to a JSON object; skipping")
                continue
            argument_text = json.dumps(parsed_arguments, ensure_ascii=False, separators=(",", ":"))
            if len(argument_text) > _MAX_ARGUMENT_CHARS:
                if mode == "strict":
                    raise InvalidToolCall("tool-call arguments exceed the configured limit")
                _LOG.warning("Tool-call arguments exceed size limit; skipping")
                continue
        elif isinstance(arguments, dict):
            argument_text = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            if len(argument_text) > _MAX_ARGUMENT_CHARS:
                if mode == "strict":
                    raise InvalidToolCall("tool-call arguments exceed the configured limit")
                _LOG.warning("Tool-call arguments exceed size limit; skipping")
                continue
        elif arguments is None:
            argument_text = "{}"
        else:
            if mode == "strict":
                raise InvalidToolCall("tool-call arguments must be a JSON object or object string")
            _LOG.warning("Tool-call arguments invalid type; skipping")
            continue

        call_id = payload.get("id") or make_id()
        if not isinstance(call_id, str) or not call_id.strip() or len(call_id) > 128:
            if mode == "strict":
                raise InvalidToolCall("tool-call id is invalid")
            call_id = make_id()

        calls.append(
            {
                "index": len(calls),
                "id": str(call_id).strip(),
                "type": "function",
                "function": {"name": clean_name, "arguments": argument_text},
            }
        )
        spans.append((tag_start, tag_end))

    if not calls:
        if has_tag and allowed_tool_names and mode == "strict":
            raise InvalidToolCall("model emitted a malformed tool-call block")
        return ParsedAssistantOutput(text=text.strip())

    cleaned = text
    for start, end in reversed(spans):
        cleaned = cleaned[:start] + cleaned[end:]
    return ParsedAssistantOutput(text=cleaned.strip(), tool_calls=tuple(calls))
