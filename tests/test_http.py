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
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

