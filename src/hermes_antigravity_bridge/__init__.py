"""Hermes Agent integration for the Antigravity CLI."""

__version__ = "0.1.0"

from .config import BridgeConfig
from .integrations.hermes import HermesPromptBuilder

__all__ = ["BridgeConfig", "HermesPromptBuilder", "__version__"]
