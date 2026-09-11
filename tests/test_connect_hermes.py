import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import connect_hermes


class TestConnectHermes(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.home_dir = self.root / "home"
        self.home_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_dump_and_parse_yaml_fallback_roundtrip(self):
        sample_data = {
            "model": {
                "default": "claude-3-5-sonnet",
                "provider": "anthropic",
                "base_url": "https://api.anthropic.com",
            },
            "custom_providers": [
                {
                    "name": "ollama",
                    "base_url": "http://localhost:11434/v1",
                    "api_key": "ollama",
                    "api_mode": "chat_completions",
                }
            ],
            "fallback_providers": [
                {
                    "provider": "groq",
                    "model": "llama-3.3-70b",
                }
            ],
            "temperature": 0.7,
            "streaming": True,
            "max_tokens": 4096,
            "null_field": None,
        }

        dumped = connect_hermes.dump_yaml_fallback(sample_data)
        parsed = connect_hermes.parse_yaml_fallback(dumped)

        self.assertEqual(parsed["model"]["default"], "claude-3-5-sonnet")
        self.assertEqual(parsed["model"]["provider"], "anthropic")
        self.assertEqual(len(parsed["custom_providers"]), 1)
        self.assertEqual(parsed["custom_providers"][0]["name"], "ollama")
        self.assertEqual(len(parsed["fallback_providers"]), 1)
        self.assertEqual(parsed["fallback_providers"][0]["provider"], "groq")
        self.assertEqual(parsed["temperature"], 0.7)
        self.assertTrue(parsed["streaming"])
        self.assertEqual(parsed["max_tokens"], 4096)
        self.assertIsNone(parsed["null_field"])

    def test_is_antigravity_authenticated(self):
        with patch.object(Path, "home", return_value=self.home_dir):
            # Not authenticated when file doesn't exist
            self.assertFalse(connect_hermes.is_antigravity_authenticated())

            cli_dir = self.home_dir / ".gemini" / "antigravity-cli"
            cli_dir.mkdir(parents=True, exist_ok=True)
            jetski = cli_dir / "jetski_state.pbtxt"

            # Not authenticated when file is empty (0 bytes)
            jetski.write_text("", encoding="utf-8")
            self.assertFalse(connect_hermes.is_antigravity_authenticated())

            # Authenticated when file has content
            jetski.write_text("access_token: 'fake-token'\n", encoding="utf-8")
            self.assertTrue(connect_hermes.is_antigravity_authenticated())

    def test_ensure_antigravity_auth_already_authenticated(self):
        with patch.object(Path, "home", return_value=self.home_dir):
            cli_dir = self.home_dir / ".gemini" / "antigravity-cli"
            cli_dir.mkdir(parents=True, exist_ok=True)
            jetski = cli_dir / "jetski_state.pbtxt"
            jetski.write_text("logged_in: true\n", encoding="utf-8")

            with patch("subprocess.run") as mock_run:
                result = connect_hermes.ensure_antigravity_auth("agy")
                self.assertTrue(result)
                mock_run.assert_not_called()

    def test_ensure_antigravity_auth_runs_interactive_and_polls(self):
        with patch.object(Path, "home", return_value=self.home_dir), patch("shutil.which", return_value="/mock/bin/agy"):
            cli_dir = self.home_dir / ".gemini" / "antigravity-cli"
            cli_dir.mkdir(parents=True, exist_ok=True)
            jetski = cli_dir / "jetski_state.pbtxt"

            def simulate_login(*args, **kwargs):
                jetski.write_text("logged_in: true\n", encoding="utf-8")
                return MagicMock(returncode=0)

            with patch("subprocess.run", side_effect=simulate_login) as mock_run:
                result = connect_hermes.ensure_antigravity_auth("agy", poll_timeout_seconds=5)
                self.assertTrue(result)
                mock_run.assert_called_once_with(["/mock/bin/agy"], check=False)

    def test_setup_bridge_default_sync_credentials_true(self):
        with patch.object(Path, "home", return_value=self.home_dir):
            user_cli = self.home_dir / ".gemini" / "antigravity-cli"
            user_cli.mkdir(parents=True, exist_ok=True)
            (user_cli / "installation_id").write_text("user-install-id", encoding="utf-8")
            (user_cli / "jetski_state.pbtxt").write_text("token-data", encoding="utf-8")

            config_file, token = connect_hermes.setup_bridge()  # default sync_credentials=True

            self.assertTrue(config_file.exists())
            self.assertTrue(bool(token))
            config_text = config_file.read_text(encoding="utf-8")
            self.assertIn("sync_user_credentials = true", config_text)

            # Check credentials copied into isolated sandbox profile
            iso_cli = (
                self.home_dir
                / ".local"
                / "state"
                / "hermes-antigravity-bridge"
                / "agy-home"
                / ".gemini"
                / "antigravity-cli"
            )
            self.assertTrue((iso_cli / "installation_id").exists())
            self.assertEqual((iso_cli / "installation_id").read_text(encoding="utf-8"), "user-install-id")
            self.assertTrue((iso_cli / "jetski_state.pbtxt").exists())
            self.assertEqual((iso_cli / "jetski_state.pbtxt").read_text(encoding="utf-8"), "token-data")

    def test_setup_bridge_sync_credentials_false(self):
        with patch.object(Path, "home", return_value=self.home_dir):
            user_cli = self.home_dir / ".gemini" / "antigravity-cli"
            user_cli.mkdir(parents=True, exist_ok=True)
            (user_cli / "installation_id").write_text("user-install-id", encoding="utf-8")
            (user_cli / "jetski_state.pbtxt").write_text("token-data", encoding="utf-8")

            config_file, _ = connect_hermes.setup_bridge(sync_credentials=False)

            self.assertTrue(config_file.exists())
            config_text = config_file.read_text(encoding="utf-8")
            self.assertIn("sync_user_credentials = false", config_text)

            iso_cli = (
                self.home_dir
                / ".local"
                / "state"
                / "hermes-antigravity-bridge"
                / "agy-home"
                / ".gemini"
                / "antigravity-cli"
            )
            self.assertFalse((iso_cli / "jetski_state.pbtxt").exists())

    def test_update_hermes_config_preserves_existing_provider_in_fallbacks(self):
        cfg_path = self.root / "hermes" / "config.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        initial_yaml = (
            "model:\n"
            '  default: "gpt-4o"\n'
            '  provider: "openai-codex"\n'
            '  base_url: "https://api.openai.com/v1"\n'
            "custom_providers:\n"
            '  - name: "ollama"\n'
            '    base_url: "http://localhost:11434/v1"\n'
            '    api_key: "ollama"\n'
            "temperature: 0.8\n"
        )
        cfg_path.write_text(initial_yaml, encoding="utf-8")

        preserved = connect_hermes.update_hermes_config(cfg_path, token="test-token-123")

        self.assertTrue(any("openai-codex" in p for p in preserved))
        self.assertTrue(any("ollama" in p for p in preserved))

        # Check backup file created
        backup_path = cfg_path.with_suffix(".yaml.bak")
        self.assertTrue(backup_path.exists())

        # Check .env created with token
        env_path = cfg_path.parent / ".env"
        self.assertTrue(env_path.exists())
        self.assertIn("HERMES_ANTIGRAVITY_BRIDGE_TOKEN=test-token-123", env_path.read_text(encoding="utf-8"))

        # Verify parsed result
        parsed = connect_hermes.parse_yaml_fallback(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["model"]["default"], "gemini-3.8-flash")
        self.assertEqual(parsed["model"]["provider"], "custom:antigravity")
        self.assertEqual(parsed["model"]["base_url"], "http://127.0.0.1:8765/v1")

        # Fallbacks list preserved and contains old provider
        fallbacks = parsed.get("fallback_providers")
        self.assertIsInstance(fallbacks, list)
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(fallbacks[0]["provider"], "openai-codex")
        self.assertEqual(fallbacks[0]["model"], "gpt-4o")
        self.assertEqual(fallbacks[0]["base_url"], "https://api.openai.com/v1")

        # Custom providers preserved
        custom = parsed.get("custom_providers")
        names = [p["name"] for p in custom]
        self.assertIn("ollama", names)
        self.assertIn("antigravity", names)

        # Other top-level keys preserved
        self.assertEqual(parsed.get("temperature"), 0.8)

    def test_update_hermes_config_pure_python_fallback(self):
        cfg_path = self.root / "hermes_no_yaml" / "config.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        initial_yaml = """model:
  default: "claude-3-5-sonnet"
  provider: "anthropic"
