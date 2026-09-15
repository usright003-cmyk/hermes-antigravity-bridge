import json
import sys
import threading
import unittest
import urllib.request
from pathlib import Path

from hermes_antigravity_bridge.backends.antigravity import (
    _TRANSIENT_MARKERS,
    AntigravityBackend,
    is_transient_backend_error,
)
from hermes_antigravity_bridge.config import (
    AntigravityConfig,
    BridgeConfig,
    ServerConfig,
)
from hermes_antigravity_bridge.contracts import BackendResponse
from hermes_antigravity_bridge.errors import BackendError
from hermes_antigravity_bridge.integrations.hermes import HermesPromptBuilder
from hermes_antigravity_bridge.openai_http import create_http_server
from hermes_antigravity_bridge.prompt.budget import PromptBudget
from hermes_antigravity_bridge.prompt.primitives import (
    compact_tool_output,
    recent_history,
    serialize_history_message,
)
from hermes_antigravity_bridge.service import ChatCompletionService
from hermes_antigravity_bridge.tool_calls import parse_tool_calls


class HermesToolTurnInversionTests(unittest.TestCase):
    def setUp(self):
        self.builder = HermesPromptBuilder()

    def test_trailing_tool_turn_emits_latest_tool_output_section(self):
        messages = [
            {"role": "system", "content": "You are Hermes."},
            {"role": "user", "content": "Check the status of the server."},
            {"role": "assistant", "content": '<tool_call>{"name": "check_status", "arguments": {}}</tool_call>'},
            {"role": "tool", "name": "check_status", "content": "Server status: OK, 0 errors."},
        ]
        prompt = self.builder.build(messages, tools=[{"name": "check_status"}])
        self.assertIn("# LATEST_TOOL_OUTPUT", prompt)
        self.assertIn("Server status: OK, 0 errors.", prompt)
        self.assertIn("Synthesize the tool result above to formulate the next step or final answer for the user.", prompt)
        # Verify earlier user turn is in history, not placed below tool output
        user_pos = prompt.find("Check the status of the server.")
        tool_pos = prompt.find("Server status: OK, 0 errors.")
        self.assertLess(user_pos, tool_pos)

    def test_multiple_trailing_tool_turns_grouped_chronologically(self):
        messages = [
            {"role": "user", "content": "Fetch two configs."},
            {"role": "assistant", "content": "Fetching..."},
            {"role": "tool", "name": "get_cfg_a", "content": "config_a = 1"},
            {"role": "tool", "name": "get_cfg_b", "content": "config_b = 2"},
        ]
        prompt = self.builder.build(messages)
        self.assertIn("# LATEST_TOOL_OUTPUT", prompt)
        self.assertIn("config_a = 1", prompt)
        self.assertIn("config_b = 2", prompt)
        pos_a = prompt.find("config_a = 1")
        pos_b = prompt.find("config_b = 2")
        self.assertLess(pos_a, pos_b)

    def test_trailing_tool_turn_with_tool_isolation(self):
        builder = HermesPromptBuilder(enforce_tool_isolation=True)
        messages = [
            {"role": "user", "content": "Run diagnostics."},
            {"role": "assistant", "content": "Running..."},
            {"role": "tool", "name": "diag", "content": "All systems nominal."},
        ]
        prompt = builder.build(messages, tools=[{"name": "diag"}])
        self.assertIn("# LATEST_TOOL_OUTPUT", prompt)
        self.assertIn("Synthesize the tool result above to formulate the next step or final answer for the user.", prompt)
        self.assertIn("CRITICAL: Do not invoke internal CLI tools directly.", prompt)

    def test_normal_user_turn_retains_user_request_guard(self):
        messages = [
            {"role": "user", "content": "Hello world!"},
        ]
        prompt = self.builder.build(messages)
        self.assertNotIn("# LATEST_TOOL_OUTPUT", prompt)
        self.assertIn("# CURRENT_REQUEST_GUARD", prompt)
        self.assertIn("Answer CURRENT_USER_REQUEST_JSON, using the system instructions, memory, and recent context above.", prompt)


