"""Fail-closed adapter for the Antigravity CLI stream-json protocol."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
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
    "429",
    "unavailable",
    "rate limit",
    "overloaded",
    "quota",
    "resource exhausted",
    "busy",
    "timeout",
    "temporar",
    "subscriber fell behind",
    "connection to the agent was interrupted",
)


def is_transient_backend_error(exc: Exception) -> bool:
    """Determine whether an upstream error represents a transient network reset."""
    if isinstance(exc, (ToolIsolationError, BackendProtocolError)):
        return False
    msg = str(exc).lower()
    non_transient_markers = (
        "empty response",
        "soft-denied",
        "denied",
        "tool confirmation",
        "internal tool",
        "false-success",
        "tool_isolation",
        "permission",
    )
    if any(m in msg for m in non_transient_markers):
        return False
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


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


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_MODEL_SKIP_PREFIXES = (
    "fetching",
    "available",
    "model id",
    "model_id",
    "name",
    "id",
    "account",
    "logged",
    "project",
    "using",
    "authenticated",
    "session",
    "total",
    "warning",
    "info",
    "error",
)
_MODEL_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{2,}$")


def parse_model_ids(output: str) -> tuple[str, ...]:
    ids: list[str] = []
    for raw_line in (output or "").splitlines():
        line = _ANSI_ESCAPE_RE.sub("", raw_line).strip()
        if not line or line.endswith(":"):
            continue
        low = line.lower()
        if any(low.startswith(prefix) for prefix in _MODEL_SKIP_PREFIXES):
            continue
        if low.startswith(("-", "=", "*", "#")):
            continue
        parts = re.split(r"\t+|\s{2,}", line, maxsplit=1)
        model_id = parts[0].strip()
        if " " in model_id:
            token = model_id.split()[0].strip()
            if token and not token.startswith(("-", "=", "*", "#")):
                model_id = token
        if (
            model_id
            and _MODEL_ID_RE.match(model_id)
            and model_id.lower() not in ("model", "models", "description", "details", "alias", "name", "id", "version", "status")
            and model_id not in ids
        ):
            ids.append(model_id)
    return tuple(ids)


DEFAULT_ANTIGRAVITY_MODELS: tuple[str, ...] = (
    "gemini-3.8-flash",
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low",
    "gemini-3.7-flash",
    "gemini-3.7-flash-high",
    "gemini-3.7-flash-medium",
    "gemini-3.7-flash-low",
    "gemini-3.6-flash",
    "gemini-3.6-flash-high",
    "gemini-3.6-flash-medium",
    "gemini-3.6-flash-low",
    "gemini-3.1-pro",
    "gemini-3.1-pro-high",
    "gemini-3.1-pro-low",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b",
    "gpt-oss-120b-medium",
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


_ALLOWED_INTERNAL_TOOLS = {
    "generate_image",
    "image_generation",
    "view_file",
    "read_url_content",
    "search_web",
}


def is_tool_confirmation_or_denial(event: Mapping[str, Any]) -> bool:
    """Check if event represents a tool confirmation prompt or denial rather than tool execution."""
    if not isinstance(event, dict):
        return False
    event_name = str(event.get("event") or event.get("type") or "").strip().lower()
    if any(m in event_name for m in ("confirmation", "denial", "denied", "permission", "ask_user", "ask_permission")):
        return True
    status = str(event.get("status") or "").strip().lower()
    if any(m in status for m in ("soft-denied", "denied", "rejected", "confirmation-required")):
        return True
    state = str(event.get("state") or "").strip().upper()
    step_type = str(event.get("step_type") or "").strip().lower()
    if step_type == "tool" and state in ("ERROR", "CANCELLED"):
        return True
    if event.get("denied_actions"):
        return True

    step_update = event.get("step_update")
    if isinstance(step_update, dict) and is_tool_confirmation_or_denial(step_update):
        return True

    error_obj = event.get("error")
    if isinstance(error_obj, dict):
        err_msg = str(error_obj.get("message") or "").lower()
        if any(m in err_msg for m in ("denied", "permission", "soft-denied", "restricted")):
            return True

    for val in event.values():
        if isinstance(val, str):
            v_low = val.lower()
            if any(m in v_low for m in (
                "soft-denied",
                "tool confirmation",
                "user denied permission",
                "permission check failed",
                "confirmation: denied",
            )):
                return True
        elif isinstance(val, dict) and is_tool_confirmation_or_denial(val):
            return True
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict) and is_tool_confirmation_or_denial(item):
                    return True
    return False


def extract_tool_calls_from_event(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract tool call name and arguments from Antigravity tool events or step updates."""
    if not isinstance(event, dict):
        return []
    extracted: list[dict[str, Any]] = []

    def _process_candidate(cand: Any, default_name: str | None = None) -> None:
        if not isinstance(cand, dict):
            return
        name = (
            cand.get("name")
            or cand.get("tool_name")
            or cand.get("tool")
            or cand.get("function")
            or default_name
        )
        if isinstance(name, dict):
            name = name.get("name")
        if isinstance(name, str) and name.strip():
            raw_args = (
                cand.get("arguments")
                or cand.get("args")
                or cand.get("parameters")
                or cand.get("input")
                or {}
            )
            cid = cand.get("id") or cand.get("call_id") or ""
            if isinstance(raw_args, str):
                try:
                    args_obj = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    args_obj = {"raw": raw_args}
            elif isinstance(raw_args, dict):
                args_obj = dict(raw_args)
            else:
                args_obj = {}
            extracted.append({
                "id": str(cid) if cid else None,
                "name": str(name).strip(),
                "arguments": args_obj,
            })

    candidate_keys = (
        "tool_call",
        "tool_calls",
        "tool",
        "tools",
        "tool_info",
        "call",
        "calls",
        "tool_use",
        "tool_uses",
        "action",
        "actions",
    )

    for key in candidate_keys:
        val = event.get(key)
        if isinstance(val, list):
            for item in val:
                _process_candidate(item)
        elif isinstance(val, dict):
            _process_candidate(val)

    step_update = event.get("step_update")
    if isinstance(step_update, dict):
        step_tool_name = step_update.get("tool_name") or step_update.get("name")
        for key in candidate_keys:
            val = step_update.get(key)
            if isinstance(val, list):
                for item in val:
                    _process_candidate(item, default_name=str(step_tool_name) if step_tool_name else None)
            elif isinstance(val, dict):
                _process_candidate(val, default_name=str(step_tool_name) if step_tool_name else None)

        if not extracted and step_tool_name:
            _process_candidate(step_update)

        error_obj = step_update.get("error")
        if isinstance(error_obj, dict):
            err_msg = str(error_obj.get("message") or "")
            cmd_match = re.search(r"user denied permission to run command:\s*\n?([^\n]+)", err_msg)
            if not cmd_match:
                cmd_match = re.search(r'permission check failed for command "(.*?)":', err_msg)
            if cmd_match:
                cmd_val = cmd_match.group(1).strip().replace('\\"', '"')
                if extracted:
                    for item in extracted:
                        if not item.get("arguments") or not item["arguments"].get("command"):
                            item["arguments"]["command"] = cmd_val
                else:
                    extracted.append({
                        "id": None,
                        "name": str(step_tool_name or "RunCommand"),
                        "arguments": {"command": cmd_val},
                    })

    if not extracted and any(k in event for k in ("name", "tool_name")):
        _process_candidate(event)

    res_obj = event.get("result")
    denied_actions = (
        event.get("denied_actions")
        or (res_obj.get("denied_actions") if isinstance(res_obj, dict) else None)
    )
    if isinstance(denied_actions, list) and not extracted:
        for da in denied_actions:
            if isinstance(da, dict):
                t_name = da.get("display_name") or da.get("action")
                if t_name:
                    extracted.append({
                        "id": None,
                        "name": str(t_name),
                        "arguments": {},
                    })

    return extracted


