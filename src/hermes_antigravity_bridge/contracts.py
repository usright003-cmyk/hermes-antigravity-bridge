"""Transport-neutral contracts shared by the Hermes and Antigravity sides."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class BackendResponse:
    response: str
    model: str
    usage: JsonObject = field(default_factory=dict)
    status: str = "SUCCESS"
    duration_seconds: float = 0.0
    conversation_id: str | None = None


@dataclass(frozen=True)
class ParsedAssistantOutput:
    text: str
    tool_calls: tuple[JsonObject, ...] = ()


@dataclass(frozen=True)
class ChatCompletionResult:
    text: str
    tool_calls: tuple[JsonObject, ...]
    usage: JsonObject
    requested_model: str
    actual_model: str


class TextBackend(Protocol):
    def list_models(self, *, force_refresh: bool = False) -> tuple[str, ...]: ...

    def resolve_model(self, requested: str) -> str: ...

    def generate(self, prompt: str, model: str) -> BackendResponse: ...

    def readiness(self) -> JsonObject: ...


Messages = Sequence[JsonObject]
Tools = Sequence[JsonObject]
