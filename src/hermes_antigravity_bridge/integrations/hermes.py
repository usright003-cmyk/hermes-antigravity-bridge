"""Hermes-owned context policy and deterministic prompt builder."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..errors import InvalidRequest, PromptTooLarge
from ..prompt.primitives import (
    clip_head_tail,
    compact_non_latest_text,
    compact_tool_section,
    recent_history,
    serialize_history_message,
    text_content,
)

_PREAMBLE = """You are the model inside an existing Hermes Agent session.
Hermes—not this bridge—owns memory, tools, session identity, and conversation history.
Treat HERMES_SYSTEM_INSTRUCTIONS as authoritative.
Use RECENT_CONVERSATION_JSONL only as ordered context.
The final CURRENT_USER_REQUEST_JSON is the current request and overrides older topics.
Never answer an older request merely because it is longer or more detailed.
# Tool Call Rules
When a listed Hermes tool is required, emit exactly one or more blocks in this form:
<tool_call>{"id":"call_unique","type":"function","function":{"name":"TOOL_NAME","arguments":{}}}</tool_call>
When no tool is required, answer normally and emit no <tool_call> tag.
"""
_TOOL_ISOLATION_PREAMBLE = """You are the model inside an existing Hermes Agent session.
Hermes—not this bridge—owns memory, tools, session identity, and conversation history.
Treat HERMES_SYSTEM_INSTRUCTIONS as authoritative.
Use RECENT_CONVERSATION_JSONL only as ordered context.
The final CURRENT_USER_REQUEST_JSON is the current request and overrides older topics.
Never answer an older request merely because it is longer or more detailed.
# Tool Call Rules
When a listed Hermes tool is required, emit exactly one or more blocks in this form:
<tool_call>{"id":"call_unique","type":"function","function":{"name":"TOOL_NAME","arguments":{}}}</tool_call>
When no tool is required, answer normally and emit no <tool_call> tag.
# Tool Isolation Rules
You MUST NEVER attempt to invoke internal tools, functions, or agent capabilities directly (such as RunCommand, run_command, ViewFile, view_file, write_to_file, read_file, edit_file, browse, terminal, etc.).
Any direct internal tool invocation will be rejected and denied by strict security policy.
All tool operations MUST be emitted strictly as text output inside <tool_call>...</tool_call> tags for the Hermes client to execute.
# Image Generation Rule
Google Antigravity's native safe image generation capability (generate_image) IS permitted for internal execution when the user requests generating, drawing, or creating an image.
If the user asks for an image, invoke generate_image(prompt=...) or output generate_image tool calls.
NEVER write Python scripts (such as PIL, pillow, or matplotlib) in terminal to draw or render images when an image is requested.
"""
_SYSTEM_LABEL = "\n# HERMES_SYSTEM_INSTRUCTIONS\n"
_TOOLS_LABEL = "\n# HERMES_TOOL_SCHEMAS_JSONL\n"
_HISTORY_LABEL = "\n# RECENT_CONVERSATION_JSONL\n"
_LATEST_LABEL = "\n# CURRENT_USER_REQUEST_JSON\n"
_FINAL_DIRECTIVE = (
    "\n# CURRENT_REQUEST_GUARD\n"
    "Answer CURRENT_USER_REQUEST_JSON, using the system instructions, memory, and recent context above."
)
_TOOL_ISOLATION_FINAL_DIRECTIVE = (
    "\n# CURRENT_REQUEST_GUARD\n"
    "Answer CURRENT_USER_REQUEST_JSON, using the system instructions, memory, and recent context above.\n"
    "CRITICAL: Do not invoke internal CLI tools directly. If a tool is needed, output <tool_call> blocks strictly as text.\n"
    "For image requests, use Google Antigravity's native generate_image tool directly rather than writing Python scripts."
)


class HermesPromptBuilder:
    """Serialize Hermes context without taking ownership of memory or history."""

    def __init__(
        self,
        *,
        max_tool_description_chars: int = 240,
        enforce_tool_isolation: bool = False,
    ) -> None:
        self.max_tool_description_chars = max_tool_description_chars
        self.enforce_tool_isolation = enforce_tool_isolation

    def build(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        max_chars: int = 4_000_000,
    ) -> str:
        if not messages:
            raise InvalidRequest("messages must be a non-empty array")
        if max_chars < 4_096:
            raise InvalidRequest("prompt budget must be at least 4096 characters")

        latest_user_index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if str(messages[index].get("role") or "").lower() == "user"
            ),
            len(messages) - 1,
        )
        preamble = _TOOL_ISOLATION_PREAMBLE if self.enforce_tool_isolation else _PREAMBLE
        final_directive = (
            _TOOL_ISOLATION_FINAL_DIRECTIVE
            if self.enforce_tool_isolation
            else _FINAL_DIRECTIVE
        )
        latest_message = messages[latest_user_index]
        latest_json = serialize_history_message(latest_message, compact_content=False)
        fixed_cost = sum(
            len(part)
            for part in (
                preamble,
                _SYSTEM_LABEL,
                _TOOLS_LABEL,
                _HISTORY_LABEL,
                _LATEST_LABEL,
                latest_json,
                final_directive,
            )
        )
        if fixed_cost > max_chars:
            raise PromptTooLarge(
                "latest user message exceeds the configured bridge prompt budget; refusing to drop it"
            )
        available = max_chars - fixed_cost

        system_parts = [
            (
                f"[{str(message.get('role') or 'system').upper()}]\n"
                + compact_non_latest_text(text_content(message.get("content", "")))
            )
            for message in messages
            if str(message.get("role") or "").lower() in {"system", "developer"}
        ]
        raw_system_text = "\n".join(system_parts)
        if system_parts:
            min_system_needed = min(len(raw_system_text), 1_024)
            if available < min_system_needed:
                raise PromptTooLarge(
                    "latest user message leaves insufficient budget for required system context"
                )

        candidates = [
            message
            for index, message in enumerate(messages)
            if index != latest_user_index
            and str(message.get("role") or "").lower() not in {"system", "developer"}
        ]
        if candidates:
            all_blocks = [serialize_history_message(message) for message in candidates]
            history_needed = sum(len(block) for block in all_blocks) + max(0, len(all_blocks) - 1)
            history_reserve = min(history_needed, available // 2)
        else:
            history_reserve = 0

        if tools:
            full_tools = compact_tool_section(
                tools,
                available,
                max_description_chars=self.max_tool_description_chars,
            )
            tools_needed = len(full_tools)
            tools_reserve = min(tools_needed, available // 3)
        else:
            tools_needed = 0
            tools_reserve = 0

        system_budget = max(0, available - history_reserve - tools_reserve)
        system_text = clip_head_tail(
            raw_system_text,
            system_budget,
            "system context truncated; head and memory tail preserved",
        )
        used_system = len(system_text)
        remaining = available - used_system

        if tools:
            tools_budget = max(
                tools_reserve,
                min(tools_needed, remaining - history_reserve),
            )
            tools_text = compact_tool_section(
                tools,
                tools_budget,
                max_description_chars=self.max_tool_description_chars,
            )
        else:
            tools_text = ""

        history_budget = available - used_system - len(tools_text)
        history_text = recent_history(messages, latest_user_index, history_budget)
        prompt = (
            preamble
            + _SYSTEM_LABEL
            + system_text
            + _TOOLS_LABEL
            + tools_text
            + _HISTORY_LABEL
            + history_text
            + _LATEST_LABEL
            + latest_json
            + final_directive
        )
        if len(prompt) > max_chars or latest_json not in prompt:
            raise RuntimeError("bridge context invariant failed: latest request was not preserved")
        return prompt
