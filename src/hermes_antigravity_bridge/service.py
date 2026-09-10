"""Transport-neutral Hermes chat-completion orchestration."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from .contracts import ChatCompletionResult, TextBackend
from .errors import InvalidRequest, InvalidToolCall
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
    "max_completion_tokens",
    "temperature",
    "top_p",
    "stop",
    "tool_choice",
    "parallel_tool_calls",
    "seed",
    "presence_penalty",
    "frequency_penalty",
    "n",
    "user",
    "reasoning_effort",
    "stream_options",
    "response_format",
}
_ALLOWED_ROLES = {"system", "developer", "user", "assistant", "tool"}


def _extract_unstreamed_text(full_text: str, streamed_text: str) -> str:
    full_stripped = full_text.strip()
    streamed_stripped = streamed_text.strip()
    if not streamed_stripped:
        return full_text
    if full_stripped == streamed_stripped:
        return ""
    if full_stripped.startswith(streamed_stripped):
        remainder = full_stripped[len(streamed_stripped):]
        streamed_trailing = streamed_text[len(streamed_text.rstrip()):]
        if streamed_trailing and remainder.startswith(streamed_trailing):
            remainder = remainder[len(streamed_trailing):]
        return remainder
    return full_text.removeprefix(streamed_text)


class ChatCompletionService:
    def __init__(
        self,
        *,
        backend: TextBackend,
        prompt_builder: HermesPromptBuilder,
        prompt_budget: PromptBudget,
        tool_call_mode: str = "compatible",
    ) -> None:
        self.backend = backend
        self.prompt_builder = prompt_builder
        self.prompt_budget = prompt_budget
        self.tool_call_mode = tool_call_mode

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

    def validate_request(
        self, body: Any
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], str | None]:
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
        for token_field in ("max_tokens", "max_completion_tokens"):
            token_val = body.get(token_field)
            if token_val is not None and (
                isinstance(token_val, bool)
                or not isinstance(token_val, int)
                or token_val < 1
            ):
                raise InvalidRequest(f"{token_field} must be a positive integer")

        seed = body.get("seed")
        if seed is not None:
            if isinstance(seed, bool) or not isinstance(seed, int):
                raise InvalidRequest("seed must be an integer")
            _LOG.debug("request specified seed=%d (accepted for OpenAI compatibility)", seed)

        parallel_tool_calls = body.get("parallel_tool_calls")
        if parallel_tool_calls is not None:
            if not isinstance(parallel_tool_calls, bool):
                raise InvalidRequest("parallel_tool_calls must be a boolean")
            _LOG.debug("request specified parallel_tool_calls=%s", parallel_tool_calls)

        for penalty_name in ("presence_penalty", "frequency_penalty"):
            penalty_val = body.get(penalty_name)
            if penalty_val is not None:
                if (
                    isinstance(penalty_val, bool)
                    or not isinstance(penalty_val, (int, float))
                    or not (-2.0 <= penalty_val <= 2.0)
                ):
                    raise InvalidRequest(f"{penalty_name} must be a number between -2.0 and 2.0")
                _LOG.debug(
                    "request specified %s=%s (accepted for OpenAI compatibility)",
                    penalty_name,
                    penalty_val,
                )

        requested = body.get("model")
        if not isinstance(requested, str) or not requested.strip():
            raise InvalidRequest("model must be a non-empty string")
        messages = self._validate_messages(body.get("messages"))
        tools = self._validate_tools(body.get("tools"))
        return requested.strip(), messages, tools, reasoning_effort

    def complete(self, body: Any) -> ChatCompletionResult:
        requested, messages, tools, reasoning_effort = self.validate_request(body)
        actual_model = self.backend.resolve_model(requested)
        max_chars = self.prompt_budget.effective_chars(
            actual_model,
            (body.get("max_completion_tokens") or body.get("max_tokens")) if isinstance(body, dict) else None,
        )
        prompt = self.prompt_builder.build(
            messages,
            tools=tools or None,
            max_chars=max_chars,
        )
        _LOG.info(
            "context model=%s effort=%s messages=%d prompt_chars=%d limit=%d",
            actual_model,
            reasoning_effort,
            len(messages),
            len(prompt),
            max_chars,
        )
        if reasoning_effort is not None:
            kwargs = {"effort": reasoning_effort}
            try:
                backend_result = self.backend.generate(prompt, actual_model, **kwargs)
            except TypeError as exc:
                msg = str(exc).lower()
                if "effort" in msg or "unexpected keyword argument" in msg:
                    backend_result = self.backend.generate(prompt, actual_model)
                else:
                    raise
        else:
            backend_result = self.backend.generate(prompt, actual_model)
        allowed_names = {
            str(tool["function"]["name"])
            for tool in tools
            if isinstance(tool.get("function"), dict)
        }
        parsed = parse_tool_calls(
            backend_result.response,
            allowed_tool_names=allowed_names,
            mode=self.tool_call_mode,
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
        requested, messages, tools, reasoning_effort = self.validate_request(body)
        actual_model = self.backend.resolve_model(requested)
        max_chars = self.prompt_budget.effective_chars(
            actual_model,
            (body.get("max_completion_tokens") or body.get("max_tokens")) if isinstance(body, dict) else None,
        )
        prompt = self.prompt_builder.build(
            messages,
            tools=tools or None,
            max_chars=max_chars,
        )
        _LOG.info(
            "context stream model=%s effort=%s messages=%d prompt_chars=%d limit=%d",
            actual_model,
            reasoning_effort,
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
            if reasoning_effort is not None:
                kwargs = {"effort": reasoning_effort}
                try:
                    stream_gen = self.backend.generate_stream(prompt, actual_model, **kwargs)
                except TypeError as exc:
                    msg = str(exc).lower()
                    if "effort" in msg or "unexpected keyword argument" in msg:
                        stream_gen = self.backend.generate_stream(prompt, actual_model)
                    else:
                        raise
            else:
                stream_gen = self.backend.generate_stream(prompt, actual_model)
            accumulated_text = ""
            streamed_text = ""
            has_tool_call_start = False
            for event in stream_gen:
                etype = event.get("type")
                if etype == "delta":
                    text = str(event.get("content", ""))
                    accumulated_text += text
                    if "<tool_call" in accumulated_text.lower():
                        has_tool_call_start = True
                    elif not has_tool_call_start:
                        streamed_text += text
                        yield {
                            "type": "delta",
                            "content": text,
                            "requested_model": requested,
                            "actual_model": actual_model,
                        }
                elif etype == "result":
                    backend_response = event["response"]
                    try:
                        parsed = parse_tool_calls(
                            backend_response.response,
                            allowed_tool_names=allowed_names,
                            mode=self.tool_call_mode,
                        )
                    except InvalidToolCall as exc:
                        _LOG.warning("tool call parse failed in stream: %s; degrading to text", exc)
                        if has_tool_call_start:
                            raw = backend_response.response
                            unstreamed = _extract_unstreamed_text(raw, streamed_text)
                            if unstreamed:
                                yield {
                                    "type": "delta",
                                    "content": unstreamed,
                                    "requested_model": requested,
                                    "actual_model": actual_model,
                                }
                        yield {
                            "type": "finish",
                            "finish_reason": "stop",
                            "x_bridge_error": {
                                "type": "tool_call_parse_error",
                                "message": str(exc),
                            },
                            "usage": {
                                "prompt_tokens": int(backend_response.usage.get("input_tokens", 0) or 0),
                                "completion_tokens": int(backend_response.usage.get("output_tokens", 0) or 0),
                                "total_tokens": int(backend_response.usage.get("total_tokens", 0) or 0),
                            },
                            "requested_model": requested,
                            "actual_model": actual_model,
                        }
                        return

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
                            unstreamed = _extract_unstreamed_text(parsed.text, streamed_text)
                            if unstreamed:
                                yield {
                                    "type": "delta",
                                    "content": unstreamed,
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
            if reasoning_effort is not None:
                try:
                    backend_result = self.backend.generate(prompt, actual_model, effort=reasoning_effort)
                except TypeError as exc:
                    msg = str(exc).lower()
                    if "effort" in msg or "unexpected keyword argument" in msg:
                        backend_result = self.backend.generate(prompt, actual_model)
                    else:
                        raise
            else:
                backend_result = self.backend.generate(prompt, actual_model)
            parsed = parse_tool_calls(
                backend_result.response,
                allowed_tool_names=allowed_names,
                mode=self.tool_call_mode,
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
