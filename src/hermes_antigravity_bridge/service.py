"""Transport-neutral Hermes chat-completion orchestration."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from .contracts import ChatCompletionResult, TextBackend
from .errors import InvalidRequest
from .integrations.hermes import HermesPromptBuilder
from .prompt.budget import PromptBudget
from .tool_calls import parse_tool_calls

_LOG = logging.getLogger(__name__)
_ALLOWED_FIELDS = {
    "model",
    "messages",
    "tools",
    "stream",
    "max_tokens",
    "temperature",
    "top_p",
    "stop",
    "tool_choice",
    "n",
    "user",
    "reasoning_effort",
    "stream_options",
    "response_format",
}
_ALLOWED_ROLES = {"system", "developer", "user", "assistant", "tool"}


class ChatCompletionService:
    def __init__(
        self,
        *,
        backend: TextBackend,
        prompt_builder: HermesPromptBuilder,
        prompt_budget: PromptBudget,
    ) -> None:
        self.backend = backend
        self.prompt_builder = prompt_builder
        self.prompt_budget = prompt_budget

    def _validate_messages(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise InvalidRequest("messages must be a non-empty array")
        messages: list[dict[str, Any]] = []
        has_user = False
        for index, message in enumerate(value):
            if not isinstance(message, dict):
                raise InvalidRequest(f"message {index} must be an object")
            role = message.get("role")
            if not isinstance(role, str) or role not in _ALLOWED_ROLES:
                raise InvalidRequest(f"message {index} has an unsupported role")
            content = message.get("content", "")
            if content is not None and not isinstance(content, (str, list)):
                raise InvalidRequest(f"message {index} content must be text or a content array")
            if role == "user":
                has_user = True
            messages.append(dict(message))
        if not has_user:
            raise InvalidRequest("messages must contain at least one user message")
        return messages

    def _validate_tools(self, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise InvalidRequest("tools must be an array")
        tools: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, tool in enumerate(value):
            if not isinstance(tool, dict) or tool.get("type", "function") != "function":
                raise InvalidRequest(f"tool {index} must be a function object")
            function = tool.get("function")
            if not isinstance(function, dict):
                raise InvalidRequest(f"tool {index} is missing its function object")
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                raise InvalidRequest(f"tool {index} is missing a function name")
            if name in seen:
                raise InvalidRequest(f"duplicate tool name: {name}")
            parameters = function.get("parameters", {"type": "object"})
            if not isinstance(parameters, dict):
                raise InvalidRequest(f"tool {index} parameters must be an object")
            seen.add(name)
            tools.append(dict(tool))
        return tools

    def validate_request(self, body: Any) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        if not isinstance(body, dict):
            raise InvalidRequest("request body must be a JSON object")
        unsupported = sorted(set(body) - _ALLOWED_FIELDS)
        if unsupported:
            raise InvalidRequest("unsupported request fields: " + ", ".join(unsupported))
        if body.get("stream", False) not in (True, False):
            raise InvalidRequest("stream must be a boolean")
        stream_options = body.get("stream_options")
        if stream_options is not None:
            if not isinstance(stream_options, dict):
                raise InvalidRequest("stream_options must be an object")
            unsupported_stream = sorted(set(stream_options) - {"include_usage"})
            if unsupported_stream:
                raise InvalidRequest(
                    "unsupported stream_options fields: " + ", ".join(unsupported_stream)
                )
            if stream_options.get("include_usage", False) not in (True, False):
                raise InvalidRequest("stream_options.include_usage must be a boolean")
        response_format = body.get("response_format")
        if response_format is not None and not isinstance(response_format, dict):
            raise InvalidRequest("response_format must be an object")
        reasoning_effort = body.get("reasoning_effort")
        if reasoning_effort is not None and (
            not isinstance(reasoning_effort, str)
            or reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
        ):
            raise InvalidRequest("reasoning_effort is invalid")
        if body.get("n", 1) not in (None, 1):
            raise InvalidRequest("only n=1 is supported")
        max_tokens = body.get("max_tokens")
        if max_tokens is not None and (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens < 1
        ):
            raise InvalidRequest("max_tokens must be a positive integer")
        requested = body.get("model")
        if not isinstance(requested, str) or not requested.strip():
            raise InvalidRequest("model must be a non-empty string")
        messages = self._validate_messages(body.get("messages"))
        tools = self._validate_tools(body.get("tools"))
        return requested.strip(), messages, tools

    def complete(self, body: Any) -> ChatCompletionResult:
        requested, messages, tools = self.validate_request(body)
        actual_model = self.backend.resolve_model(requested)
        max_chars = self.prompt_budget.effective_chars(
            actual_model,
            body.get("max_tokens") if isinstance(body, dict) else None,
        )
        prompt = self.prompt_builder.build(
            messages,
            tools=tools or None,
            max_chars=max_chars,
        )
        _LOG.info(
            "context model=%s messages=%d prompt_chars=%d limit=%d",
            actual_model,
            len(messages),
            len(prompt),
            max_chars,
        )
        backend_result = self.backend.generate(prompt, actual_model)
        allowed_names = {
            str(tool["function"]["name"])
            for tool in tools
            if isinstance(tool.get("function"), dict)
        }
        parsed = parse_tool_calls(
            backend_result.response,
            allowed_tool_names=allowed_names,
        )
        usage = backend_result.usage
        normalized_usage = {
            "prompt_tokens": int(usage.get("input_tokens", 0) or 0),
            "completion_tokens": int(usage.get("output_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
        if not parsed.text and not parsed.tool_calls:
            raise InvalidRequest("backend produced neither assistant text nor tool calls")
        return ChatCompletionResult(
            text=parsed.text,
            tool_calls=parsed.tool_calls,
            usage=normalized_usage,
            requested_model=requested,
            actual_model=actual_model,
        )

    def complete_stream(self, body: Any) -> Iterator[dict[str, Any]]:
        requested, messages, tools = self.validate_request(body)
        actual_model = self.backend.resolve_model(requested)
        max_chars = self.prompt_budget.effective_chars(
            actual_model,
            body.get("max_tokens") if isinstance(body, dict) else None,
        )
        prompt = self.prompt_builder.build(
            messages,
            tools=tools or None,
            max_chars=max_chars,
        )
        _LOG.info(
            "context stream model=%s messages=%d prompt_chars=%d limit=%d",
            actual_model,
            len(messages),
            len(prompt),
            max_chars,
        )
        allowed_names = {
            str(tool["function"]["name"])
            for tool in tools
            if isinstance(tool.get("function"), dict)
        }

        if hasattr(self.backend, "generate_stream"):
            stream_gen = self.backend.generate_stream(prompt, actual_model)
            accumulated_text = ""
            has_tool_call_start = False
            for event in stream_gen:
                etype = event.get("type")
                if etype == "delta":
                    text = str(event.get("content", ""))
                    accumulated_text += text
                    if "<tool_call" in accumulated_text:
                        has_tool_call_start = True
                    elif not has_tool_call_start:
                        yield {
                            "type": "delta",
                            "content": text,
                            "requested_model": requested,
                            "actual_model": actual_model,
                        }
                elif etype == "result":
                    backend_response = event["response"]
                    parsed = parse_tool_calls(
                        backend_response.response,
                        allowed_tool_names=allowed_names,
                    )
                    usage = backend_response.usage
                    normalized_usage = {
                        "prompt_tokens": int(usage.get("input_tokens", 0) or 0),
                        "completion_tokens": int(usage.get("output_tokens", 0) or 0),
                        "total_tokens": int(usage.get("total_tokens", 0) or 0),
                    }
                    if parsed.tool_calls:
                        yield {
                            "type": "tool_calls",
                            "tool_calls": parsed.tool_calls,
                            "requested_model": requested,
                            "actual_model": actual_model,
                        }
                        yield {
                            "type": "finish",
                            "finish_reason": "tool_calls",
                            "usage": normalized_usage,
                            "requested_model": requested,
                            "actual_model": actual_model,
                        }
                    else:
                        if has_tool_call_start and parsed.text:
                            yield {
                                "type": "delta",
                                "content": parsed.text,
                                "requested_model": requested,
                                "actual_model": actual_model,
                            }
                        yield {
                            "type": "finish",
                            "finish_reason": "stop",
                            "usage": normalized_usage,
                            "requested_model": requested,
                            "actual_model": actual_model,
                        }
                    return
        else:
            backend_result = self.backend.generate(prompt, actual_model)
            parsed = parse_tool_calls(
                backend_result.response,
                allowed_tool_names=allowed_names,
            )
            usage = backend_result.usage
            normalized_usage = {
                "prompt_tokens": int(usage.get("input_tokens", 0) or 0),
                "completion_tokens": int(usage.get("output_tokens", 0) or 0),
                "total_tokens": int(usage.get("total_tokens", 0) or 0),
            }
            if parsed.tool_calls:
                yield {
                    "type": "tool_calls",
                    "tool_calls": parsed.tool_calls,
                    "requested_model": requested,
                    "actual_model": actual_model,
                }
                yield {
                    "type": "finish",
                    "finish_reason": "tool_calls",
                    "usage": normalized_usage,
                    "requested_model": requested,
                    "actual_model": actual_model,
                }
            else:
                yield {
                    "type": "delta",
                    "content": parsed.text,
                    "requested_model": requested,
                    "actual_model": actual_model,
                }
                yield {
                    "type": "finish",
                    "finish_reason": "stop",
                    "usage": normalized_usage,
                    "requested_model": requested,
                    "actual_model": actual_model,
                }

    def list_models(self, *, force_refresh: bool = False) -> tuple[str, ...]:
        return self.backend.list_models(force_refresh=force_refresh)

    def readiness(self) -> dict[str, Any]:
        return self.backend.readiness()
