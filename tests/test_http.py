import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from hermes_antigravity_bridge.config import (
    AntigravityConfig,
    BridgeConfig,
    ServerConfig,
)
from hermes_antigravity_bridge.contracts import BackendResponse
from hermes_antigravity_bridge.errors import BackendProtocolError, UnknownModel
from hermes_antigravity_bridge.integrations.hermes import HermesPromptBuilder
from hermes_antigravity_bridge.openai_http import create_http_server
from hermes_antigravity_bridge.prompt.budget import PromptBudget
from hermes_antigravity_bridge.service import ChatCompletionService

TEST_TOKEN = "test-" + ("x" * 32)


class FakeBackend:
    def list_models(self, *, force_refresh=False):
        return ("model-a", "model-b")

    def resolve_model(self, requested):
        if requested not in self.list_models():
            raise UnknownModel(f"unknown model: {requested}")
        return requested

    def generate(self, prompt, model):
        return BackendResponse(
            response="HTTP_OK",
            model=model,
            usage={"input_tokens": 8, "output_tokens": 2, "total_tokens": 10},
        )

    def readiness(self):
        return {"status": "ready", "models": list(self.list_models())}


class BlockingBackend(FakeBackend):
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate(self, prompt, model):
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test release timeout")
        return super().generate(prompt, model)


