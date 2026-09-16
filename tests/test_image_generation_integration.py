"""Comprehensive unit tests for Antigravity Bridge image generation integration with Hermes."""

import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import connect_hermes
from hermes_antigravity_bridge.config import (
    AntigravityConfig,
    BridgeConfig,
    ServerConfig,
)
from hermes_antigravity_bridge.contracts import BackendResponse
from hermes_antigravity_bridge.integrations.hermes import HermesPromptBuilder
from hermes_antigravity_bridge.openai_http import create_http_server
from hermes_antigravity_bridge.prompt.budget import PromptBudget
from hermes_antigravity_bridge.prompt.primitives import _is_safe_media_path
from hermes_antigravity_bridge.service import ChatCompletionService
from hermes_antigravity_bridge.tool_calls import parse_tool_calls


class TestImagePromptIsolation(unittest.TestCase):
    """Verify prompt builder isolation rules explicitly permit native generate_image."""

    def test_enforce_tool_isolation_permits_generate_image(self):
        builder = HermesPromptBuilder(enforce_tool_isolation=True)
        prompt = builder.build([{"role": "user", "content": "draw a cat"}])

        # Security isolation rules intact
        self.assertIn("Tool Isolation Rules", prompt)
        self.assertIn("You MUST NEVER attempt to invoke internal tools", prompt)
        self.assertIn("<tool_call>", prompt)

        # Image generation rule and Python PIL prohibition
        self.assertIn("Image Generation Rule", prompt)
        self.assertIn("generate_image", prompt)
        self.assertIn("NEVER write Python scripts", prompt)
        self.assertIn("PIL", prompt)

        # Final directive mentions native image tool
        self.assertIn("CRITICAL: Do not invoke internal CLI tools directly", prompt)
        self.assertIn("generate_image", prompt)

    def test_service_injects_image_exception_with_and_without_tools(self):
        class DummyBackend:
            def __init__(self):
                self.prompts = []

            def list_models(self, *, force_refresh=False):
                return ("gemini-3.8-flash",)

            def resolve_model(self, req):
                return "gemini-3.8-flash"

            def generate(self, prompt, model, **kwargs):
                self.prompts.append(prompt)
                return BackendResponse(
                    response="Assistant response",
                    model=model,
                    usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    duration_seconds=0.1,
                )

        backend = DummyBackend()
        service = ChatCompletionService(
            backend=backend,
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=PromptBudget(),
        )

        # 1. With tools (e.g. terminal)
        service.complete({
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "draw a flower"}],
            "tools": [{"type": "function", "function": {"name": "terminal", "parameters": {}}}],
        })
        p_tools = backend.prompts[-1]
        self.assertIn("IMAGE GENERATION EXCEPTION", p_tools)
        self.assertIn("generate_image", p_tools)
        self.assertIn("NEVER write Python PIL/matplotlib scripts", p_tools)

        # 2. Without tools
        service.complete({
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "draw a sunset"}],
            "tools": None,
        })
        p_no_tools = backend.prompts[-1]
        self.assertIn("IMAGE GENERATION EXCEPTION", p_no_tools)
        self.assertIn("generate_image", p_no_tools)
        self.assertIn("NEVER write Python PIL/matplotlib scripts", p_no_tools)


class TestImageToolSynonyms(unittest.TestCase):
    """Verify tool call parser maps image synonyms appropriately."""

    def test_image_synonym_generate_image_to_image_gen(self):
        raw = '<tool_call>{"id":"c1","type":"function","function":{"name":"generate_image","arguments":{"prompt":"a cute cat"}}}</tool_call>'
        parsed = parse_tool_calls(raw, allowed_tool_names={"image_gen"}, mode="compatible")
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0]["function"]["name"], "image_gen")

    def test_image_synonym_image_gen_to_generate_image(self):
        raw = '<tool_call>{"id":"c2","type":"function","function":{"name":"image_gen","arguments":{"prompt":"a puppy"}}}</tool_call>'
        parsed = parse_tool_calls(raw, allowed_tool_names={"generate_image"}, mode="compatible")
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0]["function"]["name"], "generate_image")


