import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from hermes_antigravity_bridge.config import BridgeConfig
from hermes_antigravity_bridge.errors import ConfigurationError


class BridgeConfigTests(unittest.TestCase):
    def base_mapping(self):
        return {
            "server": {"host": "127.0.0.1", "port": 8765},
            "antigravity": {
                "binary": sys.executable,
                "sandbox": True,
                "mode": "plan",
            },
            "prompt": {"max_chars": 64000, "output_token_reserve": 8192},
        }

    def test_requires_a_nondefault_secret(self):
        with self.assertRaisesRegex(ConfigurationError, "bearer token"):
            BridgeConfig.from_mapping(self.base_mapping(), environ={})
        with self.assertRaisesRegex(ConfigurationError, "insecure published default"):
            BridgeConfig.from_mapping(
                self.base_mapping(), environ={"AGY_BRIDGE_TOKEN": "local-antigravity"}
            )

    def test_loads_secret_from_mode_0600_token_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "bridge.token"
            token_file.write_text("a-secure-random-looking-token-value\n", encoding="utf-8")
            token_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
            mapping = self.base_mapping()
            mapping["server"]["token_file"] = str(token_file)
            config = BridgeConfig.from_mapping(mapping, environ={})
            self.assertEqual(config.server.token, "a-secure-random-looking-token-value")
            self.assertEqual(config.prompt.max_chars, 64000)
            self.assertEqual(config.server.max_concurrent_requests, 4)
            self.assertTrue(config.antigravity.sandbox)
            self.assertEqual(config.antigravity.mode, "plan")

    @unittest.skipUnless(os.name == "posix", "POSIX file permission mode check")
    def test_rejects_group_readable_token_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "bridge.token"
            token_file.write_text("a-secure-random-looking-token-value\n", encoding="utf-8")
            token_file.chmod(0o640)
            mapping = self.base_mapping()
            mapping["server"]["token_file"] = str(token_file)
            with self.assertRaisesRegex(ConfigurationError, "0600"):
                BridgeConfig.from_mapping(mapping, environ={})

    def test_rejects_remote_binding_without_explicit_opt_in(self):
        mapping = self.base_mapping()
        mapping["server"]["host"] = "0.0.0.0"
        with self.assertRaisesRegex(ConfigurationError, "allow_remote"):
            BridgeConfig.from_mapping(
                mapping,
                environ={"AGY_BRIDGE_TOKEN": "a-secure-random-looking-token-value"},
            )

    def test_environment_overrides_nonsecret_settings(self):
        config = BridgeConfig.from_mapping(
            self.base_mapping(),
            environ={
                "AGY_BRIDGE_TOKEN": "a-secure-random-looking-token-value",
                "AGY_BRIDGE_PORT": "9876",
                "AGY_MAX_PROMPT_CHARS": "62000",
                "AGY_MAX_CONCURRENT_REQUESTS": "3",
            },
        )
        self.assertEqual(config.server.port, 9876)
        self.assertEqual(config.prompt.max_chars, 62000)
        self.assertEqual(config.server.max_concurrent_requests, 3)

    def test_requires_fail_closed_tool_isolation(self):
        mapping = self.base_mapping()
        mapping["antigravity"]["sandbox"] = False
        with self.assertRaisesRegex(ConfigurationError, "sandbox"):
            BridgeConfig.from_mapping(
                mapping,
                environ={"AGY_BRIDGE_TOKEN": "a-secure-random-looking-token-value"},
            )


if __name__ == "__main__":
    unittest.main()