def _is_allowed_tool_call(item: Any) -> bool:
    if isinstance(item, dict):
        name = str(
            item.get("name")
            or item.get("tool_name")
            or item.get("function")
            or item.get("tool")
            or ""
        ).strip().lower()
        if name in _ALLOWED_INTERNAL_TOOLS:
            return True
        fn = item.get("function")
        if isinstance(fn, dict):
            fn_name = str(fn.get("name") or "").strip().lower()
            if fn_name in _ALLOWED_INTERNAL_TOOLS:
                return True
        for subkey in ("tool_calls", "calls", "actions"):
            sub = item.get(subkey)
            if isinstance(sub, list) and sub:
                return all(_is_allowed_tool_call(tc) for tc in sub)
        return False
    if isinstance(item, list) and item:
        return all(_is_allowed_tool_call(tc) for tc in item)
    return False


def event_indicates_internal_tool(event: Mapping[str, Any]) -> bool:
    if is_tool_confirmation_or_denial(event):
        return False
    event_name = str(event.get("event") or event.get("type") or "").strip().lower()
    if event_name in _TOOL_EVENT_NAMES:
        if event_name in {"tool", "tool_call", "tool-call", "tool_use", "tool-use"}:
            tc = (
                event.get("tool_call")
                or event.get("tool_calls")
                or event.get("tool")
                or event.get("call")
                or event
            )
            if _is_allowed_tool_call(tc):
                return False
        if event_name == "artifact":
            artifact = event.get("artifact") if isinstance(event, dict) else None
            if isinstance(artifact, dict) and any(
                str(artifact.get("path") or artifact.get("filename") or "").lower().endswith(ext)
                for ext in (".jpg", ".jpeg", ".png", ".webp")
            ):
                return False
        return True

    def contains_tool_key(value: Any) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                k_low = str(key).lower()
                if k_low in _TOOL_KEYS and item not in (None, False, "", [], {}):
                    if k_low in {"tool_call", "tool_calls", "tool_use", "tool_uses"} and _is_allowed_tool_call(item):
                        continue
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
        self._model_source: str = "unknown"

    def _sync_credentials_and_settings(self) -> None:
        """Auto-sync credentials and ensure safe default settings in isolated home."""
        cli_dir = self.config.home / ".gemini" / "antigravity-cli"
        try:
            cli_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        # 1. Ensure settings.json exists with strict fail-closed isolation if missing
        settings_file = self.config.settings_file
        if not settings_file.exists():
            default_settings = {
                "artifactReviewPolicy": "asks-for-review",
                "permissions": {"allow": []},
                "toolPermission": "strict",
                "trustedWorkspaces": [],
            }
            try:
                settings_file.write_text(json.dumps(default_settings, indent=2), encoding="utf-8")
            except OSError as exc:
                _LOG.warning("could not auto-create isolated settings.json: %s", exc)

        # 2. Sync authentication state from user profile only if opt-in enabled
        if self.config.sync_user_credentials:
            try:
                user_cli = Path.home() / ".gemini" / "antigravity-cli"
                if user_cli.exists() and user_cli.resolve() != cli_dir.resolve():
                    for filename in ("installation_id", "jetski_state.pbtxt"):
                        src = user_cli / filename
                        dst = cli_dir / filename
                        if src.is_file() and src.stat().st_size > 0:
                            try:
                                if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
                                    shutil.copy2(src, dst)
                                    if os.name == "posix":
                                        try:
                                            os.chmod(dst, 0o600)
                                        except OSError:
                                            pass
                            except OSError as exc:
                                _LOG.debug("could not copy auth file: %s", exc)
                    _LOG.info("User credentials synchronized to isolated profile (opt-in enabled).")
            except OSError as exc:
                _LOG.debug("credentials auto-sync error: %s", exc)

    def _base_environment(self) -> dict[str, str]:
        self._sync_credentials_and_settings()
        env = dict(os.environ)
        env["HOME"] = str(self.config.home)
        if os.name == "nt":
            env["USERPROFILE"] = str(self.config.home)
        for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
            env.pop(var, None)
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

    def is_authenticated(self) -> bool:
        """Check if Google Antigravity credentials exist in the active profile."""
        self._sync_credentials_and_settings()
        target = self.config.home / ".gemini" / "antigravity-cli" / "jetski_state.pbtxt"
        return target.is_file() and target.stat().st_size > 0

    @property
    def model_source(self) -> str:
        return self._model_source

    def verify_tool_isolation_settings(self, settings_path: Path) -> None:
        """Require Antigravity to deny autonomous internal tool execution."""
        try:
            payload = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolIsolationError(
                "Antigravity strict permission settings are required but unreadable"
            ) from exc
        if not isinstance(payload, dict):
            raise ToolIsolationError(
                "Antigravity strict tool isolation requires toolPermission='strict', "
                "artifactReviewPolicy in ('asks-for-review', 'agent-decides'), "
                "no allow rules, and no trusted workspaces"
            )
        permissions = payload.get("permissions")
        if isinstance(permissions, dict):
            allow_rules = permissions.get("allow") or []
        elif isinstance(permissions, list):
            allow_rules = permissions
        elif permissions is not None:
            allow_rules = [permissions]
        else:
            allow_rules = []
        trusted = payload.get("trustedWorkspaces") or []
        policy = payload.get("artifactReviewPolicy")
        valid_policies = {"asks-for-review", "agent-decides"}
        if (
            payload.get("toolPermission") != "strict"
            or (policy is not None and policy not in valid_policies)
            or allow_rules
            or trusted
        ):
            raise ToolIsolationError(
                "Antigravity strict tool isolation requires toolPermission='strict', "
                "artifactReviewPolicy in ('asks-for-review', 'agent-decides'), "
                "no allow rules, and no trusted workspaces"
            )

    def _verify_if_required(self) -> None:
        if self.config.enforce_tool_isolation:
            self._sync_credentials_and_settings()
            self.verify_tool_isolation_settings(self.config.settings_file)

    def _binary_command_prefix(self) -> list[str]:
        binary_str = str(self.config.binary)
        base = [binary_str]
        if binary_str.lower().endswith(".py"):
            base = [sys.executable, binary_str]
        if self.config.wrapper:
            return list(self.config.wrapper) + base
        return base

    def build_command(self, model: str, effort: str | None = None) -> list[str]:
        command = self._binary_command_prefix() + [
            "--input-format",
            "text",
            "--output-format",
            "stream-json",
            "--disable-slash-commands",
        ]
        if self.config.sandbox:
            command.append("--sandbox")

        selected_effort: str | None = effort
        base_model = model

        # agy CLI specifically expects 'claude-opus-4-6-thinking' for Opus
        if base_model in ("claude-opus-4-6", "claude-opus-4.6"):
            base_model = "claude-opus-4-6-thinking"

        if "gemini" in base_model.lower():
            for suffix in ("-high", "-medium", "-low"):
                if base_model.endswith(suffix):
                    if not effort:
                        selected_effort = suffix[1:]
                    base_model = base_model[: -len(suffix)]
                    break

            if selected_effort is not None:
                selected_effort = selected_effort.lower().strip()
                if selected_effort in ("minimal", "none"):
                    selected_effort = "low"
                elif selected_effort in ("xhigh", "max", "ultra"):
                    selected_effort = "high"
                elif selected_effort not in ("low", "medium", "high"):
                    selected_effort = "medium"

            if selected_effort is None:
                selected_effort = "medium"

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
        if selected_effort is not None and "gemini" in base_model.lower():
            command.extend(["--effort", selected_effort])
        return command

    def list_models(self, *, force_refresh: bool = False) -> tuple[str, ...]:
        now = time.monotonic()
        with self._cache_lock:
            cached_at, cached = self._model_cache
            if cached and not force_refresh and now - cached_at < self.config.model_cache_ttl_seconds:
                return cached
            try:
                runtime = self._ensure_runtime()
                completed = subprocess.run(
                    self._binary_command_prefix() + ["models"],
                    cwd=runtime,
                    env=self._base_environment(),
                    capture_output=True,
                    text=True,
                    timeout=min(self.config.timeout_seconds, 15),
                    check=False,
                )
                if completed.returncode != 0:
                    _LOG.warning(
                        "agy models returned non-zero exit code %d: stdout=%r stderr=%r",
                        completed.returncode,
                        completed.stdout,
                        completed.stderr,
                    )
                models = parse_model_ids(completed.stdout) if completed.returncode == 0 else ()
                if not models and completed.stderr and completed.returncode == 0:
                    models = parse_model_ids(completed.stderr)
            except subprocess.TimeoutExpired as exc:
                _LOG.warning(
                    "agy models discovery timed out after %s seconds; falling back to default models",
                    exc.timeout,
                )
                models = ()
            except Exception as exc:  # noqa: BLE001 - fallback if binary discovery fails
                _LOG.warning("agy models discovery error: %s; falling back to default models", exc)
                models = ()
            if models:
                self._model_source = "discovered"
            else:
                models = DEFAULT_ANTIGRAVITY_MODELS
                self._model_source = "fallback"
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
            "claude-opus-4-6-thinking": "claude-opus-4-6-thinking",
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
            "gpt-oss-120b-medium": "gpt-oss-120b-medium",
            "gpt-oss-120b (medium)": "gpt-oss-120b",
            "gpt-oss 120b (medium)": "gpt-oss-120b",
            "gpt-oss 120b": "gpt-oss-120b",
            "gpt-oss": "gpt-oss-120b",
            "gpt-4": "gpt-oss-120b",
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
        for item in available:
            norm_item = item.lower().replace(" ", "-").replace("_", "-")
            if norm_item.startswith(norm_cand) or norm_cand.startswith(norm_item):
                return item
            if norm_item.startswith(norm_req) or norm_req.startswith(norm_item):
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
        is_validated = (
            "*" in self.config.validated_versions
            or version in self.config.validated_versions
        )
        if "*" in self.config.validated_versions:
            _LOG.warning(
                "Wildcard version validation is active; Antigravity CLI version safety enforcement is bypassed."
            )
        if not is_validated:
            if not self.config.allow_unvalidated_versions:
                raise BackendProtocolError(
                    f"Antigravity CLI version '{version or 'unknown'}' is not in validated versions: "
                    f"{', '.join(self.config.validated_versions)}. "
                    "To update agy, run: curl -fsSL https://antigravity.google/cli/install.sh | bash. "
                    "To bypass validation, set antigravity.allow_unvalidated_versions = true in config.toml."
                )
            _LOG.warning(
                "Antigravity CLI version %s is unvalidated (validated: %s); proceeding because allow_unvalidated_versions=True",
                version,
                self.config.validated_versions,
            )
        models = self.list_models(force_refresh=True)
        authenticated = self.is_authenticated()
        is_ready = bool(authenticated and self._model_source != "fallback")
        status = "ready" if is_ready else "degraded"
        result: dict[str, Any] = {
            "status": status,
            "authenticated": authenticated,
            "agy_version": version,
            "models": list(models),
            "model_source": self._model_source,
            "sandbox": self.config.sandbox,
            "mode": self.config.mode,
            "stateless": True,
        }
        if not authenticated:
            result["reason"] = "authentication required; run 'agy' to sign in"
        elif self._model_source == "fallback":
            result["reason"] = "running on fallback models without confirmed binary execution"
        return result

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            elif os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    check=False,
                )
            else:
                process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                elif os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        capture_output=True,
                        check=False,
                    )
                else:
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
        effort: str | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
        process_holder: list[subprocess.Popen[str]] | None = None,
    ) -> dict[str, Any]:
        command = self.build_command(model, effort=effort)
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
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise BackendUnavailable("could not start Antigravity CLI") from exc

        if process_holder is not None:
            process_holder.append(process)

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

        def write_stdin() -> None:
            assert process.stdin is not None
            try:
                text_to_send = prompt if prompt.endswith("\n") else prompt + "\n"
                process.stdin.write(text_to_send)
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass

        writer = threading.Thread(target=write_stdin, daemon=True)
        writer.start()

        deadline = time.monotonic() + self.config.timeout_seconds
        result: dict[str, Any] = {}
        diagnostics = ""
        accumulated_text_deltas: list[str] = []
        intercepted_tool_calls: list[dict[str, Any]] = []
        denial_detected = False
        denial_tool_name: str | None = None
        try:
            stream_closed = False
            while not stream_closed:
                if cancel_event is not None and cancel_event.is_set():
                    self._terminate(process)
                    return {}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BackendTimeout("Antigravity request timed out")
                try:
                    line = lines.get(timeout=min(remaining, 0.25))
                except queue.Empty:
                    if cancel_event is not None and cancel_event.is_set():
                        self._terminate(process)
                        return {}
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
                    m = re.search(
                        r"([A-Za-z0-9_]+)\s+tool confirmation:\s*soft-denied",
                        line,
                        re.IGNORECASE,
                    )
                    if m:
                        denial_detected = True
                        denial_tool_name = m.group(1)
                    elif (
                        "tool confirmation: soft-denied" in line.lower()
                        or "confirmation: denied" in line.lower()
                        or "user denied permission" in line.lower()
                        or "permission check failed" in line.lower()
                    ):
                        denial_detected = True
                    continue
                if not isinstance(event, dict):
                    continue
                if is_tool_confirmation_or_denial(event):
                    denial_detected = True
                    intercepted_tool_calls.extend(extract_tool_calls_from_event(event))
                elif self.config.enforce_tool_isolation and event_indicates_internal_tool(event):
                    raise ToolIsolationError(
                        "Antigravity attempted internal tool activity; request aborted"
                    )
                if event.get("event") == "step_update":
                    step_update = event.get("step_update")
                    if isinstance(step_update, dict):
                        thought = (
                            step_update.get("thought_delta")
                            or step_update.get("reasoning_delta")
                            or step_update.get("thinking_delta")
                        )
                        step_type = str(step_update.get("step_type", "")).strip().lower()
                        if thought:
                            if on_reasoning_delta is not None:
                                on_reasoning_delta(str(thought))
                        elif step_type in ("thinking", "thought", "reasoning", "internal_thought"):
                            text_delta = step_update.get("text_delta")
                            if text_delta:
                                if on_reasoning_delta is not None:
                                    on_reasoning_delta(str(text_delta))
                                elif on_delta is not None:
                                    on_delta(str(text_delta))
                        else:
                            text_delta = step_update.get("text_delta")
                            if text_delta:
                                accumulated_text_deltas.append(str(text_delta))
                                if on_delta is not None:
                                    on_delta(str(text_delta))
                        if is_tool_confirmation_or_denial(step_update):
                            denial_detected = True
                            intercepted_tool_calls.extend(extract_tool_calls_from_event(step_update))
                elif event.get("event") in ("thought", "thinking", "reasoning", "reasoning_delta"):
                    thought = (
                        event.get("content")
                        or event.get("text_delta")
                        or event.get("thought")
                        or event.get("reasoning")
                    )
                    if thought and on_reasoning_delta is not None:
                        on_reasoning_delta(str(thought))
                if event.get("event") == "result" and isinstance(event.get("result"), dict):
                    result = dict(event["result"])
                    if result.get("denied_actions"):
                        denial_detected = True
                        for da in result["denied_actions"]:
                            if isinstance(da, dict) and not denial_tool_name:
                                denial_tool_name = da.get("display_name") or da.get("action")
                    if is_tool_confirmation_or_denial(result):
                        denial_detected = True
                        intercepted_tool_calls.extend(extract_tool_calls_from_event(result))
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
            writer.join(timeout=1)
            reader.join(timeout=1)
            if process.stdout is not None and hasattr(process.stdout, "close"):
                process.stdout.close()

        if return_code != 0:
            error = diagnostics or str(result.get("error") or "Antigravity CLI failed")
            raise BackendError(error[-500:])
        if not result:
            raise BackendProtocolError("Antigravity emitted no result event")
        if result.get("status") != "SUCCESS":
            raise BackendError(str(result.get("error") or result.get("status") or "Antigravity failed")[-500:])
        response = str(result.get("response") or "").strip()
        if not response:
            if intercepted_tool_calls:
                for tc in intercepted_tool_calls:
                    if not tc.get("arguments") or tc["arguments"] == {}:
                        tname = tc.get("name") or denial_tool_name or "RunCommand"
                        cmd_m = (
                            re.search(r"user denied permission to run command:\s*\n?([^\n]+)", diagnostics)
                            or re.search(r'permission check failed for command "(.*?)":', diagnostics)
                            or re.search(r"(?:command|CommandLine|cmd):\s*(.+)", diagnostics, re.IGNORECASE)
                        )
                        path_m = (
                            re.search(r'permission check failed for (?:file|path)\s*\"?([^\":\n]+)\"?', diagnostics, re.IGNORECASE)
                            or re.search(r"(?:path|AbsolutePath|filePath|targetFile):\s*(.+)", diagnostics, re.IGNORECASE)
                        )
                        if cmd_m and ("command" in tname.lower() or not path_m):
                            tc["arguments"] = {"command": cmd_m.group(1).strip().strip("'\"").replace('\\"', '"')}
                        elif path_m:
                            tc["arguments"] = {"path": path_m.group(1).strip().strip("'\"")}

                tool_call_tags: list[str] = []
                for tc in intercepted_tool_calls:
                    call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
                    tname = tc.get("name") or "unknown_tool"
                    targs = tc.get("arguments") or {}
                    targs_str = (
                        json.dumps(targs, ensure_ascii=False, separators=(",", ":"))
                        if isinstance(targs, dict)
                        else str(targs)
                    )
                    payload = {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": tname,
                            "arguments": targs_str,
                        },
                    }
                    tool_call_tags.append(
                        f"<tool_call>{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}</tool_call>"
                    )
                prefix = "".join(accumulated_text_deltas).strip()
                calls_joined = "\n".join(tool_call_tags)
                response = f"{prefix}\n{calls_joined}".strip() if prefix else calls_joined
                _LOG.info(
                    "Recovered %d intercepted tool calls from Antigravity stream",
                    len(intercepted_tool_calls),
                )
            elif accumulated_text_deltas:
                response = "".join(accumulated_text_deltas).strip()
                _LOG.info(
                    "Recovered non-empty assistant response from %d accumulated stream deltas",
                    len(accumulated_text_deltas),
                )
            elif denial_detected:
                tname = denial_tool_name or "requested tool"
                cmd_m = (
                    re.search(r"user denied permission to run command:\s*\n?([^\n]+)", diagnostics)
                    or re.search(r'permission check failed for command "(.*?)":', diagnostics)
                    or re.search(r"(?:command|CommandLine|cmd):\s*(.+)", diagnostics, re.IGNORECASE)
                )
                path_m = (
                    re.search(r'permission check failed for (?:file|path)\s*\"?([^\":\n]+)\"?', diagnostics, re.IGNORECASE)
                    or re.search(r"(?:path|AbsolutePath|filePath|targetFile):\s*(.+)", diagnostics, re.IGNORECASE)
                )
                if cmd_m:
                    cmd_val = cmd_m.group(1).strip().strip("'\"").replace('\\"', '"')
                    effective_name = denial_tool_name or "RunCommand"
                    payload = {
                        "id": f"call_{uuid.uuid4().hex[:12]}",
                        "type": "function",
                        "function": {
                            "name": effective_name,
                            "arguments": json.dumps({"command": cmd_val}, separators=(",", ":")),
                        },
                    }
                    response = f"<tool_call>{json.dumps(payload, separators=(',', ':'))}</tool_call>"
                    _LOG.info("Recovered tool call for %s from denial diagnostics", effective_name)
                elif path_m and denial_tool_name:
                    path_val = path_m.group(1).strip().strip("'\"")
                    payload = {
                        "id": f"call_{uuid.uuid4().hex[:12]}",
                        "type": "function",
                        "function": {
                            "name": denial_tool_name,
                            "arguments": json.dumps({"path": path_val}, separators=(",", ":")),
                        },
                    }
                    response = f"<tool_call>{json.dumps(payload, separators=(',', ':'))}</tool_call>"
                    _LOG.info("Recovered tool call for %s from denial diagnostics", denial_tool_name)
                else:
                    response = (
                        f"I attempted to execute {tname}, but internal tool execution is restricted "
                        "in plan mode. Please execute this tool directly via Hermes."
                    )
                    _LOG.info(
                        "Recovered assistant message from detected tool denial for %s", tname
                    )

        if not response and is_false_success(result):
            raise BackendProtocolError(
                "Antigravity false-success: empty response with zero usage and duration"
            )
        if not response:
            raise BackendProtocolError("Antigravity returned an empty response")

        # Discover native Google Imagen generated images for this turn and attach MEDIA tag for Hermes
        conv_id = result.get("conversation_id")
        if conv_id:
            for base_dir in (Path.home(), self.config.home):
                brain_dir = base_dir / ".gemini" / "antigravity-cli" / "brain" / str(conv_id)
                if brain_dir.is_dir():
                    try:
                        img_files = sorted(
                            [
                                f
                                for f in brain_dir.iterdir()
                                if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                            ],
                            key=lambda p: p.stat().st_mtime,
                            reverse=True,
                        )
                        if img_files:
                            img_path = img_files[0]
                            media_tag = f"MEDIA:{img_path.as_posix()}"
                            if media_tag not in response:
                                response = f"{response}\n\n{media_tag}".strip()
                            break
                    except OSError:
                        pass

        result["response"] = response
        return result

    def generate(self, prompt: str, model: str, effort: str | None = None) -> BackendResponse:
        self._verify_if_required()
        runtime = self._ensure_runtime()
        last_error: BackendError | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            with tempfile.TemporaryDirectory(
                prefix="request-", dir=runtime, ignore_cleanup_errors=True
            ) as request_dir:
                try:
                    result = self._run_attempt(prompt, model, Path(request_dir), effort=effort)
                except BackendError as exc:
                    last_error = exc
                    transient = is_transient_backend_error(exc)
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
        self, prompt: str, model: str, request_dir: Path, effort: str | None = None
    ) -> Iterator[dict[str, Any]]:
        delta_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        cancel_event = threading.Event()
        process_holder: list[subprocess.Popen[str]] = []

        def on_delta(delta: str) -> None:
            delta_queue.put({"type": "delta", "content": delta})

        def on_reasoning_delta(delta: str) -> None:
            delta_queue.put({"type": "reasoning_delta", "content": delta})

        worker_error: list[Exception] = []
        final_result: list[dict[str, Any]] = []

        def worker() -> None:
            try:
                res = self._run_attempt(
                    prompt,
                    model,
                    request_dir,
                    on_delta=on_delta,
                    effort=effort,
                    on_reasoning_delta=on_reasoning_delta,
                    cancel_event=cancel_event,
                    process_holder=process_holder,
                )
                if res:
                    final_result.append(res)
            except Exception as exc:  # noqa: BLE001 - propagate worker exception to caller
                worker_error.append(exc)
            finally:
                delta_queue.put({"type": "done"})

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        try:
            while True:
                item = delta_queue.get()
                if item["type"] == "done":
                    break
                yield item
        finally:
            cancel_event.set()
            if process_holder:
                self._terminate(process_holder[0])
            thread.join(timeout=3)

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
        self, prompt: str, model: str, effort: str | None = None
    ) -> Iterator[dict[str, Any]]:
        self._verify_if_required()
        runtime = self._ensure_runtime()
        last_error: BackendError | None = None
        yielded_chunks = 0
        for attempt in range(1, self.config.max_attempts + 1):
            with tempfile.TemporaryDirectory(
                prefix="request-", dir=runtime, ignore_cleanup_errors=True
            ) as request_dir:
                try:
                    for chunk in self._stream_attempt(prompt, model, Path(request_dir), effort=effort):
                        yielded_chunks += 1
                        yield chunk
                    return
                except BackendError as exc:
                    last_error = exc
                    if yielded_chunks > 0:
                        raise
                    transient = is_transient_backend_error(exc)
                    if transient and attempt < self.config.max_attempts:
                        time.sleep(min(1.5 * attempt, 5.0))
                        continue
                    raise
        raise last_error or BackendError("Antigravity request failed")
