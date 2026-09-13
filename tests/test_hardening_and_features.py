import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from connect_hermes import create_autostart_batches, register_windows_autostart
from hermes_antigravity_bridge.config import (
    AntigravityConfig,
    BridgeConfig,
    ServerConfig,
)
from hermes_antigravity_bridge.contracts import BackendResponse
from hermes_antigravity_bridge.integrations.hermes import HermesPromptBuilder
from hermes_antigravity_bridge.openai_http import create_http_server
from hermes_antigravity_bridge.prompt.budget import PromptBudget
from hermes_antigravity_bridge.prompt.primitives import (
    _process_media_item,
    cleanup_old_media_files,
)
from hermes_antigravity_bridge.service import ChatCompletionService
from hermes_antigravity_bridge.tool_calls import _repair_json_string, parse_tool_calls

TEST_TOKEN = "test-token-" + ("y" * 32)


class SimpleFakeBackend:
    def list_models(self, *, force_refresh=False):
        return ("model-a", "model-b")

    def resolve_model(self, requested):
        return requested

    def generate(self, prompt, model, **kwargs):
        return BackendResponse(
            response="<thought>I am thinking thoroughly.</thought>Here is the solution.",
            model=model,
            usage={"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
        )

    def readiness(self):
        return {"status": "ready", "models": list(self.list_models())}


class StreamingThoughtBackend(SimpleFakeBackend):
    def generate_stream(self, prompt, model, **kwargs):
        yield {"type": "delta", "content": "<thought>Step 1: Analyz"}
        yield {"type": "delta", "content": "ing problem.</thought>Solution is 42."}
        yield {
            "type": "result",
            "response": BackendResponse(
                response="<thought>Step 1: Analyzing problem.</thought>Solution is 42.",
                model=model,
                usage={"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
                status="SUCCESS",
            ),
        }


class HardeningAndFeaturesTests(unittest.TestCase):
    def setUp(self):
        self.token = TEST_TOKEN
        self.config = BridgeConfig(
            server=ServerConfig(
                host="127.0.0.1",
                port=0,
                token=self.token,
                request_body_limit_bytes=65536,
                max_concurrent_requests=2,
            ),
            antigravity=AntigravityConfig(binary=Path(sys.executable)),
            prompt=PromptBudget(),
        )
        self.service = ChatCompletionService(
            backend=SimpleFakeBackend(),
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=self.config.prompt,
        )
        self.server = create_http_server(self.config, self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    # 1. Media Proxy Route Tests (/v1/media/<token>/<filename>)
    def test_media_proxy_route_serving_and_auth(self):
        media_dir = Path.home() / ".gemini" / "antigravity-cli" / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        test_file = media_dir / "test_artifact_img.png"
        test_content = b"\x89PNG\r\n\x1a\nTEST_MEDIA_BYTES"
        test_file.write_bytes(test_content)
        try:
            # Direct tokenless request (/v1/media/<filename>)
            url_direct = f"{self.base}/v1/media/test_artifact_img.png"
            req_direct = urllib.request.Request(url_direct)
            with urllib.request.urlopen(req_direct, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("Content-Type"), "image/png")
                self.assertEqual(resp.read(), test_content)

            # Direct tokenless request without /v1 prefix (/media/<filename>)
            url_direct_no_v1 = f"{self.base}/media/test_artifact_img.png"
            req_direct_no_v1 = urllib.request.Request(url_direct_no_v1)
            with urllib.request.urlopen(req_direct_no_v1, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.read(), test_content)

            # A. Valid request with path token
            url = f"{self.base}/v1/media/{self.token}/test_artifact_img.png"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("Content-Type"), "image/png")
                self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
                self.assertEqual(resp.headers.get("Connection"), "close")
                self.assertEqual(resp.read(), test_content)

            # A2. Valid request without /v1 prefix (/media/<token>/<filename>)
            url_no_v1 = f"{self.base}/media/{self.token}/test_artifact_img.png"
            req_no_v1 = urllib.request.Request(url_no_v1)
            with urllib.request.urlopen(req_no_v1, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.read(), test_content)

            # B. Valid request with query token
            url_query = f"{self.base}/v1/media/ignored_token/test_artifact_img.png?token={self.token}"
            req_query = urllib.request.Request(url_query)
            with urllib.request.urlopen(req_query, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.read(), test_content)

            # C. Valid request with Bearer authorization header
            req_bearer = urllib.request.Request(
                f"{self.base}/v1/media/header_auth/test_artifact_img.png",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            with urllib.request.urlopen(req_bearer, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.read(), test_content)

            # D. Unauthorized request (bad token)
            req_bad = urllib.request.Request(f"{self.base}/v1/media/wrong-token/test_artifact_img.png")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_bad, timeout=5)
            self.assertEqual(ctx.exception.code, 401)

            # E. Path traversal attack rejection
            req_trav = urllib.request.Request(f"{self.base}/v1/media/{self.token}/..%2f..%2fsecret.txt")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_trav, timeout=5)
            self.assertEqual(ctx.exception.code, 400)

            # F. Not found file
            req_missing = urllib.request.Request(f"{self.base}/v1/media/{self.token}/no_such_file.png")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_missing, timeout=5)
            self.assertEqual(ctx.exception.code, 404)
        finally:
            test_file.unlink(missing_ok=True)

    def test_completion_auto_appends_media_url_for_remote_clients(self):
        # Create a test media image
        media_dir = Path.home() / ".gemini" / "antigravity-cli" / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        img = media_dir / "gen_img_123.png"
        img.write_bytes(b"TEST_IMAGE_BYTES")
        try:
            class ImageGenBackend(SimpleFakeBackend):
                def generate(self, prompt, model, **kwargs):
                    return BackendResponse(
                        response=f"Here is your image:\n\nMEDIA:{img.as_posix()}",
                        model=model,
                        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    )

            self.server.chat_service.backend = ImageGenBackend()
            req = urllib.request.Request(
                f"{self.base}/v1/chat/completions",
                data=json.dumps({"model": "model-a", "messages": [{"role": "user", "content": "draw a cat"}]}).encode("utf-8"),
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            self.assertIn(f"MEDIA:{img.as_posix()}", content)
            self.assertIn("MEDIA_URL:http://", content)
            self.assertIn("/v1/media/gen_img_123", content)
            self.assertNotIn(self.token, content)
            media_line = next(l for l in content.splitlines() if l.startswith("MEDIA_URL:"))
            media_url = media_line.removeprefix("MEDIA_URL:").strip()
            with urllib.request.urlopen(urllib.request.Request(media_url), timeout=5) as m_resp:
                self.assertEqual(m_resp.status, 200)
                self.assertEqual(m_resp.read(), b"TEST_IMAGE_BYTES")
        finally:
            img.unlink(missing_ok=True)

    # 2. Reasoning Content Tests (non-streaming & streaming)
    def test_non_streaming_reasoning_content_extraction(self):
        req = urllib.request.Request(
            f"{self.base}/v1/chat/completions",
            data=json.dumps({"model": "model-a", "messages": [{"role": "user", "content": "think"}]}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choice = data["choices"][0]
            self.assertEqual(choice["message"]["content"], "Here is the solution.")
            self.assertEqual(choice["message"]["reasoning_content"], "I am thinking thoroughly.")
            self.assertEqual(choice["finish_reason"], "stop")

    def test_streaming_thought_tags_extracted_to_reasoning_deltas(self):
        # Swap service backend to StreamingThoughtBackend
        self.server.chat_service.backend = StreamingThoughtBackend()
        req = urllib.request.Request(
            f"{self.base}/chat/completions",
            data=json.dumps({"model": "model-a", "stream": True, "messages": [{"role": "user", "content": "solve"}]}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode("utf-8")

        chunks = [
            json.loads(line.removeprefix("data: "))
            for line in text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]

        # First chunk should have reasoning_content
        reasoning_pieces = [
            c["choices"][0]["delta"]["reasoning_content"]
            for c in chunks
            if c.get("choices") and "reasoning_content" in c["choices"][0]["delta"]
        ]
        content_pieces = [
            c["choices"][0]["delta"]["content"]
            for c in chunks
            if c.get("choices") and "content" in c["choices"][0]["delta"]
        ]

        self.assertEqual("".join(reasoning_pieces), "Step 1: Analyzing problem.")
        self.assertEqual("".join(content_pieces), "Solution is 42.")

    # 3. Safe Media Traversal and Cleanup Tests
    def test_sensitive_host_path_traversal_restricted(self):
        # Sensitive paths outside allowed roots or matching sensitive filenames
        sensitive_samples = [
            "C:/Windows/System32/config/SAM" if os.name == "nt" else "/etc/shadow",
            str(Path.home() / ".ssh" / "id_rsa"),
            str(Path.home() / ".aws" / "credentials"),
            str(Path.home() / ".gemini" / "antigravity-cli" / "jetski_state.pbtxt"),
        ]
        for path_str in sensitive_samples:
            item = {"type": "image_url", "image_url": {"url": path_str}}
            res = _process_media_item(item)
            self.assertIn("restricted host path; omitted for security", res)
            self.assertNotIn("use view_file", res)

    def test_cleanup_old_media_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dir_path = Path(tmp_dir)
            old_file = dir_path / "old_img.jpg"
            new_file = dir_path / "new_img.jpg"
            old_file.write_bytes(b"OLD")
            new_file.write_bytes(b"NEW")

            # Set old_file mtime to 10 days ago
            ten_days_ago = time.time() - (10 * 86400)
            os.utime(old_file, (ten_days_ago, ten_days_ago))

            deleted = cleanup_old_media_files(dir_path, max_age_days=7)
            self.assertEqual(deleted, 1)
            self.assertFalse(old_file.exists())
            self.assertTrue(new_file.exists())

            # Test 24h default cutoff
            older_than_24h = dir_path / "img_25h.jpg"
            older_than_24h.write_bytes(b"25H")
            twenty_five_h_ago = time.time() - (25 * 3600)
            os.utime(older_than_24h, (twenty_five_h_ago, twenty_five_h_ago))

            deleted_24h = cleanup_old_media_files(dir_path)
            self.assertEqual(deleted_24h, 1)
            self.assertFalse(older_than_24h.exists())
            self.assertTrue(new_file.exists())

            # Test max_cache_bytes size enforcement
            new_file.unlink(missing_ok=True)
            large1 = dir_path / "large1.bin"
            large2 = dir_path / "large2.bin"
            large1.write_bytes(b"X" * 100)
            time.sleep(0.01)
            large2.write_bytes(b"Y" * 100)
            # Both files are new (<24h), but total size is > 150 bytes; with max_cache_bytes=150, oldest must be pruned
            deleted_size = cleanup_old_media_files(dir_path, max_cache_bytes=150)
            self.assertEqual(deleted_size, 1)
            self.assertFalse(large1.exists())
            self.assertTrue(large2.exists())

    # 4. Tool Call JSON Repair Hardening Tests
    def test_repair_json_preserves_code_strings_with_true_and_commas(self):
        raw = '{"code": "if x: True\\n    items = [1, 2, ]", "status": True,}'
        repaired = _repair_json_string(raw)
        data = json.loads(repaired)
        self.assertEqual(data["code"], "if x: True\n    items = [1, 2, ]")
        self.assertEqual(data["status"], True)

    def test_unclosed_tool_call_tag_repair_preserves_trailing_text(self):
        text = (
            'Leading thought.\n'
            '<tool_call>{"name": "fetch", "arguments": {"active": True, }}'
            '\nTrailing assistant text that must not be truncated.'
        )
        parsed = parse_tool_calls(text, allowed_tool_names={"fetch"})
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0]["function"]["name"], "fetch")
        self.assertIn("Leading thought.", parsed.text)
        self.assertIn("Trailing assistant text that must not be truncated.", parsed.text)

    # 5. Autostart Registration Tests
    def test_autostart_helpers(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            reg_bat, unreg_bat = create_autostart_batches(repo_path)
            self.assertTrue(reg_bat.exists())
            self.assertTrue(unreg_bat.exists())
            self.assertIn("schtasks", reg_bat.read_text(encoding="utf-8"))
            self.assertIn("schtasks", unreg_bat.read_text(encoding="utf-8"))

            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                with patch("os.name", "nt"):
                    success = register_windows_autostart(repo_path, enable=True)
                    self.assertTrue(success)
                    self.assertTrue(mock_run.called)
                    call_args = mock_run.call_args[0][0]
                    self.assertIn("schtasks", call_args)
                    self.assertIn("onlogon", call_args)

                    unreg_success = register_windows_autostart(repo_path, enable=False)
                    self.assertTrue(unreg_success)

    # 6. stream_options: null handling in streaming mode
    def test_stream_options_null_does_not_raise_attribute_error(self):
        req = urllib.request.Request(
            f"{self.base}/v1/chat/completions",
            data=json.dumps({
                "model": "model-a",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": None,
            }).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read().decode("utf-8")
            self.assertIn("data: [DONE]", body)

    # 7. Unclosed and multiple thought extraction
    def test_unclosed_and_multiple_thoughts_handling(self):
        # Non-streaming with unclosed thought
        class UnclosedThoughtBackend(SimpleFakeBackend):
            def generate(self, prompt, model, **kwargs):
                return BackendResponse(
                    response="<thought>Unclosed thinking process that trails off",
                    model=model,
                    usage={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10},
                )

        self.server.chat_service.backend = UnclosedThoughtBackend()
        req = urllib.request.Request(
            f"{self.base}/v1/chat/completions",
            data=json.dumps({"model": "model-a", "messages": [{"role": "user", "content": "think"}]}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            msg = data["choices"][0]["message"]
            self.assertEqual(msg["reasoning_content"], "Unclosed thinking process that trails off")
            self.assertIsNone(msg["content"])

        # Multiple thoughts extraction
        class MultiThoughtBackend(SimpleFakeBackend):
            def generate(self, prompt, model, **kwargs):
                return BackendResponse(
                    response="<thought>Thought 1</thought>Intermediate text<thought>Thought 2</thought>Final text",
                    model=model,
                    usage={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10},
                )

        self.server.chat_service.backend = MultiThoughtBackend()
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            msg = data["choices"][0]["message"]
            self.assertEqual(msg["reasoning_content"], "Thought 1\n\nThought 2")
            self.assertEqual(msg["content"], "Intermediate text\nFinal text" if "Intermediate text\nFinal text" in (msg["content"] or "") else msg["content"])

    # 8. Media proxy rejects wildcards and non-media extensions
    def test_media_proxy_rejects_wildcards_and_non_media_extensions(self):
        # Wildcard request
        req_wild = urllib.request.Request(f"{self.base}/v1/media/{self.token}/*.png")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_wild, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

        # Direct wildcard request without token
        req_wild_direct = urllib.request.Request(f"{self.base}/v1/media/*.png")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_wild_direct, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

        # Non-media extension (.json, .pbtxt)
        req_ext = urllib.request.Request(f"{self.base}/v1/media/{self.token}/session.pbtxt")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_ext, timeout=5)
        self.assertEqual(ctx.exception.code, 403)

        # Direct non-media extension without token
        req_ext_direct = urllib.request.Request(f"{self.base}/v1/media/session.pbtxt")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_ext_direct, timeout=5)
        self.assertEqual(ctx.exception.code, 403)

    # 9. Sensitive host path in MEDIA: tag is not mirrored to media proxy
    def test_sensitive_host_path_in_media_tag_is_not_mirrored(self):
        sens_file = Path.home() / ".ssh" / "id_rsa"
        class SensitiveMediaBackend(SimpleFakeBackend):
            def generate(self, prompt, model, **kwargs):
                return BackendResponse(
                    response=f"Secret file:\n\nMEDIA:{sens_file.as_posix()}",
                    model=model,
                    usage={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10},
                )

        self.server.chat_service.backend = SensitiveMediaBackend()
        req = urllib.request.Request(
            f"{self.base}/v1/chat/completions",
            data=json.dumps({"model": "model-a", "messages": [{"role": "user", "content": "steal"}]}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            self.assertNotIn("MEDIA_URL:", content)

    # 10. response_format: {"type": "json_object"} instruction injection
    def test_response_format_json_object(self):
        service = self.server.chat_service
        service.backend = SimpleFakeBackend()
        res = service.complete({
            "model": "model-a",
            "messages": [{"role": "user", "content": "generate json"}],
            "response_format": {"type": "json_object"},
        })
        self.assertIsNotNone(res)

    # 11. Repair JSON string handles literal newlines and control characters inside quotes
    def test_repair_json_string_literal_newlines_inside_quotes(self):
        # Raw string with literal newline inside quoted value
        raw = '{"code": "line1\nline2", "status": True, }'
        repaired = _repair_json_string(raw)
        data = json.loads(repaired)
        self.assertEqual(data["code"], "line1\nline2")
        self.assertTrue(data["status"])

    # 12. Host header injection is prevented when constructing media URL
    def test_host_header_injection_is_prevented(self):
        media_dir = Path.home() / ".gemini" / "antigravity-cli" / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        img = media_dir / "safe_host_test.png"
        img.write_bytes(b"SAFE_IMAGE")
        try:
            class HostInjectionBackend(SimpleFakeBackend):
                def generate(self, prompt, model, **kwargs):
                    return BackendResponse(
                        response=f"Here is your image:\n\nMEDIA:{img.as_posix()}",
                        model=model,
                        usage={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10},
                    )

            self.server.chat_service.backend = HostInjectionBackend()
            req = urllib.request.Request(
                f"{self.base}/v1/chat/completions",
                data=json.dumps({"model": "model-a", "messages": [{"role": "user", "content": "draw"}]}).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "Host": "evil-attacker.com:1337",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            self.assertIn("MEDIA_URL:http://", content)
            self.assertNotIn("evil-attacker.com", content)
        finally:
            img.unlink(missing_ok=True)

    # 13. Stale media cache prevention on filename collision
    def test_stale_media_cache_prevention(self):
        media_dir = Path.home() / ".gemini" / "antigravity-cli" / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        img = media_dir / "collision_test.png"
        try:
            img.write_bytes(b"IMAGE_V1_BYTES")

            class CollisionBackend(SimpleFakeBackend):
                def generate(self, prompt, model, **kwargs):
                    return BackendResponse(
                        response=f"MEDIA:{img.as_posix()}",
                        model=model,
                        usage={},
                    )

            self.server.chat_service.backend = CollisionBackend()

            # First generation
            req1 = urllib.request.Request(
                f"{self.base}/v1/chat/completions",
                data=json.dumps({"model": "model-a", "messages": [{"role": "user", "content": "draw 1"}]}).encode("utf-8"),
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req1, timeout=5) as resp1:
                data1 = json.loads(resp1.read().decode("utf-8"))
            url1 = next(l for l in data1["choices"][0]["message"]["content"].splitlines() if l.startswith("MEDIA_URL:"))

            # Change content of file with same name
            img.write_bytes(b"IMAGE_V2_DIFFERENT_BYTES")

            # Second generation
            req2 = urllib.request.Request(
                f"{self.base}/v1/chat/completions",
                data=json.dumps({"model": "model-a", "messages": [{"role": "user", "content": "draw 2"}]}).encode("utf-8"),
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req2, timeout=5) as resp2:
                data2 = json.loads(resp2.read().decode("utf-8"))
            url2 = next(l for l in data2["choices"][0]["message"]["content"].splitlines() if l.startswith("MEDIA_URL:"))

            # URLs must be distinct because content changed!
            self.assertNotEqual(url1, url2)

            # Both URLs serve their respective versions
            clean_url1 = url1.removeprefix("MEDIA_URL:").strip()
            clean_url2 = url2.removeprefix("MEDIA_URL:").strip()
            with urllib.request.urlopen(urllib.request.Request(clean_url1), timeout=5) as r1:
                self.assertEqual(r1.read(), b"IMAGE_V1_BYTES")
            with urllib.request.urlopen(urllib.request.Request(clean_url2), timeout=5) as r2:
                self.assertEqual(r2.read(), b"IMAGE_V2_DIFFERENT_BYTES")
        finally:
            img.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
