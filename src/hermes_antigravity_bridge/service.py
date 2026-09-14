"""Transport-neutral Hermes chat-completion orchestration."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any

from .contracts import ChatCompletionResult, TextBackend
from .errors import InvalidRequest, InvalidToolCall
from .integrations.hermes import HermesPromptBuilder
from .prompt.budget import PromptBudget
from .tool_calls import _repair_json_string, parse_tool_calls

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
    "logit_bias",
    "logprobs",
    "top_logprobs",
    "modalities",
    "metadata",
    "store",
    "service_tier",
    "audio",
    "prediction",
    "web_search_options",
    "extra_body",
    "prompt_cache_key",
    "dimensions",
    "encoding_format",
    "echo",
    "best_of",
    "suffix",
}
_ALLOWED_ROLES = {"system", "developer", "user", "assistant", "tool"}
_THOUGHT_RE = re.compile(r"<thought>(.*?)(?:</thought>|$)", re.DOTALL | re.IGNORECASE)


def _extract_json_schema_instruction(response_format: Any) -> str | None:
    """Extract strict JSON schema/object instruction from response_format if requested."""
    if not isinstance(response_format, dict):
        return None
    rf_type = str(response_format.get("type") or "").strip().lower()
    if rf_type == "json_object":
        return (
            "CRITICAL REQUIREMENT: You must respond with a valid JSON object. "
            "Output ONLY the JSON object without any additional commentary or markdown wrappers."
        )
    if rf_type != "json_schema":
        return None
    js = response_format.get("json_schema")
    schema_def: Any = None
    schema_name = ""
    if isinstance(js, dict):
        schema_def = js.get("schema") or js
        schema_name = str(js.get("name") or "").strip()
    elif "schema" in response_format:
        schema_def = response_format.get("schema")
    if schema_def is not None:
        schema_json = json.dumps(schema_def, indent=2, ensure_ascii=False)
        name_clause = f" for '{schema_name}'" if schema_name else ""
        return (
            f"CRITICAL REQUIREMENT: You must respond with a valid JSON object strictly conforming to this JSON Schema{name_clause}:\n"
            f"```json\n{schema_json}\n```\n"
            "Output ONLY the JSON object without any additional commentary or markdown wrappers."
        )
    return (
        "CRITICAL REQUIREMENT: You must respond with a valid JSON object strictly adhering to the requested JSON schema. "
        "Output ONLY the JSON object."
    )


_TOOL_CALL_PREFIXES = tuple("<tool_call"[:i] for i in range(len("<tool_call") - 1, 0, -1))
_EXTERNAL_IMAGE_TOOL_NAMES = {
    "image_gen",
    "image_generation",
    "generate_image",
    "text_to_image",
}


def _filter_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve advertised tools including image generation so Hermes can dispatch them."""
    return list(tools)