class HTTPContractTests(unittest.TestCase):
    def setUp(self):
        token = TEST_TOKEN
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
            backend=FakeBackend(),
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=config.prompt,
        )
        self.token = token
        self.server = create_http_server(config, service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        path,
        *,
        method="GET",
        body=None,
        auth=False,
        content_type="application/json",
    ):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {}
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        if data is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()
        except (ConnectionResetError, ConnectionAbortedError):
            return 401, {}, b""

    def test_health_is_public_but_readiness_and_models_are_authenticated(self):
        status, _, body = self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

        self.assertEqual(self.request("/ready")[0], 401)
        self.assertEqual(self.request("/v1/models")[0], 401)
        ready_status, _, ready_body = self.request("/ready", auth=True)
        self.assertEqual(ready_status, 200)
        self.assertEqual(json.loads(ready_body)["status"], "ready")
        models_status, _, models_body = self.request("/v1/models", auth=True)
        self.assertEqual(models_status, 200)
        self.assertEqual(
            [item["id"] for item in json.loads(models_body)["data"]],
            ["model-a", "model-b"],
        )

    def test_dashboard_and_metrics_local_access(self):
        status, _, body = self.request("/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("Hermes Antigravity Bridge", body.decode("utf-8"))

        status, _, body = self.request("/dashboard/")
        self.assertEqual(status, 200)
        self.assertIn("Hermes Antigravity Bridge", body.decode("utf-8"))

        status, _, body = self.request("/")
        self.assertEqual(status, 200)

        status, _, body = self.request("/api/metrics")
        self.assertEqual(status, 200)
        metrics_data = json.loads(body)
        self.assertEqual(metrics_data["status"], "online")
        self.assertIn("metrics", metrics_data)

    def test_dashboard_and_metrics_remote_access_authentication(self):
        object.__setattr__(self.server.bridge_config.server, "allow_remote", True)
        try:
            # Unauthenticated requests are rejected with 401
            for path in ("/dashboard", "/dashboard/", "/", "/api/metrics"):
                with self.subTest(path=path, auth="none"):
                    status, _, body = self.request(path)
                    self.assertEqual(status, 401)
                    self.assertEqual(json.loads(body)["error"]["type"], "auth_error")

            # Bearer token succeeds
            for path in ("/dashboard", "/dashboard/", "/", "/api/metrics"):
                with self.subTest(path=path, auth="bearer"):
                    status, _, _ = self.request(path, auth=True)
                    self.assertEqual(status, 200)

            # Query parameter ?token= succeeds
            for path in ("/dashboard", "/dashboard/", "/", "/api/metrics"):
                with self.subTest(path=path, auth="query_token"):
                    status, _, _ = self.request(f"{path}?token={self.token}")
                    self.assertEqual(status, 200)

            # Invalid query token is rejected
            for path in ("/dashboard", "/dashboard/", "/", "/api/metrics"):
                with self.subTest(path=path, auth="invalid_query_token"):
                    status, _, _ = self.request(f"{path}?token=bad-token")
                    self.assertEqual(status, 401)
        finally:
            object.__setattr__(self.server.bridge_config.server, "allow_remote", False)

    def test_non_loopback_ip_requires_auth_even_if_allow_remote_is_false(self):
        from unittest.mock import patch
        self.assertFalse(self.server.bridge_config.server.allow_remote)
        with patch("hermes_antigravity_bridge.openai_http._is_loopback_ip", return_value=False):
            status, _, body = self.request("/dashboard")
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body)["error"]["type"], "auth_error")

            status, _, _ = self.request(f"/dashboard?token={self.token}")
            self.assertEqual(status, 200)

    def test_is_loopback_ip_utility(self):
        from hermes_antigravity_bridge.openai_http import _is_loopback_ip
        self.assertTrue(_is_loopback_ip("127.0.0.1"))
        self.assertTrue(_is_loopback_ip("localhost"))
        self.assertTrue(_is_loopback_ip("::1"))
        self.assertTrue(_is_loopback_ip(""))
        self.assertFalse(_is_loopback_ip("192.168.1.150"))
        self.assertFalse(_is_loopback_ip("10.0.0.2"))
        self.assertFalse(_is_loopback_ip("not-an-ip"))

    def test_metrics_evaluates_model_source_after_discovery(self):
        class DynamicFallbackBackend(FakeBackend):
            def __init__(self):
                self._source = "unknown"

            @property
            def model_source(self):
                return self._source

            def list_models(self, force_refresh=False):
                self._source = "fallback"
                return ("model-a",)

        orig_backend = self.server.chat_service.backend
        self.server.chat_service.backend = DynamicFallbackBackend()
        try:
            status, _, body = self.request("/api/metrics")
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertEqual(data["model_source"], "fallback")
            self.assertEqual(data["status"], "degraded")
        finally:
            self.server.chat_service.backend = orig_backend

    def test_metrics_and_ready_report_degraded_when_fallback_or_unauthenticated(self):
        backend = self.server.chat_service.backend
        # Simulate fallback model source
        backend.model_source = "fallback"
        try:
            status, _, body = self.request("/api/metrics")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["status"], "degraded")

            status, _, body = self.request("/ready", auth=True)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["status"], "degraded")
        finally:
            delattr(backend, "model_source")

        # Simulate unauthenticated backend
        backend.is_authenticated = lambda: False
        try:
            status, _, body = self.request("/api/metrics")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["status"], "degraded")

            status, _, body = self.request("/ready", auth=True)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["status"], "degraded")
        finally:
            delattr(backend, "is_authenticated")

    def test_hermes_model_discovery_compatibility_routes(self):
        for path in ("/api/v1/models", "/api/tags", "/version", "/api/version"):
            with self.subTest(path=path):
                status, _, _ = self.request(path, auth=True)
                self.assertEqual(status, 200)
        status, _, body = self.request(
            "/api/show",
            method="POST",
            auth=True,
            body={"model": "model-a"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["model"], "model-a")

    def test_models_route_and_loopback_discovery(self):
        # 1. /models unauthenticated succeeds on loopback
        status, _, body = self.request("/models")
        self.assertEqual(status, 200)
        parsed = json.loads(body)
        self.assertIn("data", parsed)
        self.assertIn("models", parsed)
        self.assertEqual([m["id"] for m in parsed["data"]], ["model-a", "model-b"])
        self.assertEqual([m["name"] for m in parsed["models"]], ["model-a", "model-b"])

        # 2. /models authenticated also succeeds
        status, _, _ = self.request("/models", auth=True)
        self.assertEqual(status, 200)

        # 3. /v1/models response shape contains both data and models fields
        status, _, v1_body = self.request("/v1/models", auth=True)
        self.assertEqual(status, 200)
        v1_parsed = json.loads(v1_body)
        self.assertIn("data", v1_parsed)
        self.assertIn("models", v1_parsed)

        # 4. /models/<id> model detail and 404 behavior
        self.assertEqual(self.request("/models/model-a")[0], 200)
        status, _, body = self.request("/models/not-real")
        self.assertEqual(status, 404)
        self.assertIn("unknown model", json.loads(body)["error"]["message"])

    def test_model_detail_rejects_unknown_ids(self):
        self.assertEqual(self.request("/v1/models/model-a", auth=True)[0], 200)
        status, _, body = self.request("/v1/models/not-real", auth=True)
        self.assertEqual(status, 404)
        self.assertIn("unknown model", json.loads(body)["error"]["message"])

    def test_nonstream_chat_completion(self):
        status, headers, body = self.request(
            "/v1/chat/completions",
            method="POST",
            auth=True,
            body={
                "model": "model-a",
                "messages": [{"role": "user", "content": "latest"}],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "application/json")
        payload = json.loads(body)
        self.assertEqual(payload["choices"][0]["message"]["content"], "HTTP_OK")
        self.assertEqual(payload["x_antigravity_model"], "model-a")
        self.assertEqual(payload["usage"]["total_tokens"], 10)

    def test_buffered_sse_contract(self):
        status, headers, body = self.request(
            "/v1/chat/completions",
            method="POST",
            auth=True,
            body={
                "model": "model-a",
                "stream": True,
                "messages": [{"role": "user", "content": "latest"}],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "text/event-stream")
        text = body.decode("utf-8")
        self.assertIn("HTTP_OK", text)
        self.assertTrue(text.endswith("data: [DONE]\n\n"))

    def test_rejects_missing_auth_wrong_content_type_and_large_body(self):
        payload = {
            "model": "model-a",
            "messages": [{"role": "user", "content": "latest"}],
        }
        self.assertEqual(
            self.request("/v1/chat/completions", method="POST", body=payload)[0], 401
        )
        self.assertEqual(
            self.request(
                "/v1/chat/completions",
                method="POST",
                auth=True,
                body=payload,
                content_type="text/plain",
            )[0],
            415,
        )
        large = {
            "model": "model-a",
            "messages": [{"role": "user", "content": "x" * 5000}],
        }
        self.assertEqual(
            self.request("/v1/chat/completions", method="POST", auth=True, body=large)[
                0
            ],
            413,
        )

    def test_chat_completions_rejects_non_dict_json(self):
        for invalid_body in ([], "just a string", 123, True):
            with self.subTest(invalid_body=invalid_body):
                status, _, body = self.request(
                    "/v1/chat/completions",
                    method="POST",
                    auth=True,
                    body=invalid_body,
                )
                self.assertEqual(status, 400)
                parsed = json.loads(body)
                self.assertIn("request body must be a JSON object", parsed["error"]["message"])
                self.assertEqual(parsed["error"]["type"], "invalid_request_error")

    def test_concurrency_limit_returns_429_without_starting_a_second_backend_call(self):
        token = TEST_TOKEN
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
        backend = BlockingBackend()
        service = ChatCompletionService(
            backend=backend,
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=config.prompt,
        )
        server = create_http_server(config, service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
        body = json.dumps(
            {
                "model": "model-a",
                "messages": [{"role": "user", "content": "latest"}],
            }
        ).encode("utf-8")

        def post():
            request = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status
            except urllib.error.HTTPError as exc:
                return exc.code

        first_result = []
        first_thread = threading.Thread(target=lambda: first_result.append(post()))
        first_thread.start()
        self.assertTrue(backend.entered.wait(timeout=2))
        try:
            self.assertEqual(post(), 429)
        finally:
            backend.release.set()
            first_thread.join(timeout=5)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(first_result, [200])

    def test_rapid_sequential_requests_do_not_trigger_spurious_concurrency_429(self):
        # Regression test: rapid back-to-back requests on a max_concurrent_requests=1 server
        # must not race against worker thread socket cleanup and return spurious 429.
        statuses = []
        for _ in range(25):
            for path in ("/version", "/api/version", "/api/tags", "/health"):
                status, _, _ = self.request(path, auth=True)
                statuses.append(status)
        self.assertEqual(set(statuses), {200})
        self.assertEqual(len(statuses), 100)

    def test_dashboard_endpoint_returns_html(self):
        status, headers, body = self.request("/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        self.assertIn(b"Hermes Antigravity Bridge", body)
        self.assertIn(b"Dashboard", body)

    def test_api_metrics_endpoint_tracks_requests_and_tokens(self):
        status, _, body = self.request("/api/metrics")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["status"], "online")
        self.assertIn("metrics", data)
        initial_requests = data["metrics"]["total_requests"]

        # Run a completion
        comp_status, _, _ = self.request(
            "/v1/chat/completions",
            method="POST",
            auth=True,
            body={
                "model": "model-a",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        self.assertEqual(comp_status, 200)

        # Check metrics updated
        status, _, updated_body = self.request("/api/metrics")
        self.assertEqual(status, 200)
        updated_data = json.loads(updated_body.decode("utf-8"))
        self.assertEqual(
            updated_data["metrics"]["total_requests"], initial_requests + 1
        )
        self.assertGreaterEqual(updated_data["metrics"]["uptime_seconds"], 0)

    def test_streaming_error_cleanly_terminates_sse_stream_without_abrupt_disconnect(self):
        class BrokenStreamBackend(FakeBackend):
            def generate_stream(self, prompt, model, **kwargs):
                yield {"type": "delta", "content": "Hello "}
                raise BackendProtocolError("backend stream connection severed")

        token = TEST_TOKEN
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
            backend=BrokenStreamBackend(),
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=config.prompt,
        )
        server = create_http_server(config, service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
        body = json.dumps(
            {
                "model": "model-a",
                "messages": [{"role": "user", "content": "test"}],
                "stream": True,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                content = resp.read().decode("utf-8")
                self.assertIn("data: [DONE]", content)
                self.assertIn("Hello ", content)
                self.assertNotIn("[Bridge Warning:", content)
                self.assertIn('"finish_reason":"error"', content)
                self.assertIn('"error":', content)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_image_generations_endpoint(self):
        import base64
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(b"IMAGE_CONTENT_BYTES")
            tmp_path = tmp.name

        class ImageBackend(FakeBackend):
            def generate(self, prompt, model):
                return BackendResponse(
                    response=f"Here is your image\n\nMEDIA:{Path(tmp_path).as_posix()}",
                    model=model,
                    usage={},
                )

        token = TEST_TOKEN
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
            backend=ImageBackend(),
            prompt_builder=HermesPromptBuilder(),
            prompt_budget=config.prompt,
        )
        server = create_http_server(config, service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            # 1. URL response format
            req = urllib.request.Request(
                f"{base}/v1/images/generations",
                data=json.dumps({"prompt": "sunset over mountain"}).encode("utf-8"),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("data", data)
                self.assertTrue(data["data"][0]["url"].startswith("file:///"))

            # 2. b64_json response format
            req = urllib.request.Request(
                f"{base}/v1/images/generations",
                data=json.dumps({"prompt": "sunset", "response_format": "b64_json"}).encode("utf-8"),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                b64 = data["data"][0]["b64_json"]
                self.assertEqual(base64.b64decode(b64), b"IMAGE_CONTENT_BYTES")

            # 3. Bad request - empty prompt
            req = urllib.request.Request(
                f"{base}/v1/images/generations",
                data=json.dumps({"prompt": ""}).encode("utf-8"),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()