class TestUnadvertisedImageToolCallInterception(unittest.TestCase):
    """Verify that when Hermes does not advertise an image tool, the bridge intercepts image tool calls."""

    def test_complete_intercepts_unadvertised_image_tool_call(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            img_file = Path(tmp_dir) / "test_tiger.png"
            img_file.write_bytes(b"PNG_TIGER_DATA")

            class MockBackend:
                def __init__(self):
                    self.calls = []

                def list_models(self, *, force_refresh=False):
                    return ("gemini-3.8-flash",)

                def resolve_model(self, req):
                    return "gemini-3.8-flash"

                def generate(self, prompt, model, **kwargs):
                    self.calls.append(prompt)
                    if len(self.calls) == 1:
                        # Initial call: model emits unadvertised tool call
                        return BackendResponse(
                            response='<tool_call>{"id":"call_tiger","type":"function","function":{"name":"generate_image","arguments":{"prompt":"a majestic bengal tiger"}}}</tool_call>',
                            model=model,
                            usage={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
                            duration_seconds=0.5,
                        )
                    # Second call: bridge automatically executes native generate_image
                    return BackendResponse(
                        response=f"Here is your image:\n\nMEDIA:{img_file.as_posix()}",
                        model=model,
                        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                        duration_seconds=0.2,
                    )

            backend = MockBackend()
            service = ChatCompletionService(
                backend=backend,
                prompt_builder=HermesPromptBuilder(),
                prompt_budget=PromptBudget(),
            )

            # Hermes only advertised 'terminal'
            result = service.complete({
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "draw a majestic bengal tiger"}],
                "tools": [{"type": "function", "function": {"name": "terminal", "parameters": {}}}],
            })

            self.assertEqual(len(backend.calls), 2)
            self.assertIn("a majestic bengal tiger", backend.calls[1])
            self.assertIn("MEDIA:", result.text)
            self.assertIn("test_tiger.png", result.text)

    def test_complete_stream_intercepts_unadvertised_image_tool_call(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            img_file = Path(tmp_dir) / "test_stream_tiger.png"
            img_file.write_bytes(b"PNG_STREAM_TIGER")

            class MockStreamBackend:
                def __init__(self):
                    self.gen_calls = []

                def list_models(self, *, force_refresh=False):
                    return ("gemini-3.8-flash",)

                def resolve_model(self, req):
                    return "gemini-3.8-flash"

                def generate_stream(self, prompt, model, **kwargs) -> Iterator[dict]:
                    yield {
                        "type": "result",
                        "response": BackendResponse(
                            response='<tool_call>{"id":"call_s1","type":"function","function":{"name":"generate_image","arguments":{"prompt":"a white tiger"}}}</tool_call>',
                            model=model,
                            usage={"input_tokens": 15, "output_tokens": 8, "total_tokens": 23},
                            duration_seconds=0.3,
                        ),
                    }

                def generate(self, prompt, model, **kwargs):
                    self.gen_calls.append(prompt)
                    return BackendResponse(
                        response=f"Here is your image:\n\nMEDIA:{img_file.as_posix()}",
                        model=model,
                        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                        duration_seconds=0.2,
                    )

            backend = MockStreamBackend()
            service = ChatCompletionService(
                backend=backend,
                prompt_builder=HermesPromptBuilder(),
                prompt_budget=PromptBudget(),
            )

            events = list(service.complete_stream({
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "draw a white tiger"}],
                "tools": [{"type": "function", "function": {"name": "terminal", "parameters": {}}}],
                "stream": True,
            }))

            delta_events = [e for e in events if e.get("type") == "delta"]
            finish_events = [e for e in events if e.get("type") == "finish"]
            self.assertTrue(any("test_stream_tiger.png" in d.get("content", "") for d in delta_events))
            self.assertEqual(len(finish_events), 1)
            self.assertEqual(finish_events[0]["finish_reason"], "stop")


class TestSafeMediaPath(unittest.TestCase):
    """Verify safe media path validation across bridge state, temp dirs, and brain dirs."""

    def test_safe_media_roots(self):
        home = Path.home()
        media_p = home / ".gemini" / "antigravity-cli" / "media" / "cat.png"
        brain_p = home / ".gemini" / "antigravity-cli" / "brain" / "conv-1" / "dog.png"
        temp_p = Path(tempfile.gettempdir()) / "generated_image.webp"
        bridge_state_p = home / ".local" / "state" / "hermes-antigravity-bridge" / "test.jpg"

        self.assertTrue(_is_safe_media_path(media_p))
        self.assertTrue(_is_safe_media_path(brain_p))
        self.assertTrue(_is_safe_media_path(temp_p))
        self.assertTrue(_is_safe_media_path(bridge_state_p))

        # Reject sensitive files
        ssh_p = home / ".ssh" / "id_rsa.png"
        self.assertFalse(_is_safe_media_path(ssh_p))


