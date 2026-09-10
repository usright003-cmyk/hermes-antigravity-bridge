import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_antigravity_bridge.backends.antigravity import (
    DEFAULT_ANTIGRAVITY_MODELS,
    AntigravityBackend,
)
from hermes_antigravity_bridge.config import AntigravityConfig
from hermes_antigravity_bridge.errors import (
    BackendError,
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


    def test_auto_sync_credentials_and_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user_cli = tmp_path / "user_home" / ".gemini" / "antigravity-cli"
            user_cli.mkdir(parents=True, exist_ok=True)
            (user_cli / "jetski_state.pbtxt").write_text("token: new_auth_token\n", encoding="utf-8")
            (user_cli / "installation_id").write_text("inst-999\n", encoding="utf-8")

            # 1. Default: sync_user_credentials=False does NOT copy auth tokens
            isolated_home_default = tmp_path / "isolated_home_default"
            backend_default = self.make_backend(
                tmp_path / "runtime1",
                home=isolated_home_default,
                sync_user_credentials=False,
            )
            with patch("pathlib.Path.home", return_value=tmp_path / "user_home"):
                self.assertFalse(backend_default.is_authenticated())
                synced_jetski = isolated_home_default / ".gemini" / "antigravity-cli" / "jetski_state.pbtxt"
                self.assertFalse(synced_jetski.exists())

            # 2. Opt-in: sync_user_credentials=True copies auth tokens securely
            isolated_home = tmp_path / "isolated_home"
            backend = self.make_backend(
                tmp_path / "runtime2",
                home=isolated_home,
                sync_user_credentials=True,
            )
            with patch("pathlib.Path.home", return_value=tmp_path / "user_home"):
                self.assertTrue(backend.is_authenticated())
                synced_jetski = isolated_home / ".gemini" / "antigravity-cli" / "jetski_state.pbtxt"
                synced_settings = isolated_home / ".gemini" / "antigravity-cli" / "settings.json"
                self.assertTrue(synced_jetski.exists())
                self.assertEqual(synced_jetski.read_text(encoding="utf-8"), "token: new_auth_token\n")
                self.assertTrue(synced_settings.exists())

    def test_streaming_retry_guard_prevents_duplicate_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.make_backend(Path(tmp))
            call_count = [0]

            def failing_stream_after_chunk(*args, **kwargs):
                call_count[0] += 1
                yield {"type": "delta", "content": "first_part"}
                raise BackendError("503 Service Unavailable: transient stream failure")

            with patch.object(backend, "_stream_attempt", side_effect=failing_stream_after_chunk):
                gen = backend.generate_stream("test prompt", "gemini-test-high")
                first = next(gen)
                self.assertEqual(first["content"], "first_part")
                with self.assertRaisesRegex(BackendError, "503 Service Unavailable"):
                    next(gen)
                # Ensure it did NOT retry and duplicate tokens
                self.assertEqual(call_count[0], 1)

    def test_streaming_retries_when_zero_chunks_yielded(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.make_backend(Path(tmp), max_attempts=2)
            attempts = [0]

            def fail_then_succeed(*args, **kwargs):
                attempts[0] += 1
                if attempts[0] == 1:
                    raise BackendError("503 Service Unavailable")
                yield {"type": "delta", "content": "recovered"}

            with (
                patch.object(backend, "_stream_attempt", side_effect=fail_then_succeed),
                patch("time.sleep"),
            ):
                events = list(backend.generate_stream("test prompt", "gemini-test-high"))
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["content"], "recovered")
                self.assertEqual(attempts[0], 2)

    def test_model_discovery_failure_sets_fallback_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.make_backend(Path(tmp))
            with patch("subprocess.run", side_effect=OSError("binary exec failed")):
                models = backend.list_models(force_refresh=True)
                self.assertEqual(models, DEFAULT_ANTIGRAVITY_MODELS)
                self.assertEqual(backend.model_source, "fallback")

    def test_readiness_reports_degraded_when_fallback_model_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.make_backend(Path(tmp))
            # Even if authenticated, if model_source is fallback, readiness must report degraded
            backend._model_source = "fallback"
            with (
                patch.object(backend, "is_authenticated", return_value=True),
                patch.object(backend, "list_models", return_value=DEFAULT_ANTIGRAVITY_MODELS),
            ):
                readiness = backend.readiness()
                self.assertEqual(readiness["status"], "degraded")
                self.assertTrue(readiness["authenticated"])
                self.assertEqual(readiness["model_source"], "fallback")
                self.assertIn("reason", readiness)
                self.assertIn("fallback", readiness["reason"])


if __name__ == "__main__":
    unittest.main()
