import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from hermes_antigravity_bridge import __version__
from hermes_antigravity_bridge.cli import main


class CLITests(unittest.TestCase):
    def test_version(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn(__version__, output.getvalue())

    @patch("hermes_antigravity_bridge.cli.BridgeConfig.load")
    @patch("hermes_antigravity_bridge.cli.AntigravityBackend")
    def test_check_prints_sanitized_readiness(self, backend_type, load_config):
        config = MagicMock()
        config.logging.level = "INFO"
        load_config.return_value = config
        backend = backend_type.return_value
        backend.readiness.return_value = {
            "status": "ready",
            "agy_version": "1.1.28",
            "models": ["model-a"],
            "sandbox": True,
            "mode": "plan",
            "stateless": True,
        }
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--config", "/tmp/config.toml", "check"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "ready")
        load_config.assert_called_once_with("/tmp/config.toml")


if __name__ == "__main__":
    unittest.main()
