import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_antigravity_bridge.backends.antigravity import AntigravityBackend
from hermes_antigravity_bridge.config import AntigravityConfig
from hermes_antigravity_bridge.errors import (
    BackendProtocolError,
    BackendTimeout,
    ToolIsolationError,
    UnknownModel,
)

FAKE_AGY = Path(__file__).parent / "fake_agy.py"


class AntigravityBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            FAKE_AGY.chmod(FAKE_AGY.stat().st_mode | stat.S_IXUSR)
        except OSError:
            pass

    def make_backend(self, runtime: Path, **overrides):
        runtime.mkdir(parents=True, exist_ok=True)
        agy_home = runtime / "agy-home"
        settings = agy_home / ".gemini/antigravity-cli/settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({
            "artifactReviewPolicy": "asks-for-review",
            "permissions": {"allow": []},
            "toolPermission": "strict",
            "trustedWorkspaces": [],
        }), encoding="utf-8")
        jetski = agy_home / ".gemini/antigravity-cli/jetski_state.pbtxt"
        jetski.write_text("fake_auth_token: 12345\n", encoding="utf-8")
        values = {
            "binary": FAKE_AGY,
            "default_model": "gemini-test-high",
            "timeout_seconds": 2,
            "sandbox": True,
            "mode": "plan",
            "enforce_tool_isolation": True,
            "home": agy_home,
            "runtime_dir": runtime,
            "model_cache_ttl_seconds": 60,
            "max_attempts": 1,
        }
        values.update(overrides)
        return AntigravityBackend(AntigravityConfig(**values))

    def test_command_is_stateless_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.make_backend(Path(tmp))
            command = backend.build_command("gemini-test-low")
            self.assertIn("--sandbox", command)
            self.assertEqual(command[command.index("--mode") + 1], "plan")
            self.assertIn("--disable-slash-commands", command)
            self.assertNotIn("--continue", command)
            self.assertNotIn("--conversation", command)
            self.assertNotIn("--print", command)

    def test_model_discovery_rejects_unknown_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.make_backend(Path(tmp))
            self.assertEqual(backend.list_models(), ("gemini-test-high", "gemini-test-low"))
            self.assertEqual(backend.resolve_model("gemini-test-low"), "gemini-test-low")
            with self.assertRaisesRegex(UnknownModel, "unknown model"):
                backend.resolve_model("invented-model")

    def test_model_alias_resolution_matches_dropdown_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.make_backend(Path(tmp))
            with patch.object(
                backend,
                "list_models",
                return_value=(
                    "gemini-3.8-flash-high",
                    "gemini-3.7-flash-medium",
                    "gemini-3.6-flash-medium",
                    "gemini-3.1-pro-low",
                    "claude-sonnet-4-6",
                    "claude-opus-4-6",
                    "gpt-oss-120b",
                ),
            ):
                self.assertEqual(backend.resolve_model("Gemini 3.8 Flash"), "gemini-3.8-flash-high")
                self.assertEqual(backend.resolve_model("gemini-3.7-flash"), "gemini-3.7-flash-medium")
                self.assertEqual(backend.resolve_model("Gemini 3.6 Flash"), "gemini-3.6-flash-medium")
                self.assertEqual(backend.resolve_model("Gemini 3.1 Pro"), "gemini-3.1-pro-low")
                self.assertEqual(backend.resolve_model("Claude Sonnet 4.6 (Thinking)"), "claude-sonnet-4-6")
                self.assertEqual(backend.resolve_model("claude-sonnet-4.6"), "claude-sonnet-4-6")
                self.assertEqual(backend.resolve_model("Claude Opus 4.6 (Thinking)"), "claude-opus-4-6")
                self.assertEqual(backend.resolve_model("GPT-OSS 120B (Medium)"), "gpt-oss-120b")
                self.assertEqual(backend.resolve_model("gpt-oss-120b"), "gpt-oss-120b")

    def test_generate_uses_ephemeral_empty_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            record = Path(tmp) / "record.json"
            backend = self.make_backend(runtime)
            with patch.dict(os.environ, {"FAKE_AGY_RECORD": str(record)}):
                response = backend.generate("hello", "gemini-test-high")
            self.assertEqual(response.response, "FAKE_OK")
            recorded = json.loads(record.read_text(encoding="utf-8"))
            request_cwd = Path(recorded["cwd"])
            self.assertEqual(request_cwd.parent.resolve(), runtime.resolve())
            self.assertFalse(request_cwd.exists())
            self.assertEqual(Path(recorded["home"]).resolve(), backend.config.home.resolve())
            self.assertIn("--sandbox", recorded["argv"])

    def test_false_success_is_an_upstream_protocol_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.make_backend(Path(tmp))
            with self.assertRaisesRegex(BackendProtocolError, "false-success"):
                backend.generate("FAKE_EMPTY", "gemini-test-high")

    def test_internal_tool_activity_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.make_backend(Path(tmp))
            with self.assertRaisesRegex(ToolIsolationError, "internal tool"):
                backend.generate("FAKE_TOOL_EVENT", "gemini-test-high")

    def test_timeout_terminates_the_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.make_backend(Path(tmp), timeout_seconds=1)
            with self.assertRaises(BackendTimeout):
                backend.generate("FAKE_SLEEP", "gemini-test-high")

    def test_rejects_unsafe_antigravity_permission_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(json.dumps({
                "artifactReviewPolicy": "always-proceed",
                "permissions": {"allow": ["command(*)"]},
                "toolPermission": "always-proceed",
                "trustedWorkspaces": [str(Path(tmp))],
            }), encoding="utf-8")
            backend = self.make_backend(Path(tmp) / "runtime")
            with self.assertRaisesRegex(ToolIsolationError, "strict"):
                backend.verify_tool_isolation_settings(settings)

    def test_rejects_unvalidated_cli_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.make_backend(Path(tmp))
            with (
                patch.dict(os.environ, {"FAKE_AGY_VERSION": "9.9.9"}),
                self.assertRaisesRegex(BackendProtocolError, "validated"),
            ):
                backend.readiness()

    def test_readiness_checks_required_cli_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.make_backend(Path(tmp))
            readiness = backend.readiness()
            self.assertEqual(readiness["status"], "ready")
            self.assertTrue(readiness["authenticated"])
            self.assertEqual(readiness["model_source"], "discovered")
            self.assertEqual(readiness["models"], ["gemini-test-high", "gemini-test-low"])

    def test_readiness_reports_degraded_when_unauthenticated(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.make_backend(Path(tmp))
            jetski = backend.config.home / ".gemini/antigravity-cli/jetski_state.pbtxt"
            if jetski.exists():
                jetski.unlink()
            with patch("pathlib.Path.home", return_value=Path(tmp) / "empty_home"):
                readiness = backend.readiness()
                self.assertEqual(readiness["status"], "degraded")
                self.assertFalse(readiness["authenticated"])
                self.assertIn("reason", readiness)

    def test_rejects_invalid_artifact_review_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(json.dumps({
                "artifactReviewPolicy": "request-review",
                "permissions": {"allow": []},
                "toolPermission": "strict",
                "trustedWorkspaces": [],
            }), encoding="utf-8")
            backend = self.make_backend(Path(tmp) / "runtime")
            with self.assertRaisesRegex(ToolIsolationError, "artifactReviewPolicy"):
                backend.verify_tool_isolation_settings(settings)

    def test_wrapper_prepends_to_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.make_backend(
                Path(tmp),
                wrapper=("proot-distro", "login", "ubuntu", "--"),
            )
            cmd = backend.build_command("gemini-test-high")
            self.assertEqual(cmd[:4], ["proot-distro", "login", "ubuntu", "--"])

    def test_generate_stream_yields_realtime_deltas_and_final_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.make_backend(Path(tmp))
            events = list(backend.generate_stream("FAKE_STREAM", "gemini-test-high"))
            deltas = [e["content"] for e in events if e["type"] == "delta"]
            self.assertEqual(deltas, ["FAKE_", "STREAM_OK"])
            results = [e["response"] for e in events if e["type"] == "result"]
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].response, "FAKE_STREAM_OK")
            self.assertEqual(results[0].usage["total_tokens"], 14)


if __name__ == "__main__":
    unittest.main()
