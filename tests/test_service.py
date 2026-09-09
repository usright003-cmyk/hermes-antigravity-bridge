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


if __name__ == "__main__":
    unittest.main()