"""
        cfg_path.write_text(initial_yaml, encoding="utf-8")

        # Force yaml=None to verify pure-Python fallback execution
        with patch.object(connect_hermes, "yaml", None):
            preserved = connect_hermes.update_hermes_config(cfg_path, token="pure-python-token")
            self.assertTrue(any("anthropic" in p for p in preserved))

            self.assertTrue(cfg_path.exists())
            content = cfg_path.read_text(encoding="utf-8")
            self.assertIn("custom:antigravity", content)
            self.assertIn("fallback_providers", content)
            self.assertIn("anthropic", content)

    def test_create_launchers(self):
        repo_root = self.root / "repo"
        repo_root.mkdir(parents=True, exist_ok=True)

        bat = connect_hermes.create_launcher_batch(repo_root)
        self.assertTrue(bat.exists())
        bat_text = bat.read_text(encoding="utf-8")
        self.assertIn(f'cd /d "{repo_root}"', bat_text)
        self.assertIn("hermes_antigravity_bridge.cli", bat_text)

        lan_bat = connect_hermes.create_lan_launcher_batch(repo_root)
        self.assertTrue(lan_bat.exists())
        lan_text = lan_bat.read_text(encoding="utf-8")
        self.assertIn("AGY_BRIDGE_HOST=0.0.0.0", lan_text)
        self.assertIn(f'cd /d "{repo_root}"', lan_text)

        vbs = connect_hermes.create_background_launcher_vbs(repo_root)
        self.assertTrue(vbs.exists())
        vbs_text = vbs.read_text(encoding="utf-8")
        self.assertIn("WScript.Shell", vbs_text)
        self.assertIn(str(repo_root), vbs_text)
        self.assertIn("PYTHONPATH", vbs_text)
        self.assertIn(", 0, False", vbs_text)

    def test_yaml_fallback_empty_collections_and_scalars(self):
        data = {
            "empty_dict": {},
            "empty_list": [],
            "urls": ["https://example.com/api", "http://127.0.0.1:8765"],
            "paths": [r"C:\Users\HP\workspace", r"E:\hermes"],
            "nested_list_of_dicts": [
                {
                    "name": "test",
                    "subdict": {},
                    "sublist": [],
                    "info": {"active": True},
                }
            ],
        }
        dumped = connect_hermes.dump_yaml_fallback(data)
        parsed = connect_hermes.parse_yaml_fallback(dumped)

        self.assertEqual(parsed["empty_dict"], {})
        self.assertEqual(parsed["empty_list"], [])
        self.assertEqual(parsed["urls"], ["https://example.com/api", "http://127.0.0.1:8765"])
        self.assertEqual(parsed["paths"], [r"C:\Users\HP\workspace", r"E:\hermes"])
        self.assertEqual(parsed["nested_list_of_dicts"][0]["subdict"], {})
        self.assertEqual(parsed["nested_list_of_dicts"][0]["sublist"], [])
        self.assertTrue(parsed["nested_list_of_dicts"][0]["info"]["active"])

    def test_yaml_fallback_strips_inline_comments(self):
        yaml_text = """model:
  default: gpt-4o # primary model
  provider: openai # company