class ToolCallPythonMappingAndSanitizationTests(unittest.TestCase):
    def test_repairs_python_style_single_quoted_dict_with_booleans_and_null(self):
        payload = (
            "<tool_call>{\n"
            "  'name': 'terminal',\n"
            "  'arguments': {'command': 'ls -la', 'background': True, 'timeout': None, 'quiet': False}\n"
            "}</tool_call>"
        )
        result = parse_tool_calls(payload, allowed_tool_names={"terminal"}, mode="compatible")
        self.assertEqual(len(result.tool_calls), 1)
        tc = result.tool_calls[0]
        self.assertEqual(tc["function"]["name"], "terminal")
        args = json.loads(tc["function"]["arguments"])
        self.assertEqual(args["command"], "ls -la")
        self.assertEqual(args["background"], True)
        self.assertIsNone(args["timeout"])
        self.assertEqual(args["quiet"], False)

    def test_strip_unparsed_tags_cleans_assistant_text(self):
        raw = "Thinking about running tests.\n<tool_call>{'invalid': json broken</tool_call>\nReady."
        result = parse_tool_calls(raw, allowed_tool_names={"terminal"}, mode="compatible", strip_unparsed_tags=True)
        self.assertEqual(len(result.tool_calls), 0)
        self.assertNotIn("<tool_call>", result.text)
        self.assertIn("Thinking about running tests.", result.text)
        self.assertIn("Ready.", result.text)

    def test_strip_unparsed_tags_removes_unadvertised_tool_calls(self):
        raw = "I will run this.\n<tool_call>{\"name\":\"unknown_tool\",\"arguments\":{}}</tool_call>\nDone."
        result = parse_tool_calls(raw, allowed_tool_names={"allowed_tool"}, mode="compatible", strip_unparsed_tags=True)
        self.assertEqual(len(result.tool_calls), 0)
        self.assertNotIn("unknown_tool", result.text)
        self.assertNotIn("<tool_call>", result.text)

    def test_serialize_history_message_strips_dangling_tool_calls(self):
        msg = {
            "role": "assistant",
            "content": "Let me check.\n<tool_call>{\"name\": \"test\"}</tool_call>\nAll done.",
        }
        serialized = serialize_history_message(msg)
        self.assertNotIn("<tool_call>", serialized)
        self.assertIn("Let me check.", serialized)
        self.assertIn("All done.", serialized)

    def test_serialize_history_message_strips_unclosed_tool_call(self):
        msg = {
            "role": "assistant",
            "content": "Started.\n<tool_call>{\"name\": \"broken\"",
        }
        serialized = serialize_history_message(msg)
        self.assertNotIn("<tool_call>", serialized)
        self.assertIn("Started.", serialized)


class SmartToolOutputCompactionTests(unittest.TestCase):
    def test_compact_tool_output_under_limit_unchanged(self):
        short_text = "abc " * 300
        self.assertLessEqual(len(short_text), 1500)
        self.assertEqual(compact_tool_output(short_text), short_text)

    def test_compact_tool_output_over_limit_compacts_middle(self):
        head = "".join(f"A{i:04d}" for i in range(120))  # 600 chars
        middle = "".join(f"M{i:04d}" for i in range(160))  # 800 chars
        tail = "".join(f"Z{i:04d}" for i in range(80))  # 400 chars
        full_text = head + middle + tail
        self.assertEqual(len(full_text), 1800)

        compacted = compact_tool_output(full_text)
        self.assertTrue(compacted.startswith(head))
        self.assertTrue(compacted.endswith(tail))
        self.assertIn("[... bridge compacted 800 characters of earlier tool output ...]", compacted)
        self.assertNotIn(middle, compacted)

    def test_recent_history_compacts_older_tool_outputs_only(self):
        huge_tool_output_1 = "".join(f"step1_{i:04d} " for i in range(250))
        huge_tool_output_2 = "".join(f"step2_{i:04d} " for i in range(250))
        huge_tool_output_3 = "".join(f"step3_{i:04d} " for i in range(250))

        history = [
            {"role": "user", "content": "step 1"},
            {"role": "tool", "name": "tool1", "content": huge_tool_output_1},
            {"role": "user", "content": "step 2"},
            {"role": "tool", "name": "tool2", "content": huge_tool_output_2},
            {"role": "user", "content": "step 3"},
            {"role": "tool", "name": "tool3", "content": huge_tool_output_3},
        ]
        rendered = recent_history(history, latest_user_index=4, budget=20000)

        # Older tool output 1 must have compaction marker
        self.assertIn("[... bridge compacted", rendered)
        # Recent tool outputs 2 and 3 must NOT have compaction marker
        self.assertIn(huge_tool_output_2[:50], rendered)
        self.assertIn(huge_tool_output_3[:50], rendered)


