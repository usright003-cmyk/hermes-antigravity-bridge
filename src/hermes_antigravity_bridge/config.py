"""Typed, fail-closed bridge configuration."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .prompt.budget import PromptBudget

_INSECURE_PUBLISHED_TOKEN = "local-antigravity"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _as_bool(value: Any, *, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ConfigurationError(f"{name} must be a boolean")


def _as_int(value: Any, *, name: str, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if result < minimum or (maximum is not None and result > maximum):
        suffix = f" and at most {maximum}" if maximum is not None else ""
        raise ConfigurationError(f"{name} must be at least {minimum}{suffix}")
    return result


def _section(mapping: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = mapping.get(name, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"[{name}] must be a table")
    return dict(value)


def _read_token_file(path_value: str) -> str:
    path = Path(path_value).expanduser()
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise ConfigurationError(f"bearer token file is not readable: {path}") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise ConfigurationError("bearer token file must be a regular file")
    if os.name == "posix" and file_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigurationError("bearer token file must use 0600 permissions")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigurationError(f"bearer token file is not readable: {path}") from exc
    return token


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    token: str = field(repr=False)
    token_file: Path | None = None
    allow_remote: bool = False
    request_body_limit_bytes: int = 16 * 1024 * 1024
    max_concurrent_requests: int = 4


def _default_runtime_dir() -> Path:
    uid = os.getuid() if hasattr(os, "getuid") else os.getpid()
    return Path(tempfile.gettempdir()) / f"hermes-antigravity-bridge-{uid}"


@dataclass(frozen=True)
class AntigravityConfig:
    binary: Path
    default_model: str = "gemini-3.8-flash-high"
    timeout_seconds: int = 300
    sandbox: bool = True
    mode: str = "plan"
    enforce_tool_isolation: bool = True
    home: Path = field(
        default_factory=lambda: Path(
            "~/.local/state/hermes-antigravity-bridge/agy-home"
        ).expanduser()
    )
    runtime_dir: Path = field(default_factory=_default_runtime_dir)
    model_cache_ttl_seconds: int = 60
    max_attempts: int = 3
    validated_versions: tuple[str, ...] = (
        "1.1.17",
        "1.1.26",
        "1.1.27",
        "1.1.28",
        "1.1.29",
        "1.1.30",
    )
    allow_unvalidated_versions: bool = False
    wrapper: tuple[str, ...] = ()

    @property
    def settings_file(self) -> Path:
        return self.home / ".gemini/antigravity-cli/settings.json"


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    include_prompt_content: bool = False


@dataclass(frozen=True)
class BridgeConfig:
    server: ServerConfig
    antigravity: AntigravityConfig
    prompt: PromptBudget
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> BridgeConfig:
        env = os.environ if environ is None else environ
        selected = Path(path or env.get("HAB_CONFIG", "~/.config/hermes-antigravity-bridge/config.toml")).expanduser()
        try:
            try:
                import tomllib
            except ModuleNotFoundError:  # Python 3.10 package dependency
                import tomli as tomllib  # type: ignore[no-redef]
            with selected.open("rb") as handle:
                mapping = tomllib.load(handle)
        except OSError as exc:
            raise ConfigurationError(f"configuration file is not readable: {selected}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(f"invalid TOML configuration: {exc}") from exc
        return cls.from_mapping(mapping, environ=env)

    @classmethod
    def from_mapping(
        cls,
        mapping: Mapping[str, Any],
        *,
        environ: Mapping[str, str] | None = None,
    ) -> BridgeConfig:
        env = os.environ if environ is None else environ
        server_raw = _section(mapping, "server")
        agy_raw = _section(mapping, "antigravity")
        prompt_raw = _section(mapping, "prompt")
        logging_raw = _section(mapping, "logging")

        host = str(env.get("AGY_BRIDGE_HOST", server_raw.get("host", "127.0.0.1"))).strip()
        allow_remote = _as_bool(
            env.get("AGY_BRIDGE_ALLOW_REMOTE", server_raw.get("allow_remote", False)),
            name="server.allow_remote",
        )
        if host not in _LOOPBACK_HOSTS and not allow_remote:
            raise ConfigurationError(
                "non-loopback binding requires server.allow_remote=true"
            )

        token_file_value = str(
            env.get("AGY_BRIDGE_TOKEN_FILE", server_raw.get("token_file", ""))
        ).strip()
        token = str(env.get("AGY_BRIDGE_TOKEN", "")).strip()
        if not token and token_file_value:
            token = _read_token_file(token_file_value)
        if not token:
            raise ConfigurationError("a bearer token is required")
        if token == _INSECURE_PUBLISHED_TOKEN:
            raise ConfigurationError("refusing insecure published default bearer token")
        if len(token) < 24:
            raise ConfigurationError("bearer token must contain at least 24 characters")

        binary_value = str(env.get("AGY_BINARY", agy_raw.get("binary", "agy"))).strip()
        if os.sep in binary_value or (os.altsep and os.altsep in binary_value):
            binary = Path(binary_value).expanduser()
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise ConfigurationError(f"Antigravity binary is not executable: {binary}")
        else:
            import shutil

            discovered = shutil.which(binary_value)
            if not discovered:
                # Search standard fallback locations (Linux, macOS, Windows, Termux)
                candidates = [
                    Path.home() / ".local" / "bin" / binary_value,
                    Path("/usr/local/bin") / binary_value,
                    Path("/usr/bin") / binary_value,
                    Path.home() / "AppData" / "Local" / "agy" / "bin" / f"{binary_value}.exe",
                    Path.home() / "AppData" / "Local" / "agy" / "bin" / binary_value,
                    Path.home() / ".local" / "bin" / f"{binary_value}.exe",
                ]
                for cand in candidates:
                    if cand.is_file() and os.access(cand, os.X_OK):
                        discovered = str(cand)
                        break
            if not discovered:
                raise ConfigurationError(
                    f"Antigravity binary was not found on PATH: {binary_value}. "
                    "Ensure 'agy' is installed or set AGY_BINARY to its full path."
                )
            binary = Path(discovered)

        sandbox = _as_bool(
            env.get("AGY_SANDBOX", agy_raw.get("sandbox", True)),
            name="antigravity.sandbox",
        )
        enforce_isolation = _as_bool(
            env.get(
                "AGY_ENFORCE_TOOL_ISOLATION",
                agy_raw.get("enforce_tool_isolation", True),
            ),
            name="antigravity.enforce_tool_isolation",
        )
        mode = str(env.get("AGY_MODE", agy_raw.get("mode", "plan"))).strip()
        if enforce_isolation and not sandbox:
            raise ConfigurationError("tool isolation requires antigravity.sandbox=true")
        if enforce_isolation and mode != "plan":
            raise ConfigurationError("tool isolation requires antigravity.mode='plan'")

        limits_value: Any = prompt_raw.get("model_context_tokens", {})
        if "AGY_MODEL_CONTEXT_TOKENS_JSON" in env:
            try:
                limits_value = json.loads(env["AGY_MODEL_CONTEXT_TOKENS_JSON"])
            except json.JSONDecodeError as exc:
                raise ConfigurationError("AGY_MODEL_CONTEXT_TOKENS_JSON must be valid JSON") from exc
        if not isinstance(limits_value, dict):
            raise ConfigurationError("prompt.model_context_tokens must be a table/object")
        model_limits: dict[str, int] = {}
        for model, value in limits_value.items():
            model_limits[str(model)] = _as_int(
                value,
                name=f"prompt.model_context_tokens.{model}",
                minimum=1,
            )

        prompt = PromptBudget(
            max_chars=_as_int(
                env.get("AGY_MAX_PROMPT_CHARS", prompt_raw.get("max_chars", 4_000_000)),
                name="prompt.max_chars",
                minimum=4_096,
            ),
            output_token_reserve=_as_int(
                env.get(
                    "AGY_OUTPUT_TOKEN_RESERVE",
                    prompt_raw.get("output_token_reserve", 8_192),
                ),
                name="prompt.output_token_reserve",
                minimum=0,
            ),
            chars_per_token=_as_int(
                env.get(
                    "AGY_CHARS_PER_TOKEN",
                    prompt_raw.get("chars_per_token", 4),
                ),
                name="prompt.chars_per_token",
                minimum=1,
            ),
            model_context_tokens=model_limits,
        )

        runtime_dir = Path(
            str(env.get("AGY_RUNTIME_DIR", agy_raw.get("runtime_dir", ""))).strip()
            or _default_runtime_dir()
        ).expanduser()

        server = ServerConfig(
            host=host,
            port=_as_int(
                env.get("AGY_BRIDGE_PORT", server_raw.get("port", 8765)),
                name="server.port",
                minimum=1,
                maximum=65_535,
            ),
            token=token,
            token_file=Path(token_file_value).expanduser() if token_file_value else None,
            allow_remote=allow_remote,
            request_body_limit_bytes=_as_int(
                env.get(
                    "AGY_REQUEST_BODY_LIMIT_BYTES",
                    server_raw.get("request_body_limit_bytes", 16 * 1024 * 1024),
                ),
                name="server.request_body_limit_bytes",
                minimum=4_096,
                maximum=128 * 1024 * 1024,
            ),
            max_concurrent_requests=_as_int(
                env.get(
                    "AGY_MAX_CONCURRENT_REQUESTS",
                    server_raw.get("max_concurrent_requests", 4),
                ),
                name="server.max_concurrent_requests",
                minimum=1,
                maximum=64,
            ),
        )
        default_versions = ["1.1.17", "1.1.26", "1.1.27", "1.1.28", "1.1.29", "1.1.30"]
        validated_versions_value = agy_raw.get("validated_versions", default_versions)
        if not isinstance(validated_versions_value, list) or not validated_versions_value:
            raise ConfigurationError("antigravity.validated_versions must be a non-empty array")
        validated_versions = tuple(str(value).strip() for value in validated_versions_value)
        if any(not value for value in validated_versions):
            raise ConfigurationError("antigravity.validated_versions contains an empty version")
        allow_unvalidated_versions = _as_bool(
            env.get("AGY_ALLOW_UNVALIDATED_VERSIONS", agy_raw.get("allow_unvalidated_versions", False)),
            name="antigravity.allow_unvalidated_versions",
        )
        wrapper_raw = agy_raw.get("wrapper", [])
        if isinstance(wrapper_raw, str):
            wrapper = (wrapper_raw.strip(),) if wrapper_raw.strip() else ()
        elif isinstance(wrapper_raw, (list, tuple)):
            wrapper = tuple(str(x).strip() for x in wrapper_raw if str(x).strip())
        else:
            raise ConfigurationError("antigravity.wrapper must be an array of strings")

        antigravity = AntigravityConfig(
            binary=binary,
            default_model=str(
                env.get("AGY_DEFAULT_MODEL", agy_raw.get("default_model", "gemini-3.8-flash-high"))
            ).strip(),
            timeout_seconds=_as_int(
                env.get("AGY_PRINT_TIMEOUT", agy_raw.get("timeout_seconds", 300)),
                name="antigravity.timeout_seconds",
                minimum=1,
                maximum=3_600,
            ),
            sandbox=sandbox,
            mode=mode,
            enforce_tool_isolation=enforce_isolation,
            home=Path(
                str(
                    env.get(
                        "AGY_HOME",
                        agy_raw.get(
                            "home",
                            "~/.local/state/hermes-antigravity-bridge/agy-home",
                        ),
                    )
                )
            ).expanduser(),
            runtime_dir=runtime_dir,
            model_cache_ttl_seconds=_as_int(
                agy_raw.get("model_cache_ttl_seconds", 60),
                name="antigravity.model_cache_ttl_seconds",
                minimum=1,
                maximum=3_600,
            ),
            max_attempts=_as_int(
                agy_raw.get("max_attempts", 3),
                name="antigravity.max_attempts",
                minimum=1,
                maximum=5,
            ),
            validated_versions=validated_versions,
            allow_unvalidated_versions=allow_unvalidated_versions,
            wrapper=wrapper,
        )
        if not antigravity.default_model:
            raise ConfigurationError("antigravity.default_model must not be empty")

        include_prompt = _as_bool(
            logging_raw.get("include_prompt_content", False),
            name="logging.include_prompt_content",
        )
        if include_prompt:
            raise ConfigurationError("logging prompt content is forbidden")
        logging_config = LoggingConfig(
            level=str(logging_raw.get("level", "INFO")).upper(),
            include_prompt_content=False,
        )
        return cls(
            server=server,
            antigravity=antigravity,
            prompt=prompt,
            logging=logging_config,
        )