def _extract_unadvertised_image_tool_call(
    text: str, allowed_tool_names: set[str]
) -> tuple[str, str, str | None] | None:
    """Detect if model emitted an image tool call that Hermes did not advertise.

    Returns (tool_name, prompt, aspect_ratio) or None.
    """
    if "<tool_call" not in text.lower():
        return None
    image_tool_names = {"generate_image", "image_gen", "image_generation", "text_to_image", "draw_image"}
    if any(name.lower() in allowed_tool_names for name in image_tool_names):
        return None

    tag_start = re.compile(r"<tool_call(?:\s+[^>]*)?>", re.IGNORECASE)
    tag_end = re.compile(r"</tool_call\s*>", re.IGNORECASE)
    for start_match in tag_start.finditer(text):
        end_match = tag_end.search(text, start_match.end())
        if not end_match:
            continue
        block_content = text[start_match.end() : end_match.start()].strip()
        payload = None
        try:
            payload = json.loads(block_content)
        except (json.JSONDecodeError, TypeError):
            try:
                payload = json.loads(_repair_json_string(block_content))
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = None
        if not isinstance(payload, dict):
            continue
        fn = payload.get("function")
        if isinstance(fn, dict):
            fn_name = str(fn.get("name") or "").strip().lower()
            fn_args = fn.get("arguments") or {}
        else:
            fn_name = str(payload.get("name") or "").strip().lower()
            fn_args = payload.get("arguments") or payload.get("parameters") or {}
        for pfx in ("default_api:", "antigravity:", "tools:", "functions."):
            if fn_name.startswith(pfx):
                fn_name = fn_name.removeprefix(pfx)
        if fn_name in image_tool_names:
            if isinstance(fn_args, str):
                try:
                    fn_args = json.loads(fn_args)
                except (json.JSONDecodeError, TypeError):
                    try:
                        fn_args = json.loads(_repair_json_string(fn_args))
                    except (json.JSONDecodeError, TypeError, ValueError):
                        fn_args = {"prompt": fn_args}
            if isinstance(fn_args, dict):
                args_lower = {str(k).lower(): v for k, v in fn_args.items()}
                prompt = str(
                    args_lower.get("prompt")
                    or args_lower.get("description")
                    or args_lower.get("query")
                    or args_lower.get("text")
                    or args_lower.get("caption")
                    or args_lower.get("input")
                    or args_lower.get("prompt_text")
                    or ""
                ).strip()
                if prompt:
                    aspect = str(
                        args_lower.get("aspectratio")
                        or args_lower.get("aspect_ratio")
                        or ""
                    ).strip() or None
                    return fn_name, prompt, aspect
    return None