class AntigravityBackendTransientAndEnvTests(unittest.TestCase):
    def setUp(self):
        self.backend = AntigravityBackend(AntigravityConfig(binary=Path(sys.executable)))

    def test_transient_markers_contain_improperly_formatted_function_call(self):
        self.assertIn("improperly formatted function call", _TRANSIENT_MARKERS)

    def test_transient_failure_classification(self):
        self.assertTrue(is_transient_backend_error(BackendError("Error: 400 improperly formatted function call occurred")))
        self.assertTrue(is_transient_backend_error(BackendError("RESOURCE_EXHAUSTED: quota reached")))
        self.assertFalse(is_transient_backend_error(BackendError("Invalid authentication token")))

    def test_base_environment_exports_utf8(self):
        env = self.backend._base_environment()
        self.assertEqual(env.get("PYTHONUTF8"), "1")
        self.assertEqual(env.get("PYTHONIOENCODING"), "utf-8")


class StreamingSSEProtocolTests(unittest.TestCase):
    def test_streaming_sse_emits_ping_comments_and_unblocks_immediately(self):
        class PingBackend:
            def list_models(self, *, force_refresh=False):
                return ("model-a",)

            def resolve_model(self, requested):
                return requested

            def generate_stream(self, prompt, model, **kwargs):
                yield {"type": "ping"}
                yield {"type": "delta", "content": "Hello after ping"}
                yield {
                    "type": "result",
                    "response": BackendResponse(
                        response="Hello after ping",
                        model=model,
                        usage={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10},
                        status="SUCCESS",
                        duration_seconds=0.1,
                    ),
                }

        token = "test-token-fixed-length-123456789012"
        config = BridgeConfig(
            server=ServerConfig(
                host="127.0.0.1",
                port=0,
                token=token,
                request_body_limit_bytes=4096,
                max_concurrent_requests=1,
            ),
            antigravity=AntigravityConfig(binary=Path(sys.executable)),
            prompt=PromptBudget(),
        )
        service = ChatCompletionService(
            backend=PingBackend(),
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=config.prompt,
        )
        server = create_http_server(config, service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
            body = json.dumps({
                "model": "model-a",
                "messages": [{"role": "user", "content": "test ping stream"}],
                "stream": True,
            }).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get_content_type(), "text/event-stream")
                content = resp.read().decode("utf-8")

            self.assertIn(": ping\n\n", content)
            self.assertIn("Hello after ping", content)
            self.assertIn("data: [DONE]", content)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_complete_stream_no_generator_already_executing(self):
        class GeneratorBackend:
            def list_models(self, *, force_refresh=False):
                return ("model-a",)

            def resolve_model(self, requested):
                return requested

            def generate_stream(self, prompt, model, **kwargs):
                yield {"type": "delta", "content": "chunk 1"}
                yield {"type": "delta", "content": "chunk 2"}
                yield {
                    "type": "result",
                    "response": BackendResponse(
                        response="chunk 1chunk 2",
                        model=model,
                        usage={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10},
                        status="SUCCESS",
                        duration_seconds=0.1,
                    ),
                }

        service = ChatCompletionService(
            backend=GeneratorBackend(),
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=PromptBudget(),
        )
        gen = service.complete_stream({
            "model": "model-a",
            "messages": [{"role": "user", "content": "test"}],
        })
        first = next(gen)
        self.assertEqual(first.get("type"), "delta")
        # Closing early must never raise ValueError: generator already executing
        try:
            gen.close()
        except ValueError as exc:
            self.fail(f"gen.close() raised ValueError: {exc}")

    def test_stream_error_before_headers_returns_500(self):
        class BrokenBackend:
            def list_models(self, *, force_refresh=False):
                return ("model-a",)

            def resolve_model(self, requested):
                return requested

            def generate_stream(self, prompt, model, **kwargs):
                raise BackendError("CLI connection failed")

        token = "test-token-fixed-length-123456789012"
        config = BridgeConfig(
            server=ServerConfig(
                host="127.0.0.1",
                port=0,
                token=token,
                request_body_limit_bytes=4096,
                max_concurrent_requests=1,
            ),
            antigravity=AntigravityConfig(binary=Path(sys.executable)),
            prompt=PromptBudget(),
        )
        service = ChatCompletionService(
            backend=BrokenBackend(),
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=config.prompt,
        )
        server = create_http_server(config, service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
            body = json.dumps({
                "model": "model-a",
                "messages": [{"role": "user", "content": "fail"}],
                "stream": True,
            }).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 502)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
