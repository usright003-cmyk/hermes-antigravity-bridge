import typing
import unittest

from hermes_antigravity_bridge.contracts import BackendResponse
from hermes_antigravity_bridge.errors import InvalidRequest, UnknownModel
from hermes_antigravity_bridge.integrations.hermes import HermesPromptBuilder
from hermes_antigravity_bridge.prompt.budget import PromptBudget
from hermes_antigravity_bridge.service import ChatCompletionService


class FakeBackend:
    def __init__(self, response="OK"):
        self.response = response
        self.prompts = []

    def list_models(self, *, force_refresh=False):
        return ("model-a", "model-b")

    def resolve_model(self, requested):
        if requested not in self.list_models():
            raise UnknownModel(f"unknown model: {requested}")
        return requested

    def generate(self, prompt, model):
        self.prompts.append((prompt, model))
        return BackendResponse(
            response=self.response,
            model=model,
            usage={"input_tokens": 11, "output_tokens": 2, "total_tokens": 13},
            duration_seconds=0.1,
        )

    def readiness(self):
        return {"status": "ready", "models": list(self.list_models())}


class ChatCompletionServiceTests(unittest.TestCase):
    def make_service(self, response="OK"):
        backend = FakeBackend(response)
        service = ChatCompletionService(
            backend=backend,
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=PromptBudget(),
        )
        return service, backend

    def test_plain_completion_preserves_latest_request(self):
        service, backend = self.make_service("ANSWER")
        result = service.complete({
            "model": "model-a",
            "reasoning_effort": "low",
            "stream_options": {"include_usage": True},
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Hermes memory"},
                {"role": "user", "content": "old"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "LATEST_SERVICE_SENTINEL"},
            ],
        })
        self.assertEqual(result.text, "ANSWER")
        self.assertEqual(result.actual_model, "model-a")
        self.assertEqual(result.usage["prompt_tokens"], 11)
        self.assertIn("LATEST_SERVICE_SENTINEL", backend.prompts[0][0])
        self.assertTrue(backend.prompts[0][0].endswith(
            "Answer CURRENT_USER_REQUEST_JSON, using the system instructions, memory, and recent context above."
        ))

    def test_tool_calls_are_limited_to_hermes_advertised_tools(self):
        response = '<tool_call>{"name":"memory","arguments":{"action":"add","content":"fact"}}</tool_call>'
        service, _ = self.make_service(response)
        result = service.complete({
            "model": "model-a",
            "messages": [{"role": "user", "content": "remember this"}],
            "tools": [{"type": "function", "function": {"name": "memory", "parameters": {"type": "object"}}}],
        })
        self.assertEqual(result.text, "")
        self.assertEqual(result.tool_calls[0]["function"]["name"], "memory")

    def test_rejects_request_without_user_role(self):
        service, _ = self.make_service()
        with self.assertRaisesRegex(InvalidRequest, "user message"):
            service.complete({
                "model": "model-a",
                "messages": [{"role": "system", "content": "only system"}],
            })

    def test_rejects_unknown_or_unsupported_fields(self):
        service, _ = self.make_service()
        with self.assertRaisesRegex(InvalidRequest, "unsupported request fields"):
            service.complete({
                "model": "model-a",
                "messages": [{"role": "user", "content": "hello"}],
                "logit_bias": {"1": 2},
            })

    def test_rejects_invalid_message_and_tool_shapes(self):
        service, _ = self.make_service()
        with self.assertRaisesRegex(InvalidRequest, "message 0"):
            service.complete({"model": "model-a", "messages": ["bad"]})
        with self.assertRaisesRegex(InvalidRequest, "tool 0"):
            service.complete({
                "model": "model-a",
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [{"type": "function", "function": {}}],
            })

    def test_complete_stream_yields_events(self):
        service, _ = self.make_service("STREAMED_TEXT")
        events = list(service.complete_stream({
            "model": "model-a",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        }))
        self.assertTrue(any(e["type"] == "delta" and e["content"] == "STREAMED_TEXT" for e in events))
        self.assertTrue(any(e["type"] == "finish" and e["finish_reason"] == "stop" for e in events))


    def test_accepts_max_completion_tokens_and_standard_fields(self):
        service, _ = self.make_service("OK")
        result = service.complete({
            "model": "model-a",
            "messages": [{"role": "user", "content": "hello"}],
            "max_completion_tokens": 100,
            "seed": 42,
            "parallel_tool_calls": True,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
        })
        self.assertEqual(result.text, "OK")

    def test_validate_request_return_type_and_annotation(self):
        hints = typing.get_type_hints(ChatCompletionService.validate_request)
        expected_type = tuple[str, list[dict[str, typing.Any]], list[dict[str, typing.Any]], str | None]
        self.assertEqual(hints["return"], expected_type)

        service, _ = self.make_service()
        ret = service.validate_request({
            "model": "model-a",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "high",
        })
        self.assertIsInstance(ret, tuple)
        model, messages, tools, effort = ret
        self.assertEqual(model, "model-a")
        self.assertEqual(len(messages), 1)
        self.assertEqual(tools, [])
        self.assertEqual(effort, "high")

    def test_strict_type_error_handling_on_effort_kwarg(self):
        call_count = {"complete": 0, "stream": 0}

        class EffortRejectingBackend(FakeBackend):
            def generate(self, prompt, model, **kwargs):
                call_count["complete"] += 1
                if "effort" in kwargs:
                    raise TypeError("generate() got an unexpected keyword argument 'effort'")
                return super().generate(prompt, model)

            def generate_stream(self, prompt, model, **kwargs):
                call_count["stream"] += 1
                if "effort" in kwargs:
                    raise TypeError("generate_stream() got an unexpected keyword argument 'effort'")
                return iter([{"type": "delta", "content": "streamed"}])

        backend = EffortRejectingBackend()
        service = ChatCompletionService(
            backend=backend,
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=PromptBudget(),
        )

        # 1. Effort kwarg TypeError is caught and retried without effort
        res = service.complete({
            "model": "model-a",
            "reasoning_effort": "high",
            "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(res.text, "OK")
        self.assertEqual(call_count["complete"], 2)

        events = list(service.complete_stream({
            "model": "model-a",
            "reasoning_effort": "high",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }))
        self.assertTrue(any(e.get("content") == "streamed" for e in events))
        self.assertEqual(call_count["stream"], 2)

        # 2. Unrelated internal TypeError is NOT masked and NOT retried
        internal_calls = {"complete": 0, "stream": 0}

        class BrokenInternalBackend(FakeBackend):
            def generate(self, prompt, model, **kwargs):
                internal_calls["complete"] += 1
                raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")

            def generate_stream(self, prompt, model, **kwargs):
                internal_calls["stream"] += 1
                raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")

        broken_service = ChatCompletionService(
            backend=BrokenInternalBackend(),
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=PromptBudget(),
        )

        with self.assertRaisesRegex(TypeError, "unsupported operand"):
            broken_service.complete({
                "model": "model-a",
                "reasoning_effort": "high",
                "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(internal_calls["complete"], 1)

        with self.assertRaisesRegex(TypeError, "unsupported operand"):
            list(broken_service.complete_stream({
                "model": "model-a",
                "reasoning_effort": "high",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }))
        self.assertEqual(internal_calls["stream"], 1)

        # 3. When reasoning_effort is None, unexpected keyword argument TypeError is NOT retried
        no_effort_calls = {"complete": 0, "stream": 0}

        class InternalKwargErrorBackend(FakeBackend):
            def generate(self, prompt, model, **kwargs):
                no_effort_calls["complete"] += 1
                raise TypeError("internal_function() got an unexpected keyword argument 'debug'")

            def generate_stream(self, prompt, model, **kwargs):
                no_effort_calls["stream"] += 1
                raise TypeError("internal_function() got an unexpected keyword argument 'debug'")

        no_effort_service = ChatCompletionService(
            backend=InternalKwargErrorBackend(),
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=PromptBudget(),
        )

        with self.assertRaisesRegex(TypeError, "unexpected keyword argument 'debug'"):
            no_effort_service.complete({
                "model": "model-a",
                "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(no_effort_calls["complete"], 1)

        with self.assertRaisesRegex(TypeError, "unexpected keyword argument 'debug'"):
            list(no_effort_service.complete_stream({
                "model": "model-a",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }))
        self.assertEqual(no_effort_calls["stream"], 1)

        # 4. Non-streaming backend in complete_stream handles effort and fallback
        non_stream_calls = {"count": 0}

        class NonStreamingEffortBackend(FakeBackend):
            def generate(self, prompt, model, **kwargs):
                non_stream_calls["count"] += 1
                if "effort" in kwargs:
                    raise TypeError("generate() got an unexpected keyword argument 'effort'")
                return super().generate(prompt, model)

        non_stream_backend = NonStreamingEffortBackend()
        if hasattr(non_stream_backend, "generate_stream"):
            delattr(non_stream_backend, "generate_stream")

        non_stream_service = ChatCompletionService(
            backend=non_stream_backend,
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=PromptBudget(),
        )
        events = list(non_stream_service.complete_stream({
            "model": "model-a",
            "reasoning_effort": "high",
            "messages": [{"role": "user", "content": "hi"}],
        }))
        self.assertTrue(any(e.get("content") == "OK" for e in events))
        self.assertEqual(non_stream_calls["count"], 2)


if __name__ == "__main__":
    unittest.main()
