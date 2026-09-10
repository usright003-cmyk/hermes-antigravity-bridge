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


_SLOT_ACQUIRE_TIMEOUT_SECONDS: float = 0.05


class BridgeMetrics:
    """Thread-safe in-memory telemetry counters for diagnostic monitoring."""

    def __init__(self) -> None:
        self.start_time: float = time.time()
        self._lock = threading.Lock()
        self.total_requests: int = 0
        self.streaming_requests: int = 0
        self.tokens_streamed: int = 0
        self.error_count: int = 0

    def record_request(self, *, streaming: bool = False) -> None:
        with self._lock:
            self.total_requests += 1
            if streaming:
                self.streaming_requests += 1

    def record_tokens(self, count: int = 1) -> None:
        with self._lock:
            self.tokens_streamed += count

    def record_error(self) -> None:
        with self._lock:
            self.error_count += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            uptime = int(time.time() - self.start_time)
            return {
                "uptime_seconds": uptime,
                "total_requests": self.total_requests,
                "streaming_requests": self.streaming_requests,
                "tokens_streamed": self.tokens_streamed,
                "error_count": self.error_count,
            }


_DASHBOARD_HTML: bytes = b"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Antigravity Bridge - Dashboard</title>
<style>
  :root {
    --bg: #0d1117;
    --card-bg: #161b22;
    --border: #30363d;
    --text: #c9d1d9;
    --text-bright: #f0f6fc;
    --accent: #58a6ff;
    --green: #3fb950;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background-color: var(--bg);
    color: var(--text);
    padding: 24px;
    line-height: 1.5;
  }
  .container { max-width: 900px; margin: 0 auto; }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--border);
    padding-bottom: 16px;
    margin-bottom: 24px;
    flex-wrap: wrap;
    gap: 12px;
  }
  .title-group { display: flex; align-items: center; gap: 12px; }
  .logo { font-size: 24px; }
  h1 { font-size: 20px; font-weight: 600; color: var(--text-bright); }
  .badges { display: flex; gap: 8px; align-items: center; }
  .badge {
    font-size: 12px;
    padding: 3px 8px;
    border-radius: 12px;
    font-weight: 500;
    border: 1px solid var(--border);
    background: var(--card-bg);
  }
  .badge-live {
    background: rgba(63, 185, 80, 0.15);
    color: var(--green);
    border-color: rgba(63, 185, 80, 0.4);
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }
  .card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .card-title { font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; color: #8b949e; }
  .card-value { font-size: 26px; font-weight: 700; color: var(--text-bright); }
  .panel {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 24px;
  }
  .panel h2 { font-size: 15px; margin-bottom: 12px; color: var(--text-bright); }
  .model-list { list-style: none; display: flex; flex-wrap: wrap; gap: 8px; }
  .model-item {
    background: rgba(88, 166, 255, 0.1);
    color: var(--accent);
    border: 1px solid rgba(88, 166, 255, 0.3);
    padding: 6px 12px;
    border-radius: 6px;
    font-family: monospace;
    font-size: 13px;
  }
  .security-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 12px;
  }
  .sec-item {
    border: 1px solid var(--border);
    background: rgba(255, 255, 255, 0.02);
    padding: 12px;
    border-radius: 6px;
  }
  .sec-item strong { color: var(--text-bright); font-size: 13px; display: block; margin-bottom: 4px; }
  .sec-item span { font-size: 12px; color: #8b949e; }
  footer {
    text-align: center;
    font-size: 12px;
    color: #8b949e;
    margin-top: 32px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
  }
  footer a { color: var(--accent); text-decoration: none; }
</style>
</head>
<body>
<div class="container">
  <header>
    <div class="title-group">
      <span class="logo">&#9889;</span>
      <div>
        <h1>Hermes Antigravity Bridge</h1>
        <div style="font-size: 12px; color: #8b949e;">Local OpenAI Compatibility Gateway</div>
      </div>
    </div>
    <div class="badges">
      <span class="badge" id="version-badge">v0.1.0</span>
      <span class="badge badge-live" id="status-badge">&#9679; ONLINE</span>
    </div>
  </header>

  <div class="grid">
    <div class="card">
      <div class="card-title">&#9201; Uptime</div>
      <div class="card-value" id="val-uptime">0s</div>
    </div>
    <div class="card">
      <div class="card-title">&#128260; Total Requests</div>
      <div class="card-value" id="val-requests">0</div>
    </div>
    <div class="card">
      <div class="card-title">&#128424; Typewriter Streams</div>
      <div class="card-value" id="val-streams">0</div>
    </div>
    <div class="card">
      <div class="card-title">&#9889; Tokens Streamed</div>
      <div class="card-value" id="val-tokens">0</div>
    </div>
  </div>

  <div class="panel">
    <h2>&#129302; Discovered Models</h2>
    <ul class="model-list" id="models-container">
      <li class="model-item">Scanning backend...</li>
    </ul>
  </div>

  <div class="panel">
    <h2>&#128737;&#65039; Sovereign Architecture & Safety Gates</h2>
    <div class="security-grid">
      <div class="sec-item">
        <strong>&#128274; Tool Isolation</strong>
        <span>Strict Fail-Closed (--sandbox --mode plan). Autonomous host execution blocked.</span>
      </div>
      <div class="sec-item">
        <strong>&#129504; Memory Custody</strong>
        <span>Hermes retains 100% ownership of MEMORY.md and session context.</span>
      </div>
      <div class="sec-item">
        <strong>&#128207; Context Budget</strong>
        <span>Enforces 4,000,000 character maximum prompt cap (1M Tokens).</span>
      </div>
      <div class="sec-item">
        <strong>&#9889; Low-Latency Transport</strong>
        <span>TCP_NODELAY active on loopback socket connections.</span>
      </div>
    </div>
  </div>

  <footer>
    Hermes Antigravity Bridge &bull; <a href="https://pypi.org/project/hermes-antigravity-bridge/" target="_blank">PyPI</a> &bull; <a href="https://github.com/usright003-cmyk/hermes-antigravity-bridge" target="_blank">GitHub</a>
  </footer>
</div>

<script>
function formatUptime(sec) {
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (d > 0) return d + "d " + h + "h " + m + "m";
  if (h > 0) return h + "h " + m + "m " + s + "s";
  if (m > 0) return m + "m " + s + "s";
  return s + "s";
}
async function pollMetrics() {
  try {
    const res = await fetch('/api/metrics');
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    document.getElementById('version-badge').textContent = 'v' + data.version;
    document.getElementById('val-uptime').textContent = formatUptime(data.metrics.uptime_seconds);
    document.getElementById('val-requests').textContent = data.metrics.total_requests;
    document.getElementById('val-streams').textContent = data.metrics.streaming_requests;
    document.getElementById('val-tokens').textContent = data.metrics.tokens_streamed;
    
    const container = document.getElementById('models-container');
    container.innerHTML = '';
    if (data.models && data.models.length > 0) {
      data.models.forEach(m => {
        const li = document.createElement('li');
        li.className = 'model-item';
        li.textContent = m;
        container.appendChild(li);
      });
    } else {
      const li = document.createElement('li');
      li.className = 'model-item';
      li.textContent = 'None detected (check agy)';
      container.appendChild(li);
    }
  } catch (err) {
    const sb = document.getElementById('status-badge');
    sb.textContent = '\\u25CF OFFLINE';
    sb.style.color = '#f85149';
    sb.style.borderColor = '#f85149';
  }
}
pollMetrics();
setInterval(pollMetrics, 2500);
</script>
</body>
</html>
"""


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
        self.metrics = BridgeMetrics()
        self.request_slots = threading.BoundedSemaphore(
            config.server.max_concurrent_requests
        )
        super().__init__(address, BridgeRequestHandler)

    def get_request(self) -> tuple[Any, Any]:
        sock, addr = super().get_request()
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        return sock, addr

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.request_slots.acquire(timeout=_SLOT_ACQUIRE_TIMEOUT_SECONDS):
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
        slot_released = False
        try:
            try:
                self.finish_request(request, client_address)
            finally:
                self.request_slots.release()
                slot_released = True
        except Exception:  # noqa: BLE001 - preserve socketserver error handling
            self.handle_error(request, client_address)
        finally:
            if not slot_released:
                self.request_slots.release()
            self.shutdown_request(request)


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
        self.bridge_server.metrics.record_error()
        self._send(status, {"error": {"message": message, "type": error_type}})

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.bridge_server.bridge_config.server.token}"
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if 0 < length <= 65536:
                self.rfile.read(length)
        except (OSError, ValueError):
            _LOG.debug("Failed to drain unread unauthorized request body")
        self._error(401, "unauthorized", "auth_error")
        return False

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in {"/dashboard", "/"}:
            self._send(200, _DASHBOARD_HTML, content_type="text/html; charset=utf-8")
            return
        if path == "/api/metrics":
            stats = self.bridge_server.metrics.snapshot()
            backend = getattr(self.bridge_server.chat_service, "backend", None)
            model_source = getattr(backend, "model_source", "unknown")
            is_auth_func = getattr(backend, "is_authenticated", None)
            authenticated = is_auth_func() if callable(is_auth_func) else True
            try:
                models = self.bridge_server.chat_service.list_models()
            except Exception:  # noqa: BLE001
                models = []
            self._send(
                200,
                {
                    "service": "hermes-antigravity-bridge",
                    "version": __version__,
                    "status": "online" if authenticated else "degraded",
                    "authenticated": authenticated,
                    "model_source": model_source,
                    "models": models,
                    "metrics": stats,
                    "config": {
                        "max_prompt_chars": self.bridge_server.bridge_config.prompt.max_chars,
                        "max_concurrent_requests": self.bridge_server.bridge_config.server.max_concurrent_requests,
                        "host": self.bridge_server.bridge_config.server.host,
                        "port": self.bridge_server.bridge_config.server.port,
                    },
                },
            )
            return
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
        if bool(body.get("stream")):
            self._stream_response(body)
            return

        try:
            result = self.bridge_server.chat_service.complete(body)
            response = self._completion_response(result)
            self.bridge_server.metrics.record_request(streaming=False)
            self._send(200, response)
        except BridgeError as exc:
            self._error(exc.status_code, _safe_client_message(exc), exc.error_type)
        except Exception:  # noqa: BLE001 - sanitize unexpected failures at the HTTP boundary
            _LOG.exception("unexpected bridge request failure")
            self._error(500, "internal bridge error", "internal_error")

    def _stream_response(self, body: Any) -> None:
        try:
            stream_iter = self.bridge_server.chat_service.complete_stream(body)
            first_item = next(stream_iter, None)
        except BridgeError as exc:
            self._error(exc.status_code, _safe_client_message(exc), exc.error_type)
            return
        except Exception:  # noqa: BLE001
            _LOG.exception("unexpected error before stream start")
            self._error(500, "internal bridge error", "internal_error")
            return

        stream_id = "chatcmpl-" + uuid.uuid4().hex
        now = int(time.time())
        self.bridge_server.metrics.record_request(streaming=True)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def emit_item(item: dict[str, Any]) -> None:
                itype = item.get("type")
                if itype == "delta":
                    self.bridge_server.metrics.record_tokens(1)
                    chunk = {
                        "id": stream_id,
                        "object": "chat.completion.chunk",
                        "created": now,
                        "model": item.get("requested_model", ""),
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": item["content"], "role": "assistant"},
                                "finish_reason": None,
                            }
                        ],
                    }
                    self.wfile.write(b"data: " + _json_bytes(chunk) + b"\n\n")
                    self.wfile.flush()
                elif itype == "tool_calls":
                    chunk = {
                        "id": stream_id,
                        "object": "chat.completion.chunk",
                        "created": now,
                        "model": item.get("requested_model", ""),
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"tool_calls": item["tool_calls"]},
                                "finish_reason": None,
                            }
                        ],
                    }
                    self.wfile.write(b"data: " + _json_bytes(chunk) + b"\n\n")
                    self.wfile.flush()
                elif itype == "finish":
                    chunk = {
                        "id": stream_id,
                        "object": "chat.completion.chunk",
                        "created": now,
                        "model": item.get("requested_model", ""),
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": item.get("finish_reason", "stop"),
                            }
                        ],
                    }
                    if item.get("x_bridge_error"):
                        chunk["x_bridge_error"] = item["x_bridge_error"]
                    if item.get("usage") and body.get("stream_options", {}).get("include_usage"):
                        chunk["usage"] = item["usage"]
                    self.wfile.write(b"data: " + _json_bytes(chunk) + b"\n\n")
                    self.wfile.flush()

            if first_item is not None:
                emit_item(first_item)
                for item in stream_iter:
                    emit_item(item)

            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            _LOG.info("client disconnected during stream")
        except BridgeError as exc:
            _LOG.warning("bridge error during active stream: %s", exc)
            try:
                fallback_text = f"[Bridge Warning: Stream degraded - {_safe_client_message(exc)}]"
                text_chunk = {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": now,
                    "model": "",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": fallback_text},
                            "finish_reason": None,
                        }
                    ],
                }
                self.wfile.write(b"data: " + _json_bytes(text_chunk) + b"\n\n")
                err_chunk = {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": now,
                    "model": "",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                    "error": {
                        "message": _safe_client_message(exc),
                        "type": exc.error_type,
                    },
                    "x_bridge_error": {
                        "type": exc.error_type,
                        "message": _safe_client_message(exc),
                    },
                }
                self.wfile.write(b"data: " + _json_bytes(err_chunk) + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (OSError, RuntimeError) as write_err:
                _LOG.debug("Could not flush SSE error event: %s", write_err)
        except Exception:  # noqa: BLE001
            _LOG.exception("unexpected error during active stream")
            try:
                fallback_text = "[Bridge Error: Unexpected backend stream failure]"
                text_chunk = {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": now,
                    "model": "",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": fallback_text},
                            "finish_reason": None,
                        }
                    ],
                }
                self.wfile.write(b"data: " + _json_bytes(text_chunk) + b"\n\n")
                err_chunk = {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": now,
                    "model": "",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                    "error": {
                        "message": "internal bridge stream error",
                        "type": "internal_error",
                    },
                    "x_bridge_error": {
                        "type": "internal_error",
                        "message": "internal bridge stream error",
                    },
                }
                self.wfile.write(b"data: " + _json_bytes(err_chunk) + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (OSError, RuntimeError) as write_err:
                _LOG.debug("Could not flush SSE error event: %s", write_err)

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
