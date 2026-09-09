import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LifecycleScriptTests(unittest.TestCase):
    def run_script(self, name, env, *args):
        completed = subprocess.run(
            ["bash", str(ROOT / "scripts" / name), *args],
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

    @unittest.skipUnless(shutil.which("bash"), "bash is required for lifecycle script tests")
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


if __name__ == "__main__":
    unittest.main()
