import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def find_bash():
    candidates = [
        shutil.which("bash"),
        "C:/Program Files/Git/bin/bash.exe",
        "C:/Program Files/Git/usr/bin/bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    return None


BASH = find_bash()


class LifecycleScriptTests(unittest.TestCase):
    def run_script(self, name, env, *args):
        completed = subprocess.run(
            [BASH or "bash", str(ROOT / "scripts" / name), *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0:
            self.fail(
                f"{name} failed ({completed.returncode})\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
        return completed

    @unittest.skipUnless(
        sys.platform.startswith("linux") and shutil.which("bash"),
        "Linux systemd and bash are required for lifecycle script tests",
    )
    def test_install_update_rollback_and_uninstall_preserve_external_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            install_root = home / ".local/share/hermes-antigravity-bridge"
            config_root = home / ".config/hermes-antigravity-bridge"
            systemd_root = home / ".config/systemd/user"
            agy_sentinel = home / ".gemini/antigravity-cli/conversations/sentinel.db"
            memory_sentinel = home / ".hermes/memories/MEMORY.md"
            agy_sentinel.parent.mkdir(parents=True)
            memory_sentinel.parent.mkdir(parents=True)
            agy_sentinel.write_bytes(b"DO-NOT-DELETE-AGY")
            memory_sentinel.write_bytes(b"DO-NOT-DELETE-HERMES")

            env = dict(os.environ)
            env.update({
                "HOME": str(home),
                "HAB_INSTALL_ROOT": str(install_root),
                "HAB_CONFIG_ROOT": str(config_root),
                "HAB_SYSTEMD_USER_DIR": str(systemd_root),
                "HAB_PYTHON_BIN": sys.executable,
                "HAB_SKIP_SERVICE": "1",
                "HAB_SKIP_AGY_CHECK": "1",
                "HAB_PIP_NO_DEPS": "1",
            })
            uv_candidate = os.environ.get("HAB_UV_BIN") or shutil.which("uv")
            if not uv_candidate:
                for cand in (
                    Path("/home/azureuser/.hermes/bin/uv"),
                    Path.home() / ".cargo/bin/uv",
                    Path.home() / ".local/bin/uv",
                    Path.home() / ".hermes/bin/uv",
                ):
                    if cand.is_file() and os.access(cand, os.X_OK):
                        uv_candidate = str(cand)
                        break
            if uv_candidate:
                env["HAB_UV_BIN"] = str(uv_candidate)

            agy_home = home / ".local/state/hermes-antigravity-bridge/agy-home"
            self.run_script("install.sh", env, "--prepare")
            first = (install_root / "current").resolve()
            self.assertTrue((first / "venv/bin/hermes-antigravity-bridge").exists())
            token = config_root / "bridge.token"
            self.assertGreaterEqual(len(token.read_text(encoding="utf-8").strip()), 32)
            self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
            settings = agy_home / ".gemini/antigravity-cli/settings.json"
            self.assertTrue(settings.exists())
            self.assertIn('"toolPermission": "strict"', settings.read_text(encoding="utf-8"))
            unit = (systemd_root / "hermes-antigravity-bridge.service").read_text(encoding="utf-8")
            self.assertIn(str(install_root / "current/venv/bin/hermes-antigravity-bridge"), unit)
            self.assertIn(str(agy_home), unit)
            self.assertNotIn(token.read_text(encoding="utf-8").strip(), unit)

            # Rollback must fail cleanly when only initial release exists (no previous link)
            failed_rollback = subprocess.run(
                ["bash", str(ROOT / "scripts/rollback.sh")],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(failed_rollback.returncode, 0)
            self.assertIn("cannot rollback: no previous release found", failed_rollback.stderr)

            self.run_script("update.sh", env)
            second = (install_root / "current").resolve()
            self.assertNotEqual(first, second)
            self.assertEqual((install_root / "previous").resolve(), first)

            self.run_script("rollback.sh", env)
            self.assertEqual((install_root / "current").resolve(), first)
            self.assertEqual((install_root / "previous").resolve(), second)

            self.run_script("uninstall.sh", env)
            self.assertFalse((systemd_root / "hermes-antigravity-bridge.service").exists())
            self.assertFalse((install_root / "current").exists())
            self.assertTrue(config_root.exists())
            self.assertTrue((install_root / "releases").exists())
            self.assertTrue(settings.exists())
            self.assertEqual(agy_sentinel.read_bytes(), b"DO-NOT-DELETE-AGY")
            self.assertEqual(memory_sentinel.read_bytes(), b"DO-NOT-DELETE-HERMES")


class HealthCheckAndLifecycleLogicTests(unittest.TestCase):
    @unittest.skipUnless(BASH, "bash is required for script tests")
    def test_hab_service_health_authenticated_ready_and_degraded(self):
        import http.server
        import threading
        from typing import ClassVar

        class MockBridge(http.server.BaseHTTPRequestHandler):
            mode: ClassVar[str] = "ready"
            received_auth: ClassVar[list[str | None]] = []

            def do_GET(self):
                if self.path == "/health":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')
                    return
                if self.path == "/ready":
                    auth = self.headers.get("Authorization")
                    MockBridge.received_auth.append(auth)
                    if auth != "Bearer secret-token-42":
                        self.send_response(401)
                        self.end_headers()
                        self.wfile.write(b'{"detail":"Unauthorized"}')
                        return
                    if MockBridge.mode == "ready":
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'{"status":"ready","authenticated":true}')
                    else:
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'{"status":"degraded","authenticated":false}')
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), MockBridge)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                token_file = tmp_path / "bridge.token"
                token_file.write_text("secret-token-42\n")
                config_file = tmp_path / "config.toml"
                config_file.write_text(f'[server]\nhost = "127.0.0.1"\nport = {port}\n')

                lib_path = (ROOT / "scripts/lib.sh").as_posix()

                # Test 1: ready mode succeeds and sends Bearer token via stdin config
                MockBridge.mode = "ready"
                MockBridge.received_auth.clear()
                cmd = f"""
                source "{lib_path}"
                export HAB_TOKEN_FILE="{token_file.as_posix()}"
                export HAB_CONFIG_FILE="{config_file.as_posix()}"
                export HAB_CURRENT_LINK="{tmp_path.as_posix()}"
                hab_service_health
                """
                res = subprocess.run([BASH, "-c", cmd], capture_output=True, text=True, check=False)
                self.assertEqual(res.returncode, 0, f"Expected success: {res.stderr}")
                self.assertIn("Bearer secret-token-42", MockBridge.received_auth)

                # Test 2: degraded status returns error code 1
                MockBridge.mode = "degraded"
                res = subprocess.run([BASH, "-c", cmd], capture_output=True, text=True, check=False)
                self.assertEqual(res.returncode, 1)
                self.assertIn("service status not ready", res.stderr)

                # Test 3: wrong token returns error code 1
                token_file.write_text("wrong-token\n")
                res = subprocess.run([BASH, "-c", cmd], capture_output=True, text=True, check=False)
                self.assertNotEqual(res.returncode, 0)
        finally:
            server.shutdown()
            server.server_close()

    @unittest.skipUnless(BASH, "bash is required for script tests")
    def test_rollback_prerequisites_missing_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            install_root = tmp_path / "hermes-antigravity-bridge"
            install_root.mkdir()
            current = install_root / "current"
            current.mkdir()

            env = dict(os.environ)
            env.update({
                "HOME": str(tmp_path),
                "HAB_INSTALL_ROOT": str(install_root),
                "HAB_SKIP_SERVICE": "1",
            })
            res = subprocess.run(
                [BASH, str(ROOT / "scripts/rollback.sh")],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn("cannot rollback: no previous release found", res.stderr)

    @unittest.skipUnless(BASH, "bash is required for script tests")
    def test_rollback_prerequisites_missing_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            install_root = tmp_path / "hermes-antigravity-bridge"
            install_root.mkdir()

            env = dict(os.environ)
            env.update({
                "HOME": str(tmp_path),
                "HAB_INSTALL_ROOT": str(install_root),
                "HAB_SKIP_SERVICE": "1",
            })
            res = subprocess.run(
                [BASH, str(ROOT / "scripts/rollback.sh")],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn("no active release found", res.stderr)


if __name__ == "__main__":
    unittest.main()
