"""Fail-closed adapter for the Antigravity CLI stream-json protocol."""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from ..config import AntigravityConfig
from ..contracts import BackendResponse
from ..errors import (
    BackendError,
    BackendProtocolError,
    BackendTimeout,
    BackendUnavailable,
    ToolIsolationError,
    UnknownModel,
)

_LOG = logging.getLogger(__name__)
_REQUIRED_FLAGS = {
    "--input-format",
    "--output-format",
    "--disable-slash-commands",
    "--sandbox",
    "--mode",
    "--print-timeout",
    "--model",
}
_TRANSIENT_MARKERS = (
    "503",
    "unavailable",
    "rate limit",
    "busy",
    "timeout",
    "temporar",
    "subscriber fell behind",
    "connection to the agent was interrupted",
)
_TOOL_EVENT_NAMES = {
    "tool",
    "tool_call",
    "tool-call",
    "tool_use",
    "tool-use",
    "command",
    "command_execution",
    "shell",
    "terminal",
    "file_change",
    "artifact",
}
_TOOL_KEYS = {
    "tool_call",
    "tool_calls",
    "tool_use",
    "tool_uses",
    "command_execution",
    "shell_command",
    "terminal_command",
    "file_change",
}


def parse_model_ids(output: str) -> tuple[str, ...]:
    ids: list[str] = []
    for line in (output or "").splitlines():
        parts = line.strip().split("\t", 1)
        model_id = parts[0].strip() if len(parts) == 2 else ""
        if model_id and model_id not in ids and not model_id.lower().startswith("fetching "):
            ids.append(model_id)
    return tuple(ids)


DEFAULT_ANTIGRAVITY_MODELS: tuple[str, ...] = (
    "gemini-3.8-flash",
    "gemini-3.8-flash-high",
    "gemini-3.7-flash",
    "gemini-3.7-flash-medium",
    "gemini-3.6-flash",
    "gemini-3.6-flash-medium",
    "gemini-3.1-pro",
    "gemini-3.1-pro-low",
    "claude-sonnet-4-6",
    "claude-sonnet-4.6",
    "claude-opus-4-6",
    "claude-opus-4.6",
    "gpt-oss-120b",
)


def is_false_success(result: Mapping[str, Any]) -> bool:
    if result.get("status") != "SUCCESS" or str(result.get("response") or "").strip():
        return False
    if "duration_seconds" not in result:
        return False
    try:
        if float(result.get("duration_seconds") or 0) != 0:
            return False
    except (TypeError, ValueError):
        return False
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return False
    keys = (
        "input_tokens",
        "output_tokens",
        "thinking_tokens",
        "cache_read_tokens",
        "total_tokens",
    )
    try:
        return all(int(usage.get(key, 0) or 0) == 0 for key in keys)
    except (TypeError, ValueError):
        return False


def event_indicates_internal_tool(event: Mapping[str, Any]) -> bool:
    event_name = str(event.get("event") or event.get("type") or "").strip().lower()
    if event_name in _TOOL_EVENT_NAMES:
        return True

    def contains_tool_key(value: Any) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in _TOOL_KEYS and item not in (None, False, "", [], {}):
                    return True
                if contains_tool_key(item):
                    return True
        elif isinstance(value, list):
            return any(contains_tool_key(item) for item in value)
        return False

    return contains_tool_key(event)


