"""Deterministic, structure-safe prompt serialization helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

_LOW_ENTROPY_RUN_RE = re.compile(r"([^\s])\1{511,}")


def compact_non_latest_text(text: str) -> str:
    """Compact pathological repeated-character runs outside the latest request."""
    def replacement(match: re.Match[str]) -> str:
        character = match.group(1)
        return (
            "[bridge compacted repeated character "
            f"U+{ord(character):04X} x {len(match.group(0))}]"
        )

    return _LOW_ENTROPY_RUN_RE.sub(replacement, text)


def text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in {"text", "input_text"}:
                    parts.append(str(item.get("text", "")))
                elif item.get("type") in {"image_url", "input_image"}:
                    parts.append("[image omitted from Antigravity text bridge]")
        return "\n".join(part for part in parts if part)
    return str(value or "")


def clip_head_tail(text: str, limit: int, marker: str) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker_text = f"\n[{marker}]\n"
    if limit <= len(marker_text) + 2:
        return marker_text[:limit]
    payload = limit - len(marker_text)
    head = (payload + 1) // 2
    tail = payload - head
    return text[:head] + marker_text + (text[-tail:] if tail else "")


def compact_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [compact_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    ignored = {"description", "title", "examples", "$comment"}
    return {
        key: compact_schema(item)
        for key, item in value.items()
        if key not in ignored
    }


def compact_tool_section(
    tools: Sequence[dict[str, Any]] | None,
    budget: int,
    *,
    max_description_chars: int = 240,
) -> str:
    if not tools or budget <= 0:
        return ""
    normalized: list[tuple[int, int, dict[str, Any]]] = []
    for index, tool in enumerate(tools):
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        name = str(fn["name"])
        description = str(fn.get("description") or "")[:max_description_chars]
        item = {
            "name": name,
            "description": description,
            "parameters": compact_schema(fn.get("parameters") or {"type": "object"}),
        }
        normalized.append((0 if name == "memory" else 1, index, item))

    lines: list[str] = []
    used = 0
    for _priority, _index, item in sorted(normalized, key=lambda row: (row[0], row[1])):
        line = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        cost = len(line) + (1 if lines else 0)
        if used + cost <= budget:
            lines.append(line)
            used += cost
    omitted = len(normalized) - len(lines)
    if omitted:
        note = f"\n[tool schemas omitted by bridge budget: {omitted}]"
        if used + len(note) <= budget:
            lines.append(note.lstrip("\n"))
    return "\n".join(lines)


def message_text(message: dict[str, Any]) -> str:
    content = text_content(message.get("content", ""))
    calls = message.get("tool_calls")
    if calls:
        summaries: list[str] = []
        for call in calls if isinstance(calls, list) else []:
            if not isinstance(call, dict):
                continue
            fn_value = call.get("function")
            fn = fn_value if isinstance(fn_value, dict) else {}
            summaries.append(f"{fn.get('name', 'tool')}({fn.get('arguments', '')})")
        if summaries:
            call_text = "[assistant tool calls: " + ", ".join(summaries) + "]"
            content = f"{content}\n{call_text}".strip() if content else call_text
    return content


def serialize_history_message(
    message: dict[str, Any],
    limit: int | None = None,
    *,
    compact_content: bool = True,
) -> str:
    content = message_text(message)
    if compact_content:
        content = compact_non_latest_text(content)
    payload: dict[str, Any] = {
        "role": str(message.get("role") or "user"),
        "content": content,
    }
    if message.get("tool_name") or message.get("name"):
        payload["tool_name"] = str(message.get("tool_name") or message.get("name"))
    if message.get("tool_call_id"):
        payload["tool_call_id"] = str(message["tool_call_id"])
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if limit is None or len(serialized) <= limit:
        return serialized
    if limit < 160:
        return ""
    empty_payload = dict(payload)
    empty_payload["content"] = ""
    overhead = len(json.dumps(empty_payload, ensure_ascii=False, separators=(",", ":")))
    clipped_budget = limit - overhead - 8
    if clipped_budget <= 0:
        return ""
    payload["content"] = clip_head_tail(
        str(payload["content"]), clipped_budget, "message content truncated by bridge"
    )
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return serialized if len(serialized) <= limit else ""


def recent_history(
    messages: Sequence[dict[str, Any]], latest_user_index: int, budget: int
) -> str:
    if budget <= 0:
        return ""
    candidates = [
        message
        for index, message in enumerate(messages)
        if index != latest_user_index
        and str(message.get("role") or "").lower() not in {"system", "developer"}
    ]
    if not candidates:
        return ""
    marker = json.dumps(
        {"role": "bridge", "content": "[Earlier conversation omitted to keep within context limit]"},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    marker_len = len(marker)
    all_blocks = [serialize_history_message(message) for message in candidates]
    all_cost = sum(len(block) for block in all_blocks) + max(0, len(all_blocks) - 1)
    if all_cost <= budget:
        return "\n".join(all_blocks)
    remaining = budget - marker_len - 1
    if remaining <= 0:
        return marker if marker_len <= budget else ""
    selected: list[str] = []
    for message in reversed(candidates):
        block = serialize_history_message(message)
        cost = len(block) + (1 if selected else 0)
        if cost <= remaining:
            selected.append(block)
            remaining -= cost
            continue
        if not selected:
            clipped = serialize_history_message(message, remaining)
            if clipped:
                selected.append(clipped)
                remaining -= len(clipped)
        break
    if selected:
        selected.append(marker)
        selected.reverse()
        return "\n".join(selected)
    if marker_len <= budget:
        return marker
    return ""