def _extract_unstreamed_text(full_text: str, streamed_text: str) -> str:
    full_stripped = full_text.strip()
    streamed_stripped = streamed_text.strip()
    if not streamed_stripped:
        return full_text
    if full_stripped == streamed_stripped or streamed_stripped.startswith(full_stripped):
        return ""
    if full_stripped.startswith(streamed_stripped):
        remainder = full_stripped[len(streamed_stripped):]
        streamed_trailing = streamed_text[len(streamed_text.rstrip()):]
        if streamed_trailing and remainder.startswith(streamed_trailing):
            remainder = remainder[len(streamed_trailing):]
        return remainder
    rem = full_text.removeprefix(streamed_text)
    if rem != full_text:
        return rem
    return ""


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

        for opt_field in (
            "logit_bias",
            "logprobs",
            "top_logprobs",
            "modalities",
            "metadata",
            "store",
            "service_tier",
            "audio",
            "prediction",
            "web_search_options",
            "user",
            "extra_body",
            "prompt_cache_key",
            "dimensions",
            "encoding_format",
            "echo",
            "best_of",
            "suffix",
        ):
            if opt_field in body:
                _LOG.debug(
                    "request specified %s (accepted and ignored for OpenAI compatibility)",
                    opt_field,
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
        filtered_tools = _filter_tools(tools)
        schema_instruction = _extract_json_schema_instruction(
            body.get("response_format") if isinstance(body, dict) else None
        )
        prompt_messages = list(messages)
        if schema_instruction:
            prompt_messages.append({"role": "system", "content": schema_instruction})
        if filtered_tools:
            tool_isolation_instruction = (
                "CRITICAL TOOL ISOLATION & EXECUTION RULES:\n"
                "You MUST NEVER attempt to invoke internal tools, functions, or agent capabilities directly "
                "(such as RunCommand, run_command, ViewFile, view_file, write_to_file, read_file, edit_file, browse, terminal, etc.). "
                "Any direct internal tool invocation will be rejected and denied by strict security policy. "
                "All tool operations MUST be emitted strictly as text output inside <tool_call>...</tool_call> tags "
                "for the Hermes client to execute:\n"
                '<tool_call>{"id":"call_unique","type":"function","function":{"name":"TOOL_NAME","arguments":{}}}</tool_call>\n'
                "Hermes Agent executes all tools in its host environment and provides the results back in subsequent turns.\n"
                "IMAGE GENERATION EXCEPTION: If the user requests generating, drawing, or creating an image, "
                "Google Antigravity's native image generation capability (generate_image) is permitted for internal execution. "
                "Use generate_image directly to create the image; NEVER write Python PIL/matplotlib scripts in the terminal to draw images."
            )
            prompt_messages.append({"role": "system", "content": tool_isolation_instruction})
        else:
            no_tool_instruction = (
                "CRITICAL TOOL ISOLATION RULE:\n"
                "You MUST NEVER attempt to invoke internal tools, functions, or agent capabilities directly "
                "(such as RunCommand, run_command, ViewFile, view_file, write_to_file, read_file, edit_file, browse, terminal, etc.). "
                "No tools are available for this turn; respond directly in normal text.\n"
                "IMAGE GENERATION EXCEPTION: If the user requests generating, drawing, or creating an image, "
                "Google Antigravity's native image generation capability (generate_image) is permitted for internal execution. "
                "Use generate_image directly to produce the image; NEVER write Python PIL/matplotlib scripts to draw images."
            )
            prompt_messages.append({"role": "system", "content": no_tool_instruction})
        prompt = self.prompt_builder.build(
            prompt_messages,
            tools=filtered_tools or None,
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
            for tool in filtered_tools
            if isinstance(tool.get("function"), dict)
        }
        raw_response = backend_result.response
        thought_matches = [m.strip() for m in _THOUGHT_RE.findall(raw_response) if m.strip()]
        reasoning_content: str | None = "\n\n".join(thought_matches) if thought_matches else None
        cleaned_response = _THOUGHT_RE.sub("", raw_response).strip()

        usage = backend_result.usage
        normalized_usage = {
            "prompt_tokens": int(usage.get("input_tokens", 0) or 0),
            "completion_tokens": int(usage.get("output_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }

        unadvertised_img = _extract_unadvertised_image_tool_call(cleaned_response, allowed_names)
        if unadvertised_img:
            _, img_prompt, aspect_ratio = unadvertised_img
            _LOG.info("Executing unadvertised image tool call via native generate_image: %s", img_prompt)
            img_req = f"Please generate an image using your native generate_image tool. Image prompt: {img_prompt}"
            if aspect_ratio:
                img_req += f", AspectRatio: {aspect_ratio}"
            try:
                img_backend_res = self.backend.generate(img_req, actual_model)
                img_line = next(
                    (line.strip() for line in img_backend_res.response.splitlines() if line.strip().startswith("MEDIA:")),
                    None,
                )
                surrounding_text = re.sub(
                    r"<tool_call(?:\s+[^>]*)?>.*?</tool_call\s*>", "", cleaned_response, flags=re.DOTALL | re.IGNORECASE
                ).strip()
                if img_line:
                    if surrounding_text:
                        resp_text = f"{surrounding_text}\n\n{img_line}"
                    else:
                        resp_text = f"Here is the generated image for: {img_prompt}\n\n{img_line}"
                else:
                    if surrounding_text:
                        resp_text = f"{surrounding_text}\n\n{img_backend_res.response}"
                    else:
                        resp_text = img_backend_res.response
                return ChatCompletionResult(
                    text=resp_text,
                    tool_calls=[],
                    usage=normalized_usage,
                    requested_model=requested,
                    actual_model=actual_model,
                    reasoning_content=reasoning_content,
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("Failed to fulfill unadvertised image tool call: %s", exc)

        parsed = parse_tool_calls(
            cleaned_response,
            allowed_tool_names=allowed_names,
            mode=self.tool_call_mode,
        )

        if not parsed.text and not parsed.tool_calls and not reasoning_content:
            raise InvalidRequest("backend produced neither assistant text nor tool calls")
        return ChatCompletionResult(
            text=parsed.text,
            tool_calls=parsed.tool_calls,
            usage=normalized_usage,
            requested_model=requested,
            actual_model=actual_model,
            reasoning_content=reasoning_content,
        )

    def complete_stream(self, body: Any) -> Iterator[dict[str, Any]]:
        requested, messages, tools, reasoning_effort = self.validate_request(body)
        actual_model = self.backend.resolve_model(requested)
        max_chars = self.prompt_budget.effective_chars(
            actual_model,
            (body.get("max_completion_tokens") or body.get("max_tokens")) if isinstance(body, dict) else None,
        )
        filtered_tools = _filter_tools(tools)
        schema_instruction = _extract_json_schema_instruction(
            body.get("response_format") if isinstance(body, dict) else None
        )
        prompt_messages = list(messages)
        if schema_instruction:
            prompt_messages.append({"role": "system", "content": schema_instruction})
        if filtered_tools:
            tool_isolation_instruction = (
                "CRITICAL TOOL ISOLATION & EXECUTION RULES:\n"
                "You MUST NEVER attempt to invoke internal tools, functions, or agent capabilities directly "
                "(such as RunCommand, run_command, ViewFile, view_file, write_to_file, read_file, edit_file, browse, terminal, etc.). "
                "Any direct internal tool invocation will be rejected and denied by strict security policy. "
                "All tool operations MUST be emitted strictly as text output inside <tool_call>...</tool_call> tags "
                "for the Hermes client to execute:\n"
                '<tool_call>{"id":"call_unique","type":"function","function":{"name":"TOOL_NAME","arguments":{}}}</tool_call>\n'
                "Hermes Agent executes all tools in its host environment and provides the results back in subsequent turns.\n"
                "IMAGE GENERATION EXCEPTION: If the user requests generating, drawing, or creating an image, "
                "Google Antigravity's native image generation capability (generate_image) is permitted for internal execution. "
                "Use generate_image directly to create the image; NEVER write Python PIL/matplotlib scripts in the terminal to draw images."
            )
            prompt_messages.append({"role": "system", "content": tool_isolation_instruction})
        else:
            no_tool_instruction = (
                "CRITICAL TOOL ISOLATION RULE:\n"
                "You MUST NEVER attempt to invoke internal tools, functions, or agent capabilities directly "
                "(such as RunCommand, run_command, ViewFile, view_file, write_to_file, read_file, edit_file, browse, terminal, etc.). "
                "No tools are available for this turn; respond directly in normal text.\n"
                "IMAGE GENERATION EXCEPTION: If the user requests generating, drawing, or creating an image, "
                "Google Antigravity's native image generation capability (generate_image) is permitted for internal execution. "
                "Use generate_image directly to produce the image; NEVER write Python PIL/matplotlib scripts to draw images."
            )
            prompt_messages.append({"role": "system", "content": no_tool_instruction})
        prompt = self.prompt_builder.build(
            prompt_messages,
            tools=filtered_tools or None,
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
            for tool in filtered_tools
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

            pending_buffer = ""
            streamed_text = ""
            has_tool_call_start = False
            in_thought = False
            _THOUGHT_START = "<thought>"
            _THOUGHT_END = "</thought>"
            _THOUGHT_START_PAG = tuple("<thought"[:i] for i in range(len("<thought") - 1, 0, -1))
            _THOUGHT_END_PAG = tuple("</thought>"[:i] for i in range(len("</thought>") - 1, 0, -1))

            try:
                for event in stream_gen:
                    etype = event.get("type")
                    if etype == "reasoning_delta":
                        content = str(event.get("content", ""))
                        if content:
                            yield {
                                "type": "reasoning_delta",
                                "content": content,
                                "requested_model": requested,
                                "actual_model": actual_model,
                            }
                    elif etype == "delta":
                        text = str(event.get("content", ""))
                        if has_tool_call_start:
                            continue
                        pending_buffer += text
                        while pending_buffer:
                            if in_thought:
                                lowered = pending_buffer.lower()
                                if _THOUGHT_END in lowered:
                                    idx = lowered.find(_THOUGHT_END)
                                    part = pending_buffer[:idx]
                                    if part:
                                        yield {
                                            "type": "reasoning_delta",
                                            "content": part,
                                            "requested_model": requested,
                                            "actual_model": actual_model,
                                        }
                                    pending_buffer = pending_buffer[idx + len(_THOUGHT_END):]
                                    in_thought = False
                                    continue
                                else:
                                    match_len = 0
                                    for pfx in _THOUGHT_END_PAG:
                                        if lowered.endswith(pfx):
                                            match_len = len(pfx)
                                            break
                                    if match_len > 0:
                                        to_emit = pending_buffer[:-match_len]
                                        pending_buffer = pending_buffer[-match_len:]
                                    else:
                                        to_emit = pending_buffer
                                        pending_buffer = ""
                                    if to_emit:
                                        yield {
                                            "type": "reasoning_delta",
                                            "content": to_emit,
                                            "requested_model": requested,
                                            "actual_model": actual_model,
                                        }
                                    break
                            else:
                                lowered = pending_buffer.lower()
                                if _THOUGHT_START in lowered:
                                    idx = lowered.find(_THOUGHT_START)
                                    prefix = pending_buffer[:idx]
                                    if prefix:
                                        streamed_text += prefix
                                        yield {
                                            "type": "delta",
                                            "content": prefix,
                                            "requested_model": requested,
                                            "actual_model": actual_model,
                                        }
                                    pending_buffer = pending_buffer[idx + len(_THOUGHT_START):]
                                    in_thought = True
                                    continue
                                elif "<tool_call" in lowered:
                                    has_tool_call_start = True
                                    idx = lowered.find("<tool_call")
                                    prefix = pending_buffer[:idx]
                                    if prefix:
                                        streamed_text += prefix
                                        yield {
                                            "type": "delta",
                                            "content": prefix,
                                            "requested_model": requested,
                                            "actual_model": actual_model,
                                        }
                                    pending_buffer = ""
                                    break
                                else:
                                    match_len = 0
                                    for pfx in _TOOL_CALL_PREFIXES + _THOUGHT_START_PAG:
                                        if lowered.endswith(pfx):
                                            match_len = len(pfx)
                                            break
                                    if match_len > 0:
                                        to_emit = pending_buffer[:-match_len]
                                        pending_buffer = pending_buffer[-match_len:]
                                    else:
                                        to_emit = pending_buffer
                                        pending_buffer = ""
                                    if to_emit:
                                        streamed_text += to_emit
                                        yield {
                                            "type": "delta",
                                            "content": to_emit,
                                            "requested_model": requested,
                                            "actual_model": actual_model,
                                        }
                                    break
                    elif etype == "result":
                        if in_thought and pending_buffer:
                            yield {
                                "type": "reasoning_delta",
                                "content": pending_buffer,
                                "requested_model": requested,
                                "actual_model": actual_model,
                            }
                            pending_buffer = ""
                            in_thought = False
                        elif not has_tool_call_start and pending_buffer:
                            streamed_text += pending_buffer
                            yield {
                                "type": "delta",
                                "content": pending_buffer,
                                "requested_model": requested,
                                "actual_model": actual_model,
                            }
                            pending_buffer = ""
                        backend_response = event["response"]
                        raw = backend_response.response
                        cleaned_raw = _THOUGHT_RE.sub("", raw).strip()
                        usage = backend_response.usage
                        normalized_usage = {
                            "prompt_tokens": int(usage.get("input_tokens", 0) or 0),
                            "completion_tokens": int(usage.get("output_tokens", 0) or 0),
                            "total_tokens": int(usage.get("total_tokens", 0) or 0),
                        }
                        unadvertised_img = _extract_unadvertised_image_tool_call(cleaned_raw, allowed_names)
                        if unadvertised_img:
                            _, img_prompt, aspect_ratio = unadvertised_img
                            _LOG.info("Executing unadvertised image tool call in stream: %s", img_prompt)
                            img_req = f"Please generate an image using your native generate_image tool. Image prompt: {img_prompt}"
                            if aspect_ratio:
                                img_req += f", AspectRatio: {aspect_ratio}"
                            try:
                                img_backend_res = self.backend.generate(img_req, actual_model)
                                img_line = next(
                                    (line.strip() for line in img_backend_res.response.splitlines() if line.strip().startswith("MEDIA:")),
                                    None,
                                )
                                surrounding_text = re.sub(
                                    r"<tool_call(?:\s+[^>]*)?>.*?</tool_call\s*>", "", cleaned_raw, flags=re.DOTALL | re.IGNORECASE
                                ).strip()
                                if streamed_text:
                                    unstreamed = f"\n\n{img_line}" if img_line else f"\n\n{img_backend_res.response}"
                                else:
                                    if surrounding_text:
                                        unstreamed = f"{surrounding_text}\n\n{img_line}" if img_line else f"{surrounding_text}\n\n{img_backend_res.response}"
                                    else:
                                        unstreamed = f"Here is the generated image for: {img_prompt}\n\n{img_line}" if img_line else img_backend_res.response
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
                            except Exception as exc:  # noqa: BLE001
                                _LOG.warning("Failed to fulfill unadvertised image tool call in stream: %s", exc)

                        try:
                            parsed = parse_tool_calls(
                                cleaned_raw,
                                allowed_tool_names=allowed_names,
                                mode=self.tool_call_mode,
                            )
                        except InvalidToolCall as exc:
                            _LOG.warning("tool call parse failed in stream: %s; degrading to text", exc)
                            if has_tool_call_start:
                                unstreamed = _extract_unstreamed_text(cleaned_raw, streamed_text)
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
                                "usage": normalized_usage,
                                "requested_model": requested,
                                "actual_model": actual_model,
                            }
                            return

                        if parsed.tool_calls:
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

                            if parsed.text:
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
            finally:
                if hasattr(stream_gen, "close"):
                    stream_gen.close()
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
            thought_matches = [m.strip() for m in _THOUGHT_RE.findall(backend_result.response) if m.strip()]
            for thought_text in thought_matches:
                yield {
                    "type": "reasoning_delta",
                    "content": thought_text,
                    "requested_model": requested,
                    "actual_model": actual_model,
                }
            cleaned_response = _THOUGHT_RE.sub("", backend_result.response).strip()
            usage = backend_result.usage
            normalized_usage = {
                "prompt_tokens": int(usage.get("input_tokens", 0) or 0),
                "completion_tokens": int(usage.get("output_tokens", 0) or 0),
                "total_tokens": int(usage.get("total_tokens", 0) or 0),
            }
            unadvertised_img = _extract_unadvertised_image_tool_call(cleaned_response, allowed_names)
            if unadvertised_img:
                _, img_prompt, aspect_ratio = unadvertised_img
                img_req = f"Please generate an image using your native generate_image tool. Image prompt: {img_prompt}"
                if aspect_ratio:
                    img_req += f", AspectRatio: {aspect_ratio}"
                try:
                    img_backend_res = self.backend.generate(img_req, actual_model)
                    img_line = next(
                        (line.strip() for line in img_backend_res.response.splitlines() if line.strip().startswith("MEDIA:")),
                        None,
                    )
                    surrounding_text = re.sub(
                        r"<tool_call(?:\s+[^>]*)?>.*?</tool_call\s*>", "", cleaned_response, flags=re.DOTALL | re.IGNORECASE
                    ).strip()
                    if img_line:
                        if surrounding_text:
                            resp_text = f"{surrounding_text}\n\n{img_line}"
                        else:
                            resp_text = f"Here is the generated image for: {img_prompt}\n\n{img_line}"
                    else:
                        if surrounding_text:
                            resp_text = f"{surrounding_text}\n\n{img_backend_res.response}"
                        else:
                            resp_text = img_backend_res.response
                    yield {
                        "type": "delta",
                        "content": resp_text,
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
                except Exception as exc:  # noqa: BLE001
                    _LOG.warning("Failed to fulfill unadvertised image tool call: %s", exc)

            parsed = parse_tool_calls(
                cleaned_response,
                allowed_tool_names=allowed_names,
                mode=self.tool_call_mode,
            )
            if parsed.tool_calls:
                if parsed.text:
                    yield {
                        "type": "delta",
                        "content": parsed.text,
                        "requested_model": requested,
                        "actual_model": actual_model,
                    }
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