temperature: 0.7 # sampling temp
"""
        parsed = connect_hermes.parse_yaml_fallback(yaml_text)
        self.assertEqual(parsed["model"]["default"], "gpt-4o")
        self.assertEqual(parsed["model"]["provider"], "openai")
        self.assertEqual(parsed["temperature"], 0.7)

    def test_update_hermes_config_string_model_and_dict_custom_providers(self):
        cfg_path = self.root / "hermes_str_model" / "config.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        initial_yaml = """model: "gpt-4o"
provider: "openai"
base_url: "https://api.openai.com/v1"
custom_providers:
  local_llm:
    base_url: "http://localhost:8000/v1"
    api_key: "test-key"
"""
        cfg_path.write_text(initial_yaml, encoding="utf-8")

        preserved = connect_hermes.update_hermes_config(cfg_path, token="token-xyz")
        self.assertTrue(any("openai" in p for p in preserved))
        self.assertTrue(any("local_llm" in p for p in preserved))

        parsed = connect_hermes.parse_yaml_fallback(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["model"]["default"], "gemini-3.8-flash")
        self.assertEqual(parsed["model"]["provider"], "custom:antigravity")

        fallbacks = parsed["fallback_providers"]
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(fallbacks[0]["provider"], "openai")
        self.assertEqual(fallbacks[0]["model"], "gpt-4o")
        self.assertEqual(fallbacks[0]["base_url"], "https://api.openai.com/v1")

        custom_names = [p["name"] for p in parsed["custom_providers"]]
        self.assertIn("local_llm", custom_names)
        self.assertIn("antigravity", custom_names)

    def test_setup_agy_isolation_home_same_file_safety(self):
        # When home is set such that user_cli is the same as agy_home cli, should not raise SameFileError
        same_dir = self.root / "same_home"
        same_dir.mkdir(parents=True, exist_ok=True)
        with patch.object(Path, "home", return_value=same_dir):
            # Normal run
            agy_home = connect_hermes.setup_agy_isolation_home(sync_credentials=True)
            self.assertTrue(agy_home.exists())

    def test_get_desktop_dir_onedrive_fallback(self):
        onedrive_desktop = self.root / "OneDrive" / "Desktop"
        onedrive_desktop.mkdir(parents=True, exist_ok=True)
        with patch("sys.platform", "win32"), patch.dict("os.environ", {"OneDrive": str(self.root / "OneDrive")}):
            desktop = connect_hermes.get_desktop_dir()
            self.assertEqual(desktop, onedrive_desktop)

    def test_setup_completion_banner_authenticated_vs_pending(self):
        # 1. When authenticated is True
        with (
            patch("connect_hermes.is_antigravity_authenticated", return_value=True),
            patch("connect_hermes.shutil.which", return_value="/usr/bin/agy"),
            patch("connect_hermes.ensure_antigravity_auth", return_value=True),
            patch("connect_hermes.setup_bridge", return_value=(self.root / "bridge.toml", "secret-token-123456789")),
            patch("connect_hermes.find_hermes_config_path", return_value=None),
            patch("connect_hermes.create_launcher_batch", return_value=self.root / "run.bat"),
            patch("connect_hermes.create_lan_launcher_batch", return_value=self.root / "run-lan.bat"),
            patch("connect_hermes.create_background_launcher_vbs", return_value=self.root / "run.vbs"),
            patch("connect_hermes.create_desktop_launchers", return_value=[]),
            patch("connect_hermes.get_lan_ip", return_value="127.0.0.1"),
            patch("sys.argv", ["connect_hermes.py"]),
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                exit_code = connect_hermes.main()
            self.assertEqual(exit_code, 0)
            text = out.getvalue()
            self.assertIn("SUCCESS! Hermes Agent is now fully connected to Antigravity!", text)
            self.assertNotIn("SETUP COMPLETE (AUTHENTICATION PENDING)", text)

        # 2. When authenticated is False
        with (
            patch("connect_hermes.is_antigravity_authenticated", return_value=False),
            patch("connect_hermes.shutil.which", return_value="/usr/bin/agy"),
            patch("connect_hermes.ensure_antigravity_auth", return_value=False),
            patch("connect_hermes.setup_bridge", return_value=(self.root / "bridge.toml", "secret-token-123456789")),
            patch("connect_hermes.find_hermes_config_path", return_value=None),
            patch("connect_hermes.create_launcher_batch", return_value=self.root / "run.bat"),
            patch("connect_hermes.create_lan_launcher_batch", return_value=self.root / "run-lan.bat"),
            patch("connect_hermes.create_background_launcher_vbs", return_value=self.root / "run.vbs"),
            patch("connect_hermes.create_desktop_launchers", return_value=[]),
            patch("connect_hermes.get_lan_ip", return_value="127.0.0.1"),
            patch("sys.argv", ["connect_hermes.py"]),
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                exit_code = connect_hermes.main()
            self.assertEqual(exit_code, 0)
            text = out.getvalue()
            self.assertIn("SETUP COMPLETE (AUTHENTICATION PENDING)", text)
            self.assertIn("Please run 'agy' in your terminal to complete Google authentication.", text)
            self.assertNotIn("SUCCESS! Hermes Agent is now fully connected to Antigravity!", text)

    def test_lan_instructions_mask_token_and_advise_env(self):
        raw_token = "secret-token-123456789"
        with (
            patch("connect_hermes.is_antigravity_authenticated", return_value=True),
            patch("connect_hermes.shutil.which", return_value="/usr/bin/agy"),
            patch("connect_hermes.ensure_antigravity_auth", return_value=True),
            patch("connect_hermes.setup_bridge", return_value=(self.root / "bridge.toml", raw_token)),
            patch("connect_hermes.find_hermes_config_path", return_value=None),
            patch("connect_hermes.create_launcher_batch", return_value=self.root / "run.bat"),
            patch("connect_hermes.create_lan_launcher_batch", return_value=self.root / "run-lan.bat"),
            patch("connect_hermes.create_background_launcher_vbs", return_value=self.root / "run.vbs"),
            patch("connect_hermes.create_desktop_launchers", return_value=[]),
            patch("connect_hermes.get_lan_ip", return_value="192.168.1.150"),
            patch("sys.argv", ["connect_hermes.py"]),
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                connect_hermes.main()
            text = out.getvalue()
            # Must NOT expose raw plaintext token in the curl bash command
            self.assertNotIn(f"--token {raw_token}", text)
            # Must mask token in banner
            self.assertIn("secr...6789", text)
            # Must advise passing HERMES_ANTIGRAVITY_BRIDGE_TOKEN
            self.assertIn("HERMES_ANTIGRAVITY_BRIDGE_TOKEN", text)

    def test_ensure_antigravity_auth_pending_notice(self):
        with (
            patch("connect_hermes.is_antigravity_authenticated", return_value=False),
            patch("subprocess.run"),
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                res = connect_hermes.ensure_antigravity_auth("agy", poll_timeout_seconds=0)
            self.assertFalse(res)
            text = out.getvalue()
            self.assertIn("Google authentication pending activation", text)
            self.assertIn("run 'agy'", text)

    def test_supported_antigravity_models_catalog(self):
        from hermes_antigravity_bridge.backends.antigravity import (
            DEFAULT_ANTIGRAVITY_MODELS,
        )

        expected_gemini = [
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
        ]
        expected_claude = [
            "claude-sonnet-4-6",
            "claude-opus-4-6",
            "claude-opus-4-6-thinking",
        ]
        expected_gpt = [
            "gpt-oss-120b",
            "gpt-oss-120b-medium",
        ]
        expected_all = tuple(expected_gemini + expected_claude + expected_gpt)

        self.assertEqual(len(connect_hermes.SUPPORTED_ANTIGRAVITY_MODELS), 20)
        self.assertEqual(connect_hermes.SUPPORTED_ANTIGRAVITY_MODELS, expected_all)
        self.assertEqual(DEFAULT_ANTIGRAVITY_MODELS, expected_all)

    def test_update_hermes_config_writes_api_key_and_model_list(self):
        cfg_path = self.root / "hermes_full" / "config.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("model: 'old-model'\n", encoding="utf-8")

        connect_hermes.update_hermes_config(cfg_path, token="test-token-12345")
        parsed = connect_hermes.parse_yaml_fallback(cfg_path.read_text(encoding="utf-8"))

        custom = parsed.get("custom_providers")
        self.assertIsInstance(custom, list)
        agy_entry = next((p for p in custom if p.get("name") == "antigravity"), None)
        self.assertIsNotNone(agy_entry)
        self.assertEqual(agy_entry.get("api_key"), "test-token-12345")
        self.assertEqual(agy_entry.get("model"), "gemini-3.8-flash")
        self.assertEqual(agy_entry.get("models"), list(connect_hermes.SUPPORTED_ANTIGRAVITY_MODELS))

    def test_create_desktop_launchers_is_self_contained(self):
        fake_desktop = self.root / "desktop"
        fake_desktop.mkdir(parents=True, exist_ok=True)
        repo_dir = self.root / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)

        with patch("connect_hermes.get_desktop_dir", return_value=fake_desktop):
            result = connect_hermes.create_desktop_launchers(repo_dir)
            self.assertEqual(result, [])
            # Assert desktop directory remains completely untouched
            self.assertEqual(list(fake_desktop.iterdir()), [])


if __name__ == "__main__":
    unittest.main()


