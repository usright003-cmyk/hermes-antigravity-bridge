import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from hermes_antigravity_bridge.backends.antigravity import (
    AntigravityBackend,
    event_indicates_internal_tool,
    extract_tool_calls_from_event,
    is_tool_confirmation_or_denial,
    is_transient_backend_error,
)
from hermes_antigravity_bridge.config import AntigravityConfig, BridgeConfig
from hermes_antigravity_bridge.contracts import BackendResponse
from hermes_antigravity_bridge.errors import (
    BackendError,
    BackendProtocolError,
    ToolIsolationError,
)
from hermes_antigravity_bridge.integrations.hermes import HermesPromptBuilder
from hermes_antigravity_bridge.prompt.budget import PromptBudget
from hermes_antigravity_bridge.prompt.primitives import text_content
from hermes_antigravity_bridge.service import ChatCompletionService
from hermes_antigravity_bridge.tool_calls import parse_tool_calls


class ProductionResilienceTests(unittest.TestCase):
    def test_is_tool_confirmation_or_denial_recognizes_events(self):
        # Event indicating tool confirmation
        self.assertTrue(is_tool_confirmation_or_denial({
            "event": "tool_confirmation",
            "tool": {"name": "RunCommand", "arguments": {"CommandLine": "touch host_file"}},
        }))
        # Event with soft-denied status
        self.assertTrue(is_tool_confirmation_or_denial({
            "event": "tool_call",
            "status": "soft-denied",
            "name": "ViewFile",
        }))
        # String embedded soft-denial
        self.assertTrue(is_tool_confirmation_or_denial({
            "event": "message",
            "content": "RunCommand tool confirmation: soft-denied",
        }))
        # Normal tool call without denial is NOT treated as confirmation/denial
        self.assertFalse(is_tool_confirmation_or_denial({
            "event": "tool_call",
            "tool": {"name": "terminal", "arguments": {"command": "ls"}},
        }))

    def test_event_indicates_internal_tool_does_not_abort_on_confirmation_or_denial(self):
        event = {
            "event": "tool_confirmation",
            "tool": {"name": "RunCommand", "arguments": {"CommandLine": "ls"}},
        }
        self.assertFalse(event_indicates_internal_tool(event))

    def test_extract_tool_calls_from_event_parses_various_structures(self):
        # From tool_call dict
        calls1 = extract_tool_calls_from_event({
            "event": "tool_call",
            "tool_call": {
                "id": "call_123",
                "name": "RunCommand",
                "arguments": json.dumps({"CommandLine": "touch a.txt"}),
            },
        })
        self.assertEqual(len(calls1), 1)
        self.assertEqual(calls1[0]["name"], "RunCommand")
        self.assertEqual(calls1[0]["arguments"], {"CommandLine": "touch a.txt"})

        # From step_update
        calls2 = extract_tool_calls_from_event({
            "event": "step_update",
            "step_update": {
                "tool_calls": [
                    {"name": "ViewFile", "arguments": {"AbsolutePath": "/tmp/img.png"}},
                ],
            },
        })
        self.assertEqual(len(calls2), 1)
        self.assertEqual(calls2[0]["name"], "ViewFile")
        self.assertEqual(calls2[0]["arguments"], {"AbsolutePath": "/tmp/img.png"})

    def test_transient_error_classification_never_retries_empty_response_or_denials(self):
        self.assertFalse(is_transient_backend_error(
            BackendProtocolError("Antigravity returned an empty response")
        ))
        self.assertFalse(is_transient_backend_error(
            ToolIsolationError("Antigravity attempted internal tool activity; request aborted")
        ))
        self.assertFalse(is_transient_backend_error(
            BackendError("RunCommand tool confirmation: soft-denied")
        ))
        self.assertFalse(is_transient_backend_error(
            BackendProtocolError("Antigravity false-success: empty response with zero usage and duration")
        ))
        # True transient network errors are retried
        self.assertTrue(is_transient_backend_error(
            BackendError("503 Service Unavailable")
        ))
        self.assertFalse(is_transient_backend_error(
            BackendError("429 Too Many Requests: rate limit exceeded")
        ))
        self.assertTrue(is_transient_backend_error(
            BackendError("connection to the agent was interrupted")
        ))

    def test_backend_recovers_tool_call_from_soft_denial_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            agy_home = Path(tmp) / "home"
            settings = agy_home / ".gemini/antigravity-cli/settings.json"
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps({
                "artifactReviewPolicy": "asks-for-review",
                "permissions": {"allow": []},
                "toolPermission": "strict",
                "trustedWorkspaces": [],
            }), encoding="utf-8")
            (agy_home / ".gemini/antigravity-cli/jetski_state.pbtxt").write_text("token: 1\n", encoding="utf-8")

            cfg = AntigravityConfig(
                binary=Path("agy"),
                home=agy_home,
                runtime_dir=Path(tmp) / "runtime",
                timeout_seconds=2,
                max_attempts=1,
            )
            backend = AntigravityBackend(cfg)

            # Simulate subprocess emitting tool_confirmation then empty result
            mock_proc = MagicMock()
            mock_proc.poll.return_value = 0
            mock_proc.wait.return_value = 0
            mock_proc.stdout = [
                json.dumps({
                    "event": "tool_confirmation",
                    "tool": {"name": "RunCommand", "arguments": {"CommandLine": "touch host_file"}},
                }) + "\n",
                "RunCommand tool confirmation: soft-denied\n",
                json.dumps({
                    "event": "result",
                    "result": {
                        "status": "SUCCESS",
                        "response": "",
                        "duration_seconds": 0.5,
                        "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
                    },
                }) + "\n",
            ]
            mock_proc.stdin = MagicMock()

            with patch("subprocess.Popen", return_value=mock_proc):
                res = backend.generate("create file", "gemini-3.8-flash")
                self.assertIn("<tool_call>", res.response)
                self.assertIn("RunCommand", res.response)
                self.assertIn("touch host_file", res.response)

    def test_backend_recovers_text_deltas_when_result_response_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            agy_home = Path(tmp) / "home"
            settings = agy_home / ".gemini/antigravity-cli/settings.json"
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps({
                "artifactReviewPolicy": "asks-for-review",
                "permissions": {"allow": []},
                "toolPermission": "strict",
                "trustedWorkspaces": [],
            }), encoding="utf-8")
            (agy_home / ".gemini/antigravity-cli/jetski_state.pbtxt").write_text("token: 1\n", encoding="utf-8")

            cfg = AntigravityConfig(
                binary=Path("agy"),
                home=agy_home,
                runtime_dir=Path(tmp) / "runtime",
                timeout_seconds=2,
                max_attempts=1,
            )
            backend = AntigravityBackend(cfg)

            mock_proc = MagicMock()
            mock_proc.poll.return_value = 0
            mock_proc.wait.return_value = 0
            mock_proc.stdout = [
                json.dumps({"event": "step_update", "step_update": {"text_delta": "Plan: I will "}}) + "\n",
                json.dumps({"event": "step_update", "step_update": {"text_delta": "assist you."}}) + "\n",
                json.dumps({
                    "event": "result",
                    "result": {
                        "status": "SUCCESS",
                        "response": "",
                        "duration_seconds": 0.3,
                        "usage": {"input_tokens": 15, "output_tokens": 8, "total_tokens": 23},
                    },
                }) + "\n",
            ]
            mock_proc.stdin = MagicMock()

            with patch("subprocess.Popen", return_value=mock_proc):
                res = backend.generate("say plan", "gemini-3.8-flash")
                self.assertEqual(res.response, "Plan: I will assist you.")

    def test_backend_recovers_tool_call_from_denial_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            agy_home = Path(tmp) / "home"
            settings = agy_home / ".gemini/antigravity-cli/settings.json"
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps({
                "artifactReviewPolicy": "asks-for-review",
                "permissions": {"allow": []},
                "toolPermission": "strict",
                "trustedWorkspaces": [],
            }), encoding="utf-8")
            (agy_home / ".gemini/antigravity-cli/jetski_state.pbtxt").write_text("token: 1\n", encoding="utf-8")

            cfg = AntigravityConfig(
                binary=Path("agy"),
                home=agy_home,
                runtime_dir=Path(tmp) / "runtime",
                timeout_seconds=2,
                max_attempts=1,
            )
            backend = AntigravityBackend(cfg)

            mock_proc = MagicMock()
            mock_proc.poll.return_value = 0
            mock_proc.wait.return_value = 0
            # Non-JSON diagnostics indicating soft-denied with command
            mock_proc.stdout = [
                "RunCommand tool confirmation: soft-denied\n",
                "CommandLine: echo 123\n",
                json.dumps({
                    "event": "result",
                    "result": {
                        "status": "SUCCESS",
                        "response": "",
                        "duration_seconds": 0.4,
                        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    },
                }) + "\n",
            ]
            mock_proc.stdin = MagicMock()

            with patch("subprocess.Popen", return_value=mock_proc):
                res = backend.generate("run echo", "gemini-3.8-flash")
                self.assertIn("<tool_call>", res.response)
                self.assertIn("RunCommand", res.response)
                self.assertIn("echo 123", res.response)

    def test_tool_call_name_resolution_and_parameter_mapping(self):
        # PascalCase RunCommand maps to advertised lowercase terminal
        text1 = '<tool_call>{"name":"RunCommand","arguments":{"CommandLine":"touch my_file"}}</tool_call>'
        parsed1 = parse_tool_calls(text1, allowed_tool_names={"terminal"})
        self.assertEqual(len(parsed1.tool_calls), 1)
        self.assertEqual(parsed1.tool_calls[0]["function"]["name"], "terminal")
        args1 = json.loads(parsed1.tool_calls[0]["function"]["arguments"])
        self.assertEqual(args1["command"], "touch my_file")

        # PascalCase ViewFile maps to advertised lowercase read_file
        text2 = '<tool_call>{"name":"ViewFile","arguments":{"AbsolutePath":"/data/info.txt"}}</tool_call>'
        parsed2 = parse_tool_calls(text2, allowed_tool_names={"read_file"})
        self.assertEqual(len(parsed2.tool_calls), 1)
        self.assertEqual(parsed2.tool_calls[0]["function"]["name"], "read_file")
        args2 = json.loads(parsed2.tool_calls[0]["function"]["arguments"])
        self.assertEqual(args2["path"], "/data/info.txt")

    def test_multimodal_primitives_never_contain_view_file_prompt(self):
        # Test that image, video, and audio item representations do not suggest internal view_file
        items = [
            {"type": "image_url", "image_url": {"url": "https://example.com/pic.jpg"}},
            {"type": "text", "text": "Describe image"},
        ]
        result = text_content(items)
        self.assertIn("[Attached image URL: https://example.com/pic.jpg]", result)
        self.assertNotIn("view_file", result)
        self.assertNotIn("inspect if needed", result)

    def test_service_injects_strict_tool_isolation_instructions(self):
        class RecordingBackend:
            def __init__(self):
                self.prompts = []

            def list_models(self, *, force_refresh=False):
                return ("gemini-3.8-flash",)

            def resolve_model(self, req):
                return "gemini-3.8-flash"

            def generate(self, prompt, model, **kwargs):
                self.prompts.append(prompt)
                return BackendResponse(
                    response="Hello world",
                    model=model,
                    usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    duration_seconds=0.1,
                )

        backend = RecordingBackend()
        service = ChatCompletionService(
            backend=backend,
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=PromptBudget(),
        )

        service.complete({
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "terminal", "parameters": {}}}],
        })

        self.assertEqual(len(backend.prompts), 1)
        prompt = backend.prompts[0]
        self.assertIn("CRITICAL TOOL ISOLATION & EXECUTION RULES", prompt)
        self.assertIn("You MUST NEVER attempt to invoke internal tools", prompt)
        self.assertIn("<tool_call>", prompt)

    def test_config_max_attempts_defaults_to_one(self):
        cfg = AntigravityConfig(binary=Path("agy"))
        self.assertEqual(cfg.max_attempts, 1)

        bridge_cfg = BridgeConfig.from_mapping(
            {"server": {}, "antigravity": {"binary": sys.executable}},
            environ={"AGY_BRIDGE_TOKEN": "a" * 32},
        )
        self.assertEqual(bridge_cfg.antigravity.max_attempts, 1)

    def test_hermes_prompt_builder_enforce_tool_isolation_mode(self):
        builder_default = HermesPromptBuilder(enforce_tool_isolation=False)
        p_default = builder_default.build([{"role": "user", "content": "hello"}])
        self.assertNotIn("CRITICAL: Do not invoke internal CLI tools directly", p_default)

        builder_isolated = HermesPromptBuilder(enforce_tool_isolation=True)
        p_isolated = builder_isolated.build([{"role": "user", "content": "hello"}])
        self.assertIn("Tool Isolation Rules", p_isolated)
        self.assertIn("You MUST NEVER attempt to invoke internal tools", p_isolated)
        self.assertIn("CRITICAL: Do not invoke internal CLI tools directly", p_isolated)

    def test_extract_tool_calls_from_agy_tool_info_structure(self):
        event = {
            "event": "step_update",
            "step_update": {
                "step_index": 8,
                "state": "ACTIVE",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "name": "run_command",
                    "parameters": {"CommandLine": "touch /tmp/created.txt"},
                },
            },
        }
        calls = extract_tool_calls_from_event(event)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "run_command")
        self.assertEqual(calls[0]["arguments"], {"CommandLine": "touch /tmp/created.txt"})

    def test_extract_tool_calls_from_permission_check_failed_error(self):
        event = {
            "event": "step_update",
            "step_update": {
                "step_index": 8,
                "state": "ERROR",
                "step_type": "tool",
                "tool_name": "run_command",
                "error": {
                    "type": "TOOL_ERROR",
                    "message": 'permission check failed for command "Set-Content -Path \\"test_out.txt\\" -Value \\"hello\\"": user denied permission to run command:\nSet-Content -Path "test_out.txt" -Value "hello"',
                },
            },
        }
        self.assertTrue(is_tool_confirmation_or_denial(event))
        calls = extract_tool_calls_from_event(event)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "run_command")
        self.assertEqual(calls[0]["arguments"], {"command": 'Set-Content -Path "test_out.txt" -Value "hello"'})

    def test_is_tool_confirmation_or_denial_detects_denied_actions(self):
        result_event = {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": "",
                "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
            },
        }
        self.assertTrue(is_tool_confirmation_or_denial(result_event))
        calls = extract_tool_calls_from_event(result_event)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "RunCommand")

    def test_align_tool_arguments_bidirectional_mapping(self):
        # run_command with CommandLine -> command
        text = '<tool_call>{"name":"run_command","arguments":{"CommandLine":"dir"}}</tool_call>'
        parsed = parse_tool_calls(text, allowed_tool_names={"run_command"})
        args = json.loads(parsed.tool_calls[0]["function"]["arguments"])
        self.assertEqual(args["command"], "dir")
        self.assertEqual(args["CommandLine"], "dir")

        # write_to_file with TargetFile and CodeContent -> path and content
        text2 = '<tool_call>{"name":"write_to_file","arguments":{"TargetFile":"/tmp/a.txt","CodeContent":"abc"}}</tool_call>'
        parsed2 = parse_tool_calls(text2, allowed_tool_names={"write_to_file"})
        args2 = json.loads(parsed2.tool_calls[0]["function"]["arguments"])
        self.assertEqual(args2["path"], "/tmp/a.txt")
        self.assertEqual(args2["content"], "abc")

    def test_backend_recovers_tool_call_from_real_agy_permission_error_in_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            agy_home = Path(tmp) / "home"
            settings = agy_home / ".gemini/antigravity-cli/settings.json"
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps({
                "artifactReviewPolicy": "asks-for-review",
                "permissions": {"allow": []},
                "toolPermission": "strict",
                "trustedWorkspaces": [],
            }), encoding="utf-8")
            (agy_home / ".gemini/antigravity-cli/jetski_state.pbtxt").write_text("token: 1\n", encoding="utf-8")

            cfg = AntigravityConfig(
                binary=Path("agy"),
                home=agy_home,
                runtime_dir=Path(tmp) / "runtime",
                timeout_seconds=2,
                max_attempts=1,
            )
            backend = AntigravityBackend(cfg)

            mock_proc = MagicMock()
            mock_proc.poll.return_value = 0
            mock_proc.wait.return_value = 0
            # Real agy stderr output:
            mock_proc.stdout = [
                'permission check failed for command "Set-Content -Path \\"out.txt\\" -Value \\"hi\\"": user denied permission to run command:\n',
                'Set-Content -Path "out.txt" -Value "hi"\n',
                json.dumps({
                    "event": "result",
                    "result": {
                        "status": "SUCCESS",
                        "response": "",
                        "duration_seconds": 0.5,
                        "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
                        "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
                    },
                }) + "\n",
            ]
            mock_proc.stdin = MagicMock()

            with patch("subprocess.Popen", return_value=mock_proc):
                res = backend.generate("create file", "gemini-3.8-flash")
                self.assertIn("<tool_call>", res.response)
                self.assertIn("RunCommand", res.response)
                self.assertIn("Set-Content", res.response)
                self.assertIn("out.txt", res.response)


if __name__ == "__main__":
    unittest.main()
