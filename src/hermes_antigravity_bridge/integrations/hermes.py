"""Hermes-owned context policy and deterministic prompt builder."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..errors import InvalidRequest, PromptTooLarge
from ..prompt.primitives import (
    clip_head_tail,
    compact_non_latest_text,
    compact_tool_section,
    extract_attached_media,
    format_user_uploaded_media_header,
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
# Multimodal Media & Vision Rule
Google Antigravity's native file-reading capability (read_file / view_file) IS permitted for internal execution when the user uploads or attaches media files (images, screenshots, video frames, audio clips, or documents).
When the user uploads or asks about an image, screenshot, or video frame, inspect its visual contents directly using native multimodal vision. NEVER attempt to invoke terminal commands, shell scripts, or RunCommand to search or inspect media files.
"""
_SYSTEM_LABEL = "\n# HERMES_SYSTEM_INSTRUCTIONS\n"
_TOOLS_LABEL = "\n# HERMES_TOOL_SCHEMAS_JSONL\n"
_HISTORY_LABEL = "\n# RECENT_CONVERSATION_JSONL\n"
_LATEST_LABEL = "\n# CURRENT_USER_REQUEST_JSON\n"
_TOOL_OUTPUT_LABEL = "\n# LATEST_TOOL_OUTPUT\n"
_FINAL_DIRECTIVE = (
    "\n# CURRENT_REQUEST_GUARD\n"
    "Answer CURRENT_USER_REQUEST_JSON, using the system instructions, memory, and recent context above."
)
_TOOL_ISOLATION_FINAL_DIRECTIVE = (
    "\n# CURRENT_REQUEST_GUARD\n"
    "Answer CURRENT_USER_REQUEST_JSON, using the system instructions, memory, and recent context above.\n"
    "CRITICAL: Do not invoke internal CLI tools directly. If a tool is needed, output <tool_call> blocks strictly as text.\n"
    "For user uploaded images or video frames, inspect the visual media directly using native vision without terminal commands.\n"
    "For image requests, use Google Antigravity's native generate_image tool directly rather than writing Python scripts."
)
_TOOL_FINAL_DIRECTIVE = (
    "\n# CURRENT_REQUEST_GUARD\n"
    "Hermes has executed the requested tool(s) and returned the result above. "
    "Synthesize the tool result above to formulate the next step or final answer for the user."
)
_TOOL_ISOLATION_TOOL_FINAL_DIRECTIVE = (
    "\n# CURRENT_REQUEST_GUARD\n"
    "Hermes has executed the requested tool(s) and returned the result above. "
    "Synthesize the tool result above to formulate the next step or final answer for the user.\n"
    "CRITICAL: Do not invoke internal CLI tools directly. If a tool is needed, output <tool_call> blocks strictly as text.\n"
    "For user uploaded images or video frames, inspect the visual media directly using native vision without terminal commands.\n"
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

        is_tool_turn = str(messages[-1].get("role") or "").lower() == "tool"
        if is_tool_turn:
            latest_indices: list[int] = []
            for index in range(len(messages) - 1, -1, -1):
                if str(messages[index].get("role") or "").lower() == "tool":
                    latest_indices.append(index)
                else:
                    break
            latest_indices.reverse()
            latest_label = _TOOL_OUTPUT_LABEL
            final_directive = (
                _TOOL_ISOLATION_TOOL_FINAL_DIRECTIVE
                if self.enforce_tool_isolation
                else _TOOL_FINAL_DIRECTIVE
            )
            latest_json = "\n".join(
                serialize_history_message(messages[idx], compact_content=False)
                for idx in latest_indices
            )
            excluded_indices: set[int] = set(latest_indices)
        else:
            latest_user_index = next(
                (
                    index
                    for index in range(len(messages) - 1, -1, -1)
                    if str(messages[index].get("role") or "").lower() == "user"
                ),
                len(messages) - 1,
            )
            latest_label = _LATEST_LABEL
            final_directive = (
                _TOOL_ISOLATION_FINAL_DIRECTIVE
                if self.enforce_tool_isolation
                else _FINAL_DIRECTIVE
            )
            latest_message = messages[latest_user_index]
            latest_json = serialize_history_message(latest_message, compact_content=False)
            excluded_indices = {latest_user_index}

        preamble = _TOOL_ISOLATION_PREAMBLE if self.enforce_tool_isolation else _PREAMBLE
        media_items = extract_attached_media(messages)
        media_header = format_user_uploaded_media_header(media_items)
        media_prefix = f"{media_header}\n\n" if media_header else ""
        fixed_cost = sum(
            len(part)
            for part in (
                media_prefix,
                preamble,
                _SYSTEM_LABEL,
                _TOOLS_LABEL,
                _HISTORY_LABEL,
                latest_label,
                latest_json,
                final_directive,
            )
        )
        if fixed_cost > max_chars:
            msg = (
                "latest tool output exceeds the configured bridge prompt budget; refusing to drop it"
                if is_tool_turn
                else "latest user message exceeds the configured bridge prompt budget; refusing to drop it"
            )
            raise PromptTooLarge(msg)
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
                msg = (
                    "latest tool output leaves insufficient budget for required system context"
                    if is_tool_turn
                    else "latest user message leaves insufficient budget for required system context"
                )
                raise PromptTooLarge(msg)

        candidate_indices = [
            index
            for index, message in enumerate(messages)
            if index not in excluded_indices
            and str(message.get("role") or "").lower() not in {"system", "developer"}
        ]
        candidates = [messages[index] for index in candidate_indices]
        if candidates:
            tool_indices_in_messages = [
                i for i, m in enumerate(messages)
                if str(m.get("role") or "").lower() == "tool"
            ]
            recent_tool_indices = set(tool_indices_in_messages[-2:])
            all_blocks = [
                serialize_history_message(
                    messages[idx],
                    is_older_tool=(
                        str(messages[idx].get("role") or "").lower() == "tool"
                        and idx not in recent_tool_indices
                    ),
                )
                for idx in candidate_indices
            ]
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
        history_text = recent_history(messages, excluded_indices, history_budget)
        prompt = (
            media_prefix
            + preamble
            + _SYSTEM_LABEL
            + system_text
            + _TOOLS_LABEL
            + tools_text
            + _HISTORY_LABEL
            + history_text
            + latest_label
            + latest_json
            + final_directive
        )
        if len(prompt) > max_chars or latest_json not in prompt:
            raise RuntimeError("bridge context invariant failed: latest request was not preserved")
        return prompt
