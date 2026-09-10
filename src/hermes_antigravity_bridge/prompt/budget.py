"""Prompt-budget calculation with explicit character/token units."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import ConfigurationError

DEFAULT_MODEL_CONTEXT_TOKENS: dict[str, int] = {
    # Google Gemini Series (1,000,000 tokens)
    "gemini-3.8-flash": 1_000_000,
    "gemini-3.8-flash-medium": 1_000_000,
    "gemini-3.8-flash-high": 1_000_000,
    "gemini-3.8-flash-low": 1_000_000,
    "gemini-3.7-flash": 1_000_000,
    "gemini-3.7-flash-medium": 1_000_000,
    "gemini-3.7-flash-high": 1_000_000,
    "gemini-3.6-flash": 1_000_000,
    "gemini-3.6-flash-medium": 1_000_000,
    "gemini-3.1-pro": 1_000_000,
    "gemini-3.1-pro-high": 1_000_000,
    "gemini-3.1-pro-low": 1_000_000,
    "gemini-2.5-pro": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    "antigravity": 1_000_000,
    "antigravity-flash": 1_000_000,
    "antigravity-flash-fast": 1_000_000,
    "antigravity-pro": 1_000_000,
    "flash": 1_000_000,
    "pro": 1_000_000,
    # Anthropic Claude Series (200,000 tokens)
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4.6": 200_000,
    "claude-sonnet-4-6-thinking": 200_000,
    "claude-sonnet-4.6-thinking": 200_000,
    "claude-opus-4-6": 200_000,
    "claude-opus-4.6": 200_000,
    "claude-opus-4-6-thinking": 200_000,
    "claude-opus-4.6-thinking": 200_000,
    "claude-3-7-sonnet": 200_000,
    "claude-3.7-sonnet": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3.5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
    # OpenAI Series (128,000 tokens)
    "gpt-oss-120b": 128_000,
    "gpt-oss-120b-medium": 128_000,
    "gpt-oss": 128_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "o3-mini": 128_000,
    "o1": 128_000,
}


@dataclass(frozen=True)
class PromptBudget:
    max_chars: int = 4_000_000
    output_token_reserve: int = 8_192
    chars_per_token: int = 4
    model_context_tokens: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_chars < 4_096:
            raise ConfigurationError("prompt.max_chars must be at least 4096")
        if self.output_token_reserve < 0:
            raise ConfigurationError("prompt.output_token_reserve must not be negative")
        if self.chars_per_token < 1:
            raise ConfigurationError("prompt.chars_per_token must be at least 1")
        for model, limit in self.model_context_tokens.items():
            if not model or not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                raise ConfigurationError("model context limits must be positive integers")

    def _lookup_provider_tokens(self, model: str) -> int | None:
        if not model:
            return None
        norm = model.lower().strip()
        if norm in self.model_context_tokens:
            return self.model_context_tokens[norm]
        if model in self.model_context_tokens:
            return self.model_context_tokens[model]
        if norm in DEFAULT_MODEL_CONTEXT_TOKENS:
            return DEFAULT_MODEL_CONTEXT_TOKENS[norm]
        stripped = norm.removeprefix("models/").strip()
        if stripped in DEFAULT_MODEL_CONTEXT_TOKENS:
            return DEFAULT_MODEL_CONTEXT_TOKENS[stripped]
        # Dynamic fallback based on model family for future models (e.g. gemini-3.9, gemini-4.0, claude-4.0)
        if "gemini" in norm or "antigravity" in norm:
            return 1_000_000
        if "claude" in norm:
            return 200_000
        if "gpt" in norm or "o1" in norm or "o3" in norm:
            return 128_000
        return None

    def effective_chars(self, model: str, requested_output_tokens: object = None) -> int:
        provider_tokens = self._lookup_provider_tokens(model)
        if not provider_tokens:
            return self.max_chars
        try:
            requested = int(requested_output_tokens or 0)
        except (TypeError, ValueError):
            requested = 0
        reserve = max(self.output_token_reserve, requested)
        effective_tokens = provider_tokens - reserve
        effective_chars = effective_tokens * self.chars_per_token
        if effective_chars < 4_096:
            raise ConfigurationError(
                f"provider context limit ({provider_tokens}) minus output reserve "
                f"({reserve}) leaves insufficient budget ({effective_chars} < 4096 chars)"
            )
        return min(self.max_chars, effective_chars)
