"""Command-line entry point and composition root."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence

from . import __version__
from .backends.antigravity import AntigravityBackend
from .config import BridgeConfig
from .errors import BridgeError, ConfigurationError
from .integrations.hermes import HermesPromptBuilder
from .openai_http import create_http_server
from .service import ChatCompletionService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-antigravity-bridge",
        description="Hermes Agent integration for the Antigravity CLI",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        default=None,
        help="TOML configuration path (default: ~/.config/hermes-antigravity-bridge/config.toml)",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("serve", "Run the local HTTP bridge"),
        ("check", "Validate configuration, CLI compatibility, and account readiness"),
        ("models", "List available Antigravity model IDs"),
    ):
        sub = subcommands.add_parser(name, help=help_text)
        sub.add_argument("--config", default=argparse.SUPPRESS, help="TOML configuration path")
    return parser


def _components(config: BridgeConfig) -> tuple[AntigravityBackend, ChatCompletionService]:
    backend = AntigravityBackend(config.antigravity)
    service = ChatCompletionService(
        backend=backend,
        prompt_builder=HermesPromptBuilder(),
        prompt_budget=config.prompt,
        tool_call_mode=config.antigravity.tool_call_mode,
    )
    return backend, service


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = BridgeConfig.load(args.config)
        logging.basicConfig(
            level=getattr(logging, config.logging.level, logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        backend, service = _components(config)
        if args.command == "check":
            print(json.dumps(backend.readiness(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "models":
            for model in backend.list_models(force_refresh=True):
                print(model)
            return 0
        backend.readiness()
        server = create_http_server(config, service)
        logging.getLogger(__name__).info(
            "listening on http://%s:%d (version=%s, max_prompt_chars=%d)",
            config.server.host,
            server.server_address[1],
            __version__,
            config.prompt.max_chars,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except (BridgeError, ConfigurationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