class TestMediaUrlAndMarkdownInHTTP(unittest.TestCase):
    """Verify chat completions responses contain clean MEDIA:<path> without duplicate or URL clutter."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.media_file = self.tmp_path / "flower_123.png"
        self.media_file.write_bytes(b"FLOWER_IMAGE_BYTES")

        self.token = "test-token-" + ("x" * 24)
        self.bridge_cfg = BridgeConfig(
            server=ServerConfig(
                host="127.0.0.1",
                port=0,
                token=self.token,
            ),
            antigravity=AntigravityConfig(binary=Path(sys.executable)),
            prompt=PromptBudget(),
        )

        class MediaBackend:
            def __init__(self, media_path):
                self.media_path = media_path

            def list_models(self, *, force_refresh=False):
                return ("gemini-3.8-flash",)

            def resolve_model(self, req):
                return "gemini-3.8-flash"

            def generate(self, prompt, model, **kwargs):
                return BackendResponse(
                    response=f"Here is your flower:\n\nMEDIA:{self.media_path.as_posix()}",
                    model=model,
                    usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                )

            def generate_stream(self, prompt, model, **kwargs):
                yield {
                    "type": "delta",
                    "content": f"Here is your flower:\n\nMEDIA:{self.media_path.as_posix()}",
                }
                yield {
                    "type": "result",
                    "response": BackendResponse(
                        response=f"Here is your flower:\n\nMEDIA:{self.media_path.as_posix()}",
                        model=model,
                        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    ),
                }

        self.media_backend = MediaBackend(self.media_file)
        self.service = ChatCompletionService(
            backend=self.media_backend,
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=PromptBudget(),
        )
        self.server = create_http_server(self.bridge_cfg, self.service)
        self.http_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.http_thread.start()
        port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_non_streaming_chat_completion_emits_markdown_and_media_url(self):
        req = urllib.request.Request(
            f"{self.base}/v1/chat/completions",
            data=json.dumps({"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "draw a flower"}]}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        content = data["choices"][0]["message"]["content"]
        self.assertIn(f"MEDIA:{self.media_file.as_posix()}", content)
        self.assertNotIn("MEDIA_URL:", content)
        self.assertNotIn("![Generated Image]", content)
        self.assertNotIn("/v1/media/", content)
        media_lines = [l for l in content.splitlines() if l.strip().startswith("MEDIA:")]
        self.assertEqual(len(media_lines), 1)

    def test_streaming_chat_completion_emits_markdown_and_media_url(self):
        req = urllib.request.Request(
            f"{self.base}/v1/chat/completions",
            data=json.dumps({
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "draw a flower"}],
                "stream": True,
            }).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        accumulated_text = ""
        with urllib.request.urlopen(req, timeout=5) as resp:
            for line in resp:
                decoded = line.decode("utf-8").strip()
                if decoded.startswith("data: ") and decoded != "data: [DONE]":
                    chunk = json.loads(decoded.removeprefix("data: "))
                    delta = chunk["choices"][0].get("delta", {})
                    if "content" in delta:
                        accumulated_text += delta["content"]

        self.assertIn(f"MEDIA:{self.media_file.as_posix()}", accumulated_text)
        self.assertNotIn("MEDIA_URL:", accumulated_text)
        self.assertNotIn("![Generated Image]", accumulated_text)
        self.assertNotIn("/v1/media/", accumulated_text)
        media_lines = [l for l in accumulated_text.splitlines() if l.strip().startswith("MEDIA:")]
        self.assertEqual(len(media_lines), 1)

    def test_streaming_chat_completion_with_quoted_media_path(self):
        class QuotedMediaBackend:
            def __init__(self, media_path):
                self.media_path = media_path

            def list_models(self, *, force_refresh=False):
                return ("gemini-3.8-flash",)

            def resolve_model(self, req):
                return "gemini-3.8-flash"

            def generate_stream(self, prompt, model, **kwargs):
                yield {
                    "type": "delta",
                    "content": f'Here is your flower:\n\nMEDIA:"{self.media_path.as_posix()}"',
                }
                yield {
                    "type": "result",
                    "response": BackendResponse(
                        response=f'Here is your flower:\n\nMEDIA:"{self.media_path.as_posix()}"',
                        model=model,
                        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    ),
                }

        self.service.backend = QuotedMediaBackend(self.media_file)
        req = urllib.request.Request(
            f"{self.base}/v1/chat/completions",
            data=json.dumps({
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "draw a flower"}],
                "stream": True,
            }).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        accumulated_text = ""
        with urllib.request.urlopen(req, timeout=5) as resp:
            for line in resp:
                decoded = line.decode("utf-8").strip()
                if decoded.startswith("data: ") and decoded != "data: [DONE]":
                    chunk = json.loads(decoded.removeprefix("data: "))
                    delta = chunk["choices"][0].get("delta", {})
                    if "content" in delta:
                        accumulated_text += delta["content"]

        self.assertIn(f"MEDIA:{self.media_file.as_posix()}", accumulated_text)
        self.assertNotIn("MEDIA_URL:", accumulated_text)
        self.assertNotIn("![Generated Image]", accumulated_text)
        media_lines = [l for l in accumulated_text.splitlines() if l.strip().startswith("MEDIA:")]
        self.assertEqual(len(media_lines), 1)
        self.assertEqual(media_lines[0], f"MEDIA:{self.media_file.as_posix()}")

    def test_completion_deduplicates_duplicate_media_tags(self):
        class DuplicateMediaBackend:
            def __init__(self, media_path):
                self.media_path = media_path

            def list_models(self, *, force_refresh=False):
                return ("gemini-3.8-flash",)

            def resolve_model(self, req):
                return "gemini-3.8-flash"

            def generate(self, prompt, model, **kwargs):
                return BackendResponse(
                    response=f"Here is your flower:\n\nMEDIA:{self.media_path.as_posix()}\n\nMEDIA:{self.media_path.as_posix()}",
                    model=model,
                    usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                )

        self.service.backend = DuplicateMediaBackend(self.media_file)
        req = urllib.request.Request(
            f"{self.base}/v1/chat/completions",
            data=json.dumps({"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "draw duplicate"}]}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        content = data["choices"][0]["message"]["content"]
        media_lines = [l for l in content.splitlines() if l.strip().startswith("MEDIA:")]
        self.assertEqual(len(media_lines), 1)
        self.assertEqual(media_lines[0], f"MEDIA:{self.media_file.as_posix()}")

    def test_streaming_completion_deduplicates_streamed_media_tags(self):
        class MultiChunkMediaBackend:
            def __init__(self, media_path):
                self.media_path = media_path

            def list_models(self, *, force_refresh=False):
                return ("gemini-3.8-flash",)

            def resolve_model(self, req):
                return "gemini-3.8-flash"

            def generate_stream(self, prompt, model, **kwargs):
                yield {"type": "delta", "content": f"Photo:\n\nMEDIA:{self.media_path.as_posix()}\n"}
                yield {"type": "delta", "content": f"\nMEDIA:{self.media_path.as_posix()}\n"}
                yield {
                    "type": "result",
                    "response": BackendResponse(
                        response=f"Photo:\n\nMEDIA:{self.media_path.as_posix()}",
                        model=model,
                        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    ),
                }

        self.service.backend = MultiChunkMediaBackend(self.media_file)
        req = urllib.request.Request(
            f"{self.base}/v1/chat/completions",
            data=json.dumps({"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "draw multi"}], "stream": True}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        accumulated_text = ""
        with urllib.request.urlopen(req, timeout=5) as resp:
            for line in resp:
                decoded = line.decode("utf-8").strip()
                if decoded.startswith("data: ") and decoded != "data: [DONE]":
                    chunk = json.loads(decoded.removeprefix("data: "))
                    delta = chunk["choices"][0].get("delta", {})
                    if "content" in delta:
                        accumulated_text += delta["content"]

        media_lines = [l for l in accumulated_text.splitlines() if l.strip().startswith("MEDIA:")]
        self.assertEqual(len(media_lines), 1)
        self.assertEqual(media_lines[0], f"MEDIA:{self.media_file.as_posix()}")

    def test_streaming_completion_handles_split_token_media_tags(self):
        class SplitTokenMediaBackend:
            def __init__(self, media_path):
                self.media_path = media_path

            def list_models(self, *, force_refresh=False):
                return ("gemini-3.8-flash",)

            def resolve_model(self, req):
                return "gemini-3.8-flash"

            def generate_stream(self, prompt, model, **kwargs):
                yield {"type": "delta", "content": "Photo:\n\nMED"}
                yield {"type": "delta", "content": f"IA:{self.media_path.as_posix()}\n"}
                yield {"type": "delta", "content": "\nMED"}
                yield {"type": "delta", "content": f"IA:{self.media_path.as_posix()}\n"}
                yield {
                    "type": "result",
                    "response": BackendResponse(
                        response=f"Photo:\n\nMEDIA:{self.media_path.as_posix()}",
                        model=model,
                        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    ),
                }

        self.service.backend = SplitTokenMediaBackend(self.media_file)
        req = urllib.request.Request(
            f"{self.base}/v1/chat/completions",
            data=json.dumps({"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "draw split"}], "stream": True}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        accumulated_text = ""
        with urllib.request.urlopen(req, timeout=5) as resp:
            for line in resp:
                decoded = line.decode("utf-8").strip()
                if decoded.startswith("data: ") and decoded != "data: [DONE]":
                    chunk = json.loads(decoded.removeprefix("data: "))
                    delta = chunk["choices"][0].get("delta", {})
                    if "content" in delta:
                        accumulated_text += delta["content"]

        media_lines = [l for l in accumulated_text.splitlines() if l.strip().startswith("MEDIA:")]
        self.assertEqual(len(media_lines), 1)
        self.assertEqual(media_lines[0], f"MEDIA:{self.media_file.as_posix()}")
        self.assertNotIn("MEDIA_URL:", accumulated_text)
        self.assertNotIn("![Generated Image]", accumulated_text)
        self.assertEqual(accumulated_text.strip(), f"Photo:\n\nMEDIA:{self.media_file.as_posix()}")

    def test_v1_images_generations_endpoint_url_and_b64(self):
        # 1. URL format
        req_url = urllib.request.Request(
            f"{self.base}/v1/images/generations",
            data=json.dumps({"prompt": "a beautiful rose", "response_format": "url"}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.assertIn("data", data)
        self.assertIn("url", data["data"][0])
        self.assertIn("/v1/media/", data["data"][0]["url"])

        # 2. b64_json format
        req_b64 = urllib.request.Request(
            f"{self.base}/v1/images/generations",
            data=json.dumps({"prompt": "a beautiful rose", "response_format": "b64_json"}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_b64, timeout=5) as resp:
            data_b64 = json.loads(resp.read().decode("utf-8"))
        self.assertIn("data", data_b64)
        self.assertIn("b64_json", data_b64["data"][0])
        decoded_bytes = base64.b64decode(data_b64["data"][0]["b64_json"])
        self.assertEqual(decoded_bytes, b"FLOWER_IMAGE_BYTES")


class TestConnectHermesImageConfig(unittest.TestCase):
    """Verify connect_hermes.py configures image provider and model."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "config.yaml"

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_hermes_config_sets_image_provider(self):
        self.cfg_path.write_text("temperature: 0.7\n", encoding="utf-8")
        connect_hermes.update_hermes_config(self.cfg_path, token="tok-image-123")

        parsed = connect_hermes.parse_yaml_fallback(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(parsed.get("image_provider"), "custom:antigravity")
        self.assertEqual(parsed.get("image_model"), "gemini-3.8-flash")
        self.assertEqual(parsed.get("image_api_base"), "http://127.0.0.1:8765/v1")


class TestImageGenerationEdgeCasesAndHardening(unittest.TestCase):
    """Deep verification of edge cases and robustness improvements."""

    def test_unadvertised_image_tool_call_in_strict_mode(self):
        """In strict mode, unadvertised image tool calls must NOT raise InvalidToolCall."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            img_file = Path(tmp_dir) / "strict_tiger.png"
            img_file.write_bytes(b"PNG_DATA")

            class MockBackend:
                def __init__(self):
                    self.calls = []

                def list_models(self, *, force_refresh=False):
                    return ("gemini-3.8-flash",)

                def resolve_model(self, req):
                    return "gemini-3.8-flash"

                def generate(self, prompt, model, **kwargs):
                    self.calls.append(prompt)
                    if len(self.calls) == 1:
                        return BackendResponse(
                            response='<tool_call>{"name":"generate_image","arguments":{"prompt":"a majestic tiger"}}</tool_call>',
                            model=model,
                            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                        )
                    return BackendResponse(
                        response=f"MEDIA:{img_file.as_posix()}",
                        model=model,
                        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    )

            service = ChatCompletionService(
                backend=MockBackend(),
                prompt_builder=HermesPromptBuilder(),
                prompt_budget=PromptBudget(),
                tool_call_mode="strict",
            )

            # Must NOT raise InvalidToolCall("tool 'generate_image' was not advertised by Hermes")
            result = service.complete({
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "draw a tiger"}],
                "tools": [{"type": "function", "function": {"name": "terminal", "parameters": {}}}],
            })
            self.assertIn("MEDIA:", result.text)
            self.assertIn("strict_tiger.png", result.text)

    def test_accompanying_bengali_text_preserved(self):
        """Assistant text accompanying image tool call must be preserved, not wiped out."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            img_file = Path(tmp_dir) / "bengali_cat.png"
            img_file.write_bytes(b"BENGALI_CAT")

            bengali_intro = "এই যে আপনার সুন্দর বিড়ালের ছবি:"
            raw_resp = f"{bengali_intro}\n<tool_call>{{\"name\":\"generate_image\",\"arguments\":{{\"prompt\":\"a cute cat\"}}}}</tool_call>"

            class MockBackend:
                def __init__(self):
                    self.calls = []

                def list_models(self, *, force_refresh=False):
                    return ("gemini-3.8-flash",)

                def resolve_model(self, req):
                    return "gemini-3.8-flash"

                def generate(self, prompt, model, **kwargs):
                    self.calls.append(prompt)
                    if len(self.calls) == 1:
                        return BackendResponse(
                            response=raw_resp,
                            model=model,
                            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                        )
                    return BackendResponse(
                        response=f"MEDIA:{img_file.as_posix()}",
                        model=model,
                        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    )

            service = ChatCompletionService(
                backend=MockBackend(),
                prompt_builder=HermesPromptBuilder(),
                prompt_budget=PromptBudget(),
            )

            result = service.complete({
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "একটি বিড়ালের ছবি আঁকো"}],
                "tools": [{"type": "function", "function": {"name": "terminal", "parameters": {}}}],
            })

            # The original Bengali text must be preserved!
            self.assertIn(bengali_intro, result.text)
            self.assertIn("MEDIA:", result.text)
            self.assertIn("bengali_cat.png", result.text)

    def test_pascalcase_prompt_and_aspect_ratio_extraction(self):
        """PascalCase parameters (Prompt, AspectRatio, ImageName) in tool calls must be extracted."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            img_file = Path(tmp_dir) / "sunset_16_9.png"
            img_file.write_bytes(b"SUNSET_IMAGE")

            captured_prompt = []

            class MockBackend:
                def list_models(self, *, force_refresh=False):
                    return ("gemini-3.8-flash",)

                def resolve_model(self, req):
                    return "gemini-3.8-flash"

                def generate(self, prompt, model, **kwargs):
                    captured_prompt.append(prompt)
                    if len(captured_prompt) == 1:
                        # Antigravity native schema format with PascalCase keys
                        return BackendResponse(
                            response='<tool_call>{"id":"call_1","type":"function","function":{"name":"default_api:generate_image","arguments":{"Prompt":"a brilliant ocean sunset","AspectRatio":"16:9","ImageName":"ocean_sunset"}}}</tool_call>',
                            model=model,
                            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                        )
                    return BackendResponse(
                        response=f"MEDIA:{img_file.as_posix()}",
                        model=model,
                        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    )

            service = ChatCompletionService(
                backend=MockBackend(),
                prompt_builder=HermesPromptBuilder(),
                prompt_budget=PromptBudget(),
            )

            result = service.complete({
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "generate a 16:9 ocean sunset"}],
                "tools": [{"type": "function", "function": {"name": "terminal", "parameters": {}}}],
            })

            self.assertEqual(len(captured_prompt), 2)
            self.assertIn("a brilliant ocean sunset", captured_prompt[1])
            self.assertIn("16:9", captured_prompt[1])
            self.assertIn("sunset_16_9.png", result.text)

    def test_cross_turn_contamination_prevented(self):
        """Images generated in earlier turns must not be attached to later unrelated turns."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            brain_conv = home / ".gemini" / "antigravity-cli" / "brain" / "conv-42"
            brain_conv.mkdir(parents=True, exist_ok=True)
            old_image = brain_conv / "old_turn_cat.png"
            old_image.write_bytes(b"OLD_CAT")
            past_time = time.time() - 3600
            os.utime(old_image, (past_time, past_time))

            cwd = Path(tmp_dir) / "cwd"
            cwd.mkdir(exist_ok=True)

            candidate_dirs = [
                home / ".gemini" / "antigravity-cli" / "brain" / "conv-42",
                home / ".gemini" / "antigravity-cli" / "media",
                cwd,
            ]
            run_start = time.time() - 2.0
            found = []
            for cdir in candidate_dirs:
                if cdir.is_dir():
                    for f in cdir.iterdir():
                        if f.is_file() and f.suffix.lower() in {".png", ".jpg"} and f.stat().st_mtime >= run_start:
                            found.append(f)
            self.assertEqual(len(found), 0)

    def test_images_generations_stale_file_rejected(self):
        """In /v1/images/generations, pre-existing stale images must not be returned if generation produces nothing."""
        token = "test-token-stale"
        bridge_cfg = BridgeConfig(
            server=ServerConfig(
                host="127.0.0.1",
                port=0,
                token=token,
            ),
            antigravity=AntigravityConfig(binary=Path(sys.executable)),
            prompt=PromptBudget(),
        )

        class FailingImageBackend:
            def list_models(self, *, force_refresh=False):
                return ("gemini-3.8-flash",)

            def resolve_model(self, req):
                return "gemini-3.8-flash"

            def generate(self, prompt, model, **kwargs):
                return BackendResponse(
                    response="Sorry, cannot generate image.",
                    model=model,
                    usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                )

        service = ChatCompletionService(
            backend=FailingImageBackend(),
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=PromptBudget(),
        )
        server = create_http_server(bridge_cfg, service)
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        try:
            port = server.server_address[1]
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/images/generations",
                data=json.dumps({"prompt": "a rose"}).encode("utf-8"),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 502)
        finally:
            server.shutdown()
            server.server_close()