class AntigravityBackend:
    """One isolated, stateless Antigravity print invocation per request."""

    def __init__(self, config: AntigravityConfig) -> None:
        self.config = config
        self._cache_lock = threading.Lock()
        self._model_cache: tuple[float, tuple[str, ...]] = (0.0, ())

    def _base_environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env["HOME"] = str(self.config.home)
        env.setdefault("NO_COLOR", "1")
        return env

    def _ensure_runtime(self) -> Path:
        runtime = self.config.runtime_dir
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            runtime.chmod(0o700)
        except OSError:
            pass
        return runtime

    def verify_tool_isolation_settings(self, settings_path: Path) -> None:
        """Require Antigravity to deny autonomous internal tool execution."""
        try:
            payload = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolIsolationError(
                "Antigravity strict permission settings are required but unreadable"
            ) from exc
        permissions = payload.get("permissions")
        allow_rules = permissions.get("allow", []) if isinstance(permissions, dict) else []
        trusted = payload.get("trustedWorkspaces", [])
        if (
            payload.get("toolPermission") != "strict"
            or payload.get("artifactReviewPolicy") == "always-proceed"
            or allow_rules
            or trusted
        ):
            raise ToolIsolationError(
                "Antigravity strict tool isolation requires toolPermission='strict', "
                "no allow rules, no trusted workspaces, and no always-proceed artifact policy"
            )

    def _verify_if_required(self) -> None:
        if self.config.enforce_tool_isolation:
            self.verify_tool_isolation_settings(self.config.settings_file)

    def _binary_command_prefix(self) -> list[str]:
        binary_str = str(self.config.binary)
        if binary_str.lower().endswith(".py"):
            return [sys.executable, binary_str]
        return [binary_str]

    def build_command(self, model: str) -> list[str]:
        command = self._binary_command_prefix() + [
            "--input-format",
            "text",
            "--output-format",
            "stream-json",
            "--disable-slash-commands",
        ]
        if self.config.sandbox:
            command.append("--sandbox")

        effort: str | None = None
        base_model = model
        for suffix in ("-high", "-medium", "-low"):
            if base_model.endswith(suffix):
                effort = suffix[1:]
                base_model = base_model[: -len(suffix)]
                break

        if effort is None and "gemini" in base_model.lower():
            effort = "high"

        command.extend(
            [
                "--mode",
                self.config.mode,
                "--print-timeout",
                f"{self.config.timeout_seconds}s",
                "--model",
                base_model,
            ]
        )
        if effort is not None and "gemini" in base_model.lower():
            command.extend(["--effort", effort])
        return command

    def list_models(self, *, force_refresh: bool = False) -> tuple[str, ...]:
        now = time.monotonic()
        with self._cache_lock:
            cached_at, cached = self._model_cache
            if cached and not force_refresh and now - cached_at < self.config.model_cache_ttl_seconds:
                return cached
            runtime = self._ensure_runtime()
            try:
                completed = subprocess.run(
                    self._binary_command_prefix() + ["models"],
                    cwd=runtime,
                    env=self._base_environment(),
                    capture_output=True,
                    text=True,
                    timeout=min(self.config.timeout_seconds, 3),
                    check=False,
                )
                models = parse_model_ids(completed.stdout) if completed.returncode == 0 else ()
            except (OSError, subprocess.TimeoutExpired):
                models = ()
            if not models:
                models = DEFAULT_ANTIGRAVITY_MODELS
            self._model_cache = (now, models)
            return models

    def resolve_model(self, requested: str) -> str:
        requested = str(requested or "").strip()
        aliases = {
            # Default & Gemini family aliases
            "antigravity": self.config.default_model,
            "antigravity-pro": "gemini-3.1-pro-high",
            "antigravity-flash": "gemini-3.8-flash-medium",
            "antigravity-flash-fast": "gemini-3.8-flash-low",
            "pro": "gemini-3.1-pro-high",
            "flash": "gemini-3.8-flash-medium",
            "gemini-3.8-flash": "gemini-3.8-flash-medium",
            "gemini-3.7-flash": "gemini-3.7-flash-medium",
            "gemini-3.6-flash": "gemini-3.6-flash-medium",
            "gemini-3.1-pro": "gemini-3.1-pro-high",
            "gemini-pro": "gemini-3.1-pro-high",
            "gemini-flash": "gemini-3.8-flash-medium",
            "gemini 3.8 flash": "gemini-3.8-flash-high",
            "gemini 3.7 flash": "gemini-3.7-flash-medium",
            "gemini 3.6 flash": "gemini-3.6-flash-medium",
            "gemini 3.1 pro": "gemini-3.1-pro-low",
            # Anthropic Claude family aliases
            "claude-sonnet-4-6": "claude-sonnet-4-6",
            "claude-sonnet-4.6": "claude-sonnet-4-6",
            "claude sonnet 4.6 (thinking)": "claude-sonnet-4-6",
            "claude sonnet 4.6": "claude-sonnet-4-6",
            "claude-opus-4-6": "claude-opus-4-6",
            "claude-opus-4.6": "claude-opus-4-6",
            "claude opus 4.6 (thinking)": "claude-opus-4-6",
            "claude opus 4.6": "claude-opus-4-6",
            "claude-sonnet": "claude-sonnet-4-6",
            "claude-3-7-sonnet": "claude-3-7-sonnet",
            "claude-3.7-sonnet": "claude-3-7-sonnet",
            "claude-3.5-sonnet": "claude-3-5-sonnet",
            "claude-opus": "claude-opus-4-6",
            "claude-3-opus": "claude-opus-4-6",
            # OpenAI / GPT-OSS family aliases
            "gpt-oss-120b": "gpt-oss-120b",
            "gpt-oss-120b (medium)": "gpt-oss-120b",
            "gpt-oss 120b (medium)": "gpt-oss-120b",
            "gpt-oss 120b": "gpt-oss-120b",
            "gpt-oss": "gpt-oss-120b",
            "gpt-4": "gpt-4o",
        }
        candidate = aliases.get(requested.lower(), requested or self.config.default_model)
        available = self.list_models()
        if candidate in available:
            return candidate
        if requested in available:
            return requested
        norm_req = requested.lower().replace(" ", "-").replace("_", "-")
        norm_cand = candidate.lower().replace(" ", "-").replace("_", "-")
        for item in available:
            norm_item = item.lower().replace(" ", "-").replace("_", "-")
            if norm_item in (norm_req, norm_cand):
                return item
        raise UnknownModel(f"unknown model: {requested or candidate}")

    def readiness(self) -> dict[str, Any]:
        self._verify_if_required()
        runtime = self._ensure_runtime()
        try:
            help_result = subprocess.run(
                self._binary_command_prefix() + ["--help"],
                cwd=runtime,
                env=self._base_environment(),
                capture_output=True,
                text=True,
                timeout=min(self.config.timeout_seconds, 20),
                check=False,
            )
            version_result = subprocess.run(
                self._binary_command_prefix() + ["--version"],
                cwd=runtime,
                env=self._base_environment(),
                capture_output=True,
                text=True,
                timeout=min(self.config.timeout_seconds, 20),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendUnavailable("Antigravity compatibility check failed") from exc
        if help_result.returncode != 0:
            raise BackendUnavailable("Antigravity --help failed")
        help_output = help_result.stdout + "\n" + help_result.stderr
        missing = sorted(flag for flag in _REQUIRED_FLAGS if flag not in help_output)
        if missing:
            raise BackendUnavailable(
                "Antigravity CLI lacks required safe-mode flags: " + ", ".join(missing)
            )
        if version_result.returncode != 0:
            raise BackendUnavailable("Antigravity --version failed")
        version = version_result.stdout.strip().splitlines()[0] if version_result.stdout.strip() else ""
        if "*" not in self.config.validated_versions and version not in self.config.validated_versions:
            if not self.config.allow_unvalidated_versions:
                raise BackendProtocolError(
                    f"Antigravity CLI version {version or 'unknown'} is not validated; "
                    f"supported versions: {', '.join(self.config.validated_versions)}. "
                    "Set antigravity.allow_unvalidated_versions = true in config to bypass."
                )
            _LOG.warning(
                "Antigravity CLI version %s is unvalidated (validated: %s); proceeding because allow_unvalidated_versions=True",
                version,
                self.config.validated_versions,
            )
        models = self.list_models(force_refresh=True)
        return {
            "status": "ready",
            "agy_version": version,
            "models": list(models),
            "sandbox": self.config.sandbox,
            "mode": self.config.mode,
            "stateless": True,
        }

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:  # pragma: no cover - Linux is the supported service platform
                process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:  # pragma: no cover
                    process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _run_attempt(
        self,
        prompt: str,
        model: str,
        cwd: Path,
        on_delta: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        command = self.build_command(model)
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=self._base_environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True if os.name == "posix" else False,
            )
        except OSError as exc:
            raise BackendUnavailable("could not start Antigravity CLI") from exc

        lines: queue.Queue[str | None] = queue.Queue()

        def drain_stdout() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=drain_stdout, daemon=True)
        reader.start()
        assert process.stdin is not None
        try:
            if not prompt.endswith("\n"):
                prompt += "\n"
            process.stdin.write(prompt)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        deadline = time.monotonic() + self.config.timeout_seconds
        result: dict[str, Any] = {}
        diagnostics = ""
        try:
            stream_closed = False
            while not stream_closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BackendTimeout("Antigravity request timed out")
                try:
                    line = lines.get(timeout=min(remaining, 0.25))
                except queue.Empty:
                    if process.poll() is not None and not reader.is_alive():
                        stream_closed = True
                    continue
                if line is None:
                    stream_closed = True
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    diagnostics = (diagnostics + line)[-2_000:]
                    continue
                if not isinstance(event, dict):
                    continue
                if self.config.enforce_tool_isolation and event_indicates_internal_tool(event):
                    raise ToolIsolationError(
                        "Antigravity attempted internal tool activity; request aborted"
                    )
                if on_delta is not None and event.get("event") == "step_update":
                    step_update = event.get("step_update")
                    if isinstance(step_update, dict):
                        text_delta = step_update.get("text_delta")
                        if text_delta:
                            on_delta(str(text_delta))
                if event.get("event") == "result" and isinstance(event.get("result"), dict):
                    result = dict(event["result"])
            return_code = process.wait(timeout=3)
        except BackendError:
            self._terminate(process)
            raise
        except subprocess.TimeoutExpired as exc:
            self._terminate(process)
            raise BackendTimeout("Antigravity process did not exit") from exc
        finally:
            if process.poll() is None:
                self._terminate(process)
            reader.join(timeout=1)
            if process.stdout is not None:
                process.stdout.close()

        if return_code != 0:
            error = diagnostics or str(result.get("error") or "Antigravity CLI failed")
            raise BackendError(error[-500:])
        if not result:
            raise BackendProtocolError("Antigravity emitted no result event")
        if is_false_success(result):
            raise BackendProtocolError(
                "Antigravity false-success: empty response with zero usage and duration"
            )
        if result.get("status") != "SUCCESS":
            raise BackendError(str(result.get("error") or result.get("status") or "Antigravity failed")[-500:])
        response = str(result.get("response") or "").strip()
        if not response:
            raise BackendProtocolError("Antigravity returned an empty response")
        result["response"] = response
        return result

    def generate(self, prompt: str, model: str) -> BackendResponse:
        self._verify_if_required()
        runtime = self._ensure_runtime()
        last_error: BackendError | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            with tempfile.TemporaryDirectory(
                prefix="request-", dir=runtime, ignore_cleanup_errors=True
            ) as request_dir:
                try:
                    result = self._run_attempt(prompt, model, Path(request_dir))
                except BackendError as exc:
                    last_error = exc
                    transient = any(marker in str(exc).lower() for marker in _TRANSIENT_MARKERS)
                    if transient and attempt < self.config.max_attempts:
                        time.sleep(min(1.5 * attempt, 5.0))
                        continue
                    raise
            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            return BackendResponse(
                response=str(result["response"]),
                model=model,
                usage=dict(usage),
                status=str(result.get("status") or "SUCCESS"),
                duration_seconds=float(result.get("duration_seconds") or 0),
                conversation_id=(
                    str(result["conversation_id"])
                    if result.get("conversation_id") is not None
                    else None
                ),
            )
        raise last_error or BackendError("Antigravity request failed")

    def _stream_attempt(
        self, prompt: str, model: str, request_dir: Path
    ) -> Iterator[dict[str, Any]]:
        delta_queue: queue.Queue[dict[str, Any]] = queue.Queue()

        def on_delta(delta: str) -> None:
            delta_queue.put({"type": "delta", "content": delta})

        worker_error: list[Exception] = []
        final_result: list[dict[str, Any]] = []

        def worker() -> None:
            try:
                res = self._run_attempt(prompt, model, request_dir, on_delta=on_delta)
                final_result.append(res)
            except Exception as exc:  # noqa: BLE001 - propagate worker exception to caller
                worker_error.append(exc)
            finally:
                delta_queue.put({"type": "done"})

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        while True:
            item = delta_queue.get()
            if item["type"] == "done":
                break
            yield item

        thread.join()
        if worker_error:
            exc = worker_error[0]
            if isinstance(exc, BackendError):
                raise exc
            raise BackendError(str(exc)) from exc

        if final_result:
            result = final_result[0]
            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            backend_response = BackendResponse(
                response=str(result["response"]),
                model=model,
                usage=dict(usage),
                status=str(result.get("status") or "SUCCESS"),
                duration_seconds=float(result.get("duration_seconds") or 0),
                conversation_id=(
                    str(result["conversation_id"])
                    if result.get("conversation_id") is not None
                    else None
                ),
            )
            yield {"type": "result", "response": backend_response}

    def generate_stream(
        self, prompt: str, model: str
    ) -> Iterator[dict[str, Any]]:
        self._verify_if_required()
        runtime = self._ensure_runtime()
        last_error: BackendError | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            with tempfile.TemporaryDirectory(
                prefix="request-", dir=runtime, ignore_cleanup_errors=True
            ) as request_dir:
                try:
                    yield from self._stream_attempt(prompt, model, Path(request_dir))
                    return
                except BackendError as exc:
                    last_error = exc
                    transient = any(marker in str(exc).lower() for marker in _TRANSIENT_MARKERS)
                    if transient and attempt < self.config.max_attempts:
                        time.sleep(min(1.5 * attempt, 5.0))
                        continue
                    raise
        raise last_error or BackendError("Antigravity request failed")
