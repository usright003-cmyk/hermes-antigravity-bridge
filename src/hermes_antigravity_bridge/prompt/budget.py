"""Prompt-budget calculation with explicit character/token units."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import ConfigurationError


@dataclass(frozen=True)
class PromptBudget:
    max_chars: int = 64_000
    output_token_reserve: int = 8_192
    model_context_tokens: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_chars < 4_096:
            raise ConfigurationError("prompt.max_chars must be at least 4096")
        if self.output_token_reserve < 0:
            raise ConfigurationError("prompt.output_token_reserve must not be negative")
        for model, limit in self.model_context_tokens.items():
            if not model or not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                raise ConfigurationError("model context limits must be positive integers")

    def effective_chars(self, model: str, requested_output_tokens: object = None) -> int:
        provider_tokens = self.model_context_tokens.get(model)
        if not provider_tokens:
            return self.max_chars
        try:
            requested = int(requested_output_tokens or 0)
        except (TypeError, ValueError):
            requested = 0
        reserve = max(self.output_token_reserve, requested)
        effective = provider_tokens - reserve
        if effective < 4_096:
            raise ConfigurationError(
                f"provider context limit ({provider_tokens}) minus output reserve "
                f"({reserve}) leaves insufficient budget ({effective} < 4096)"
            )
        return min(self.max_chars, effective)