class TestMediaDeduplicationAndFiltering(unittest.TestCase):
    """Deep unit tests for media tag deduplication and stream filtering."""

    def test_stream_media_filter_split_across_tokens(self):
        from hermes_antigravity_bridge.openai_http import _StreamMediaFilter

        f = _StreamMediaFilter()
        out = []
        for c in ["Photo:\n\nMED", "IA:/tmp/cat.png\n", "\nMED", "IA:/tmp/cat.png\n"]:
            out.extend(f.process_chunk(c))
        out.extend(f.finish())
        self.assertEqual("".join(out), "Photo:\n\nMEDIA:/tmp/cat.png\n")

    def test_stream_media_filter_colon_split(self):
        from hermes_antigravity_bridge.openai_http import _StreamMediaFilter

        f = _StreamMediaFilter()
        out = []
        for c in ["Photo:\n\nMEDIA:", "/tmp/cat.png\n", "\nMEDIA:", "/tmp/cat.png\n"]:
            out.extend(f.process_chunk(c))
        out.extend(f.finish())
        self.assertEqual("".join(out), "Photo:\n\nMEDIA:/tmp/cat.png\n")

    def test_stream_media_filter_quoted_path(self):
        from hermes_antigravity_bridge.openai_http import _StreamMediaFilter

        f = _StreamMediaFilter()
        out = []
        for c in ['Photo:\n\nMEDIA:"/tmp/cat.png"\n']:
            out.extend(f.process_chunk(c))
        out.extend(f.finish())
        self.assertEqual("".join(out), "Photo:\n\nMEDIA:/tmp/cat.png\n")

    def test_stream_media_filter_strips_media_url_and_proxy_markdown(self):
        from hermes_antigravity_bridge.openai_http import _StreamMediaFilter

        f = _StreamMediaFilter()
        out = []
        chunks = [
            "Photo:\n\nMEDIA:/tmp/1.png\n",
            "MEDIA_URL:http://127.0.0.1:8765/v1/media/1.png\n",
            "![Generated Image](http://127.0.0.1:8765/v1/media/1.png)\n",
            "MEDIA:/tmp/1.png\n",
        ]
        for c in chunks:
            out.extend(f.process_chunk(c))
        out.extend(f.finish())
        self.assertEqual("".join(out), "Photo:\n\nMEDIA:/tmp/1.png\n")

    def test_stream_media_filter_preserves_normal_words(self):
        from hermes_antigravity_bridge.openai_http import _StreamMediaFilter

        f = _StreamMediaFilter()
        out = []
        for c in ["Model ", "evaluation ", "is done."]:
            out.extend(f.process_chunk(c))
        out.extend(f.finish())
        self.assertEqual("".join(out), "Model evaluation is done.")

    def test_stream_media_filter_unstreamed_chunk_after_media_emitted(self):
        from hermes_antigravity_bridge.openai_http import _StreamMediaFilter

        f = _StreamMediaFilter()
        out = []
        out.extend(f.process_chunk("Photo:\n\nMEDIA:/tmp/1.png\n"))
        out.extend(f.process_chunk("\n\nMEDIA:/tmp/1.png"))
        out.extend(f.finish())
        self.assertEqual("".join(out), "Photo:\n\nMEDIA:/tmp/1.png\n")

    def test_format_single_media_response_in_backend(self):
        from hermes_antigravity_bridge.backends.antigravity import (
            _format_single_media_response,
        )

        # Overwrite duplicate tags with canonical tag
        raw = "Here is your image:\n\nMEDIA:/old/img1.png\nMEDIA:/old/img2.png"
        formatted = _format_single_media_response(raw, "MEDIA:/canonical/img.png")
        self.assertEqual(formatted, "Here is your image:\n\nMEDIA:/canonical/img.png")

        # MEDIA_URL without MEDIA tag is cleaned (Bug 1 regression check)
        raw_url = "Image generated.\nMEDIA_URL:http://127.0.0.1:8765/v1/media/test.png"
        cleaned_url = _format_single_media_response(raw_url)
        self.assertEqual(cleaned_url, "Image generated.")

        # Proxy markdown without MEDIA tag is cleaned
        raw_md = "Image generated.\n![Generated Image](http://127.0.0.1:8765/v1/media/test.png)"
        cleaned_md = _format_single_media_response(raw_md)
        self.assertEqual(cleaned_md, "Image generated.")

    def test_service_media_helpers(self):
        from hermes_antigravity_bridge.service import (
            _deduplicate_media_lines,
            _strip_media_lines,
        )

        # _strip_media_lines strips MEDIA:, MEDIA_URL:, and localhost markdown (Bug 2 regression check)
        surrounding = (
            "Here is the text.\n"
            "MEDIA:/tmp/img1.png\n"
            "MEDIA_URL:http://127.0.0.1:8765/v1/media/img1.png\n"
            "![Generated Image](http://127.0.0.1:8765/v1/media/img1.png)\n"
            "End of text."
        )
        stripped = _strip_media_lines(surrounding)
        self.assertEqual(stripped, "Here is the text.\nEnd of text.")

        # _deduplicate_media_lines leaves exactly one clean tag at end
        dup_text = "Intro.\nMEDIA:'/tmp/1.png'\n\nMEDIA:\"/tmp/2.png\""
        deduped = _deduplicate_media_lines(dup_text)
        self.assertEqual(deduped, "Intro.\n\nMEDIA:/tmp/2.png")

    def test_multi_image_batch_support(self):
        from hermes_antigravity_bridge.openai_http import (
            _deduplicate_media_lines as http_dedupe,
        )
        from hermes_antigravity_bridge.openai_http import (
            _StreamMediaFilter,
        )
        from hermes_antigravity_bridge.service import (
            _deduplicate_media_lines,
            _extract_unadvertised_image_tool_calls,
        )

        # 1. Extraction of 3 batch image tool calls
        raw_model_output = (
            "Here are the 3 requested images:\n"
            "<tool_call>{\"name\": \"generate_image\", \"arguments\": {\"prompt\": \"A sunset over mountains\", \"aspect_ratio\": \"16:9\"}}</tool_call>\n"
            "<tool_call>{\"name\": \"generate_image\", \"arguments\": {\"prompt\": \"A cozy cabin in snow\", \"aspect_ratio\": \"1:1\"}}</tool_call>\n"
            "<tool_call>{\"name\": \"image_gen\", \"arguments\": {\"prompt\": \"A futuristic flying car\"}}</tool_call>"
        )
        calls = _extract_unadvertised_image_tool_calls(raw_model_output, allowed_tool_names={"read_file", "terminal"})
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], ("generate_image", "A sunset over mountains", "16:9"))
        self.assertEqual(calls[1], ("generate_image", "A cozy cabin in snow", "1:1"))
        self.assertEqual(calls[2], ("image_gen", "A futuristic flying car", None))

        # 2. Service media deduplication with keep_all_distinct=True
        multi_media_text = (
            "Here are the generated images:\n\n"
            "MEDIA:/tmp/image1.png\n"
            "MEDIA:/tmp/image2.png\n"
            "MEDIA:/tmp/image3.png\n"
            "MEDIA:/tmp/image1.png"  # Duplicate of image1
        )
        deduped = _deduplicate_media_lines(multi_media_text, keep_all_distinct=True)
        expected = (
            "Here are the generated images:\n\n"
            "MEDIA:/tmp/image1.png\n"
            "MEDIA:/tmp/image2.png\n"
            "MEDIA:/tmp/image3.png"
        )
        self.assertEqual(deduped, expected)

        # 3. HTTP deduplication preserves multiple distinct images
        http_deduped = http_dedupe(multi_media_text)
        self.assertEqual(http_deduped, expected)

        # 4. StreamMediaFilter allows multiple distinct images without dropping 2nd or 3rd
        filter_ = _StreamMediaFilter()
        chunks = [
            "Here are the images:\n",
            "MEDIA:/tmp/image1.png\n",
            "MEDIA:/tmp/image2.png\n",
            "MEDIA:/tmp/image1.png\n",  # duplicate, should be skipped
            "MEDIA:/tmp/image3.png\n",
        ]
        emitted: list[str] = []
        for ch in chunks:
            emitted.extend(filter_.process_chunk(ch))
        emitted.extend(filter_.finish())
        joined = "".join(emitted)
        self.assertIn("MEDIA:/tmp/image1.png\n", joined)
        self.assertIn("MEDIA:/tmp/image2.png\n", joined)
        self.assertIn("MEDIA:/tmp/image3.png\n", joined)
        self.assertEqual(joined.count("MEDIA:/tmp/image1.png"), 1)


if __name__ == "__main__":
    unittest.main()
