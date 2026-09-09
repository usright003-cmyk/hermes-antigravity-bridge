"""Authenticated OpenAI Chat Completions compatibility HTTP layer."""

from __future__ import annotations

import hmac
import json
import logging
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

from . import __version__
from .config import BridgeConfig
from .errors import BackendError, BridgeError
from .service import ChatCompletionService

_LOG = logging.getLogger(__name__)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _safe_client_message(error: BridgeError) -> str:
    if isinstance(error, BackendError):
        return "Antigravity backend request failed"
    return " ".join(str(error).split())[:300]


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        config: BridgeConfig,
        service: ChatCompletionService,
    ) -> None:
        self.bridge_config = config
        self.chat_service = service
        self.request_slots = threading.BoundedSemaphore(
            config.server.max_concurrent_requests
        )
        super().__init__(address, BridgeRequestHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.request_slots.acquire(blocking=False):
            body = _json_bytes(
                {
                    "error": {
                        "message": "bridge concurrency limit reached",
                        "type": "rate_limit_error",
                    }
                }
            )
            response = (
                b"HTTP/1.1 429 Too Many Requests\r\n"
                b"Content-Type: application/json\r\n"
                b"Connection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )
            try:
                request.sendall(response)
                try:
                    request.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                request.settimeout(0.2)
                try:
                    while request.recv(4096):
                        pass
                except OSError:
                    pass
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = f"hermes-antigravity-bridge/{__version__}"

    @property
    def bridge_server(self) -> BridgeHTTPServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args: Any) -> None:
        _LOG.info("http %s", fmt % args)

    def _send(
        self,
        status: int,
        payload: Any,
        content_type: str = "application/json",
    ) -> None:
        data = _json_bytes(payload) if content_type == "application/json" else payload
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            _LOG.info("client disconnected before response delivery")

    def _error(self, status: int, message: str, error_type: str) -> None:
        self._send(status, {"error": {"message": message, "type": error_type}})

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.bridge_server.bridge_config.server.token}"
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._error(401, "unauthorized", "auth_error")
        return False

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(
                200,
                {
                    "status": "ok",
                    "service": "hermes-antigravity-bridge",
                    "version": __version__,
                    "bridge_max_prompt_chars": self.bridge_server.bridge_config.prompt.max_chars,
                },
            )
            return
        if path == "/ready":
            if not self._require_auth():
                return
            try:
                self._send(200, self.bridge_server.chat_service.readiness())
            except BridgeError as exc:
                self._error(exc.status_code, _safe_client_message(exc), exc.error_type)
            return
        if path == "/v1/models":
            if not self._require_auth():
                return
            try:
                models = self.bridge_server.chat_service.list_models()
                now = int(time.time())
                self._send(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": model,
                                "object": "model",
                                "created": now,
                                "owned_by": "antigravity",
                            }
                            for model in models
                        ],
                    },
                )
            except BridgeError as exc:
                self._error(exc.status_code, _safe_client_message(exc), exc.error_type)
            return
        if path in {"/api/v1/models", "/api/tags"}:
            if not self._require_auth():
                return
            try:
                models = self.bridge_server.chat_service.list_models()
                self._send(
                    200,
                    {"models": [{"name": model, "model": model} for model in models]},
                )
            except BridgeError as exc:
                self._error(exc.status_code, _safe_client_message(exc), exc.error_type)
            return
        if path in {"/version", "/api/version"}:
            if not self._require_auth():
                return
            self._send(200, {"version": __version__})
            return
        if path.startswith("/v1/models/"):
            if not self._require_auth():
                return
            model = unquote(path[len("/v1/models/") :]).strip()
            try:
                models = self.bridge_server.chat_service.list_models()
            except BridgeError as exc:
                self._error(exc.status_code, _safe_client_message(exc), exc.error_type)
                return
            if not model or model not in models:
                self._error(404, f"unknown model: {model}", "not_found_error")
                return
            self._send(
                200,
                {
                    "id": model,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "antigravity",
                },
            )
            return
        self._error(404, "not found", "not_found_error")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/show":
            if not self._require_auth():
                return
            if self.headers.get_content_type() != "application/json":
                self._error(415, "Content-Type must be application/json", "invalid_request_error")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0:
                self._error(400, "request body is empty", "invalid_request_error")
                return
            if length > self.bridge_server.bridge_config.server.request_body_limit_bytes:
                self._error(413, "request body exceeds configured limit", "request_too_large")
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._error(400, "request body is not valid UTF-8 JSON", "invalid_request_error")
                return
            if not isinstance(payload, dict):
                self._error(400, "request body must be a JSON object", "invalid_request_error")
                return
            model = str(payload.get("model") or payload.get("name") or "").strip()
            try:
                models = self.bridge_server.chat_service.list_models()
            except BridgeError as exc:
                self._error(exc.status_code, _safe_client_message(exc), exc.error_type)
                return
            if not model or model not in models:
                self._error(404, f"unknown model: {model}", "not_found_error")
                return
            self._send(200, {"model": model, "name": model, "details": {"family": "antigravity"}})
            return
        if path != "/v1/chat/completions":
            self._error(404, "not found", "not_found_error")
            return
        if not self._require_auth():
            return
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            self._error(415, "Content-Type must be application/json", "invalid_request_error")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        limit = self.bridge_server.bridge_config.server.request_body_limit_bytes
        if length <= 0:
            self._error(400, "request body is empty", "invalid_request_error")
            return
        if length > limit:
            self._error(413, "request body exceeds configured limit", "request_too_large")
            return
        try:
            raw = self.rfile.read(length)
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error(400, "request body is not valid UTF-8 JSON", "invalid_request_error")
            return
        try:
            result = self.bridge_server.chat_service.complete(body)
            response = self._completion_response(result)
            if bool(body.get("stream")):
                self._send(200, self._sse_response(response), "text/event-stream")
            else:
                self._send(200, response)
        except BridgeError as exc:
            self._error(exc.status_code, _safe_client_message(exc), exc.error_type)
        except Exception:  # noqa: BLE001 - sanitize unexpected failures at the HTTP boundary
            _LOG.exception("unexpected bridge request failure")
            self._error(500, "internal bridge error", "internal_error")

    def _completion_response(self, result: Any) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": result.text if result.text else None,
        }
        if result.tool_calls:
            message["tool_calls"] = list(result.tool_calls)
        finish_reason = "tool_calls" if result.tool_calls else "stop"
        return {
            "id": "chatcmpl-" + uuid.uuid4().hex,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": result.requested_model,
            "choices": [
                {"index": 0, "message": message, "finish_reason": finish_reason}
            ],
            "usage": result.usage,
            "x_antigravity_model": result.actual_model,
        }

    def _sse_response(self, response: dict[str, Any]) -> bytes:
        choice = response["choices"][0]
        message = choice["message"]
        delta: dict[str, Any] = {
            "role": "assistant",
            "content": message.get("content"),
        }
        if message.get("tool_calls"):
            delta["tool_calls"] = message["tool_calls"]
        chunks = [
            {
                "id": response["id"],
                "object": "chat.completion.chunk",
                "created": response["created"],
                "model": response["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            },
            {
                "id": response["id"],
                "object": "chat.completion.chunk",
                "created": response["created"],
                "model": response["model"],
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": choice["finish_reason"],
                    }
                ],
            },
            {
                "id": response["id"],
                "object": "chat.completion.chunk",
                "created": response["created"],
                "model": response["model"],
                "choices": [],
                "usage": response["usage"],
            },
        ]
        data = "".join(
            "data: " + json.dumps(chunk, ensure_ascii=False, separators=(",", ":")) + "\n\n"
            for chunk in chunks
        )
        return (data + "data: [DONE]\n\n").encode("utf-8")


def create_http_server(
    config: BridgeConfig,
    service: ChatCompletionService,
) -> BridgeHTTPServer:
    return BridgeHTTPServer((config.server.host, config.server.port), config, service)
