import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class OpsScriptTests(unittest.TestCase):
    def test_launcher_is_a_thin_autonomous_launcher(self):
        path = ROOT / "scripts" / "ctfy-qualifier-launcher.sh"
        text = path.read_text(encoding="utf-8")
        subprocess.run(["bash", "-n", str(path)], check=True)
        self.assertIn("SAPPER_BIN", text)
        self.assertIn("OPENAI_BASE_URL", text)
        self.assertIn("/v1", text)
        self.assertIn("--engine", text)
        self.assertIn("flock", text)
        self.assertIn("deadline", text)
        # VPN lifecycle belongs to the worker code, not the launcher.
        self.assertNotIn("wg-quick", text)
        self.assertNotIn("202.112.238.87", text)
        self.assertNotIn("CTFY_ONLY_CHALLENGE", text)
        for token in ("pk_", "pf_", "CTFY_TOKEN='"):
            self.assertNotIn(token, text)

    def test_watchdog_detects_hangs_and_restarts(self):
        wrapper = ROOT / "scripts" / "ctfy-watchdog.sh"
        subprocess.run(["bash", "-n", str(wrapper)], check=True)
        self.assertIn("ctfy_watchdog.py",
                      wrapper.read_text(encoding="utf-8"))
        path = ROOT / "scripts" / "ctfy_watchdog.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        for marker in (
                "heartbeat_state", "lock_is_free", "find_play_pids",
                "lock_holders", "os.kill", "systemctl", "start", "--no-block"):
            self.assertIn(marker, text)

    def test_engine_normalizes_gateway_path_and_finishes_bounded(self):
        path = ROOT / "openai_http_engine.py"
        text = path.read_text(encoding="utf-8")
        self.assertIn('BASE + "/v1"', text)
        self.assertIn("finish_without_tools", text)
        self.assertIn("MAX_ROUNDS = 16", text)
        self.assertIn("CTFY_ENGINE_NO_TOOLS", text)
        compile(text, str(path), "exec")

    def test_scripts_are_executable_or_can_be_invoked_with_bash(self):
        for name in ("ctfy-qualifier-launcher.sh", "ctfy-watchdog.sh"):
            path = ROOT / "scripts" / name
            self.assertTrue(os.access(path, os.R_OK))


if __name__ == "__main__":
    unittest.main()
