"""Contract tests for the one-prompt `ctfy-play` skill and installer wiring."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "ctfy-play" / "SKILL.md"
ROOT_SKILL = ROOT / "SKILL.md"
INSTALL = ROOT / "install.sh"
CTFY_DOC = ROOT / "docker" / "CTFY.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _plain(text: str) -> str:
    """Lower-case text with Markdown emphasis markers removed."""
    return re.sub(r"[*_`]", "", text).lower()


class SkillContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SKILL.is_file():
            raise AssertionError(f"missing skill file: {SKILL}")
        cls.text = _read(SKILL)
        cls.plain = _plain(cls.text)

    def test_has_frontmatter_with_name_and_trigger(self):
        lines = self.text.splitlines()
        self.assertEqual(lines[0], "---")
        end = lines.index("---", 1)
        frontmatter = "\n".join(lines[1:end])
        self.assertRegex(frontmatter, r"(?m)^name:\s*ctfy-play\s*$")
        self.assertRegex(frontmatter, r"(?m)^description:.*打比赛")

    def test_trigger_and_thin_play_launcher(self):
        self.assertIn("打比赛", self.text)
        self.assertIn("python3 lib/ctfy_local.py play", self.text)
        self.assertIn("thin launcher", self.plain)
        self.assertIn("do not re-implement", self.plain)

    def test_operator_must_not_solve_for_the_worker(self):
        self.assertIn("do not read", self.plain)
        self.assertIn("challenge", self.plain)
        self.assertIn("solver", self.plain)
        self.assertIn("do not hand-write", self.plain)
        self.assertIn("payload", self.plain)
        self.assertIn("faithfully", self.plain)
        self.assertIn("accepted", self.plain)

    def test_contains_no_credentials_or_hard_coded_ids(self):
        self.assertNotIn("/root", self.text)
        forbidden = (
            r"\b(?:sk|pk|rk|ghp|gho|ghu|ghs|ghr|xox[abpsr]|AKIA)[-_A-Za-z0-9]{12,}",
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
            r"\b[0-9a-f]{32}\b",
            r"AUTHORED-[A-Z0-9]{6,}",
            r"flag\{[^}\n]{3,}\}",
        )
        for pattern in forbidden:
            self.assertNotRegex(self.text, pattern)

    def test_documented_vps_path_loads_env_only_when_present(self):
        self.assertIn("${CTFY_ENV_FILE:-$HOME/.config/ctfy/env}", self.text)
        self.assertIn('if [[ -f "$ENV_FILE" ]]', self.text)

    def test_maps_openai_session_variables_without_printing_secrets(self):
        self.assertIn('LLM_API_KEY="${LLM_API_KEY:-${OPENAI_API_KEY:-}}"', self.text)
        self.assertIn('LLM_MODEL="${LLM_MODEL:-${OPENAI_MODEL:-deepseek-v4-pro}}"', self.text)
        self.assertIn('LLM_BASE_URL="${LLM_BASE_URL:-${OPENAI_BASE_URL:-}}"', self.text)
        self.assertIn("deepseek-v4-pro", self.text)
        self.assertIn("only the current test default", self.text)
        self.assertNotIn("set -x", self.text)
        for line in self.text.splitlines():
            if "echo" in line or "printf" in line:
                for secret in (
                    "CTFY_TOKEN", "LLM_API_KEY", "OPENAI_API_KEY",
                    "ANTHROPIC_API_KEY", "LLM_BASE_URL", "OPENAI_BASE_URL",
                ):
                    self.assertNotIn(secret, line)

    def test_forwards_existing_engine_and_derives_add_host_at_runtime(self):
        self.assertIn("openai_http_engine.py", self.text)
        self.assertIn("--engine", self.text)
        self.assertIn("CTFY_ENGINE", self.text)
        self.assertIn("getent ahostsv4", self.text)
        self.assertIn("LLM_BASE_URL", self.text)
        self.assertIn("gateway_host", self.text)
        self.assertRegex(
            self.text,
            r'ADD_HOST_ARGS\+?=\(?--add-host\s+"\$\{gateway_host\}',
        )
        # No literal gateway address anywhere in the committed skill.
        self.assertNotRegex(
            self.text,
            r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])",
        )
        self.assertNotIn("docker build", self.text)

    def test_explains_options_cleanup_results_and_resume(self):
        for option in ("--minutes", "--stall-minutes", "--max-workers"):
            self.assertIn(option, self.text)
        self.assertIn("cleanup", self.text)
        for category in (
            "recorded", "accepted", "rejected", "submission_failed", "handed_off",
        ):
            self.assertIn(category, self.text)
        self.assertRegex(self.plain, r"resum")

    def test_explains_single_prompt_operator_flow(self):
        self.assertIn("--competition", self.text)
        self.assertIn("campaign.json", self.text)
        self.assertIn("do not ask", self.plain)


class DocumentationTests(unittest.TestCase):
    def test_root_skill_distinguishes_hosted_and_campaign_modes(self):
        text = _plain(_read(ROOT_SKILL))
        self.assertIn("ctfy-play", text)
        self.assertIn("campaign mode", text)
        self.assertIn("single-challenge", text)

    def test_ctfy_doc_distinguishes_hosted_and_campaign_modes(self):
        text = _plain(_read(CTFY_DOC))
        self.assertIn("ai-commanded", text)
        self.assertIn("campaign mode", text)
        self.assertIn("play", text)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.home = Path(self._temporary.name)
        self.project = self.home / "project"
        (self.project / ".sapper").mkdir(parents=True)
        self.graph = self.project / ".sapper" / "project.json"
        self.graph_text = '{"goal": "keep me"}'
        self.graph.write_text(self.graph_text, encoding="utf-8")

    def tearDown(self):
        self._temporary.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            ["bash", str(INSTALL), *args],
            cwd=self.project,
            env=env,
            text=True,
            capture_output=True,
        )

    def _assert_untouched(self):
        self.assertEqual(self.graph.read_text(encoding="utf-8"), self.graph_text)
        self.assertFalse((self.project / ".claude").exists())
        self.assertFalse((self.home / ".claude" / "settings.json").exists())

    def test_install_links_both_skills_without_touching_project_state(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        skills = self.home / ".claude" / "skills"
        for name, target in (
            ("sapper-setup", ROOT / "skill"),
            ("ctfy-play", ROOT / "ctfy-play"),
        ):
            link = skills / name
            self.assertTrue(link.is_symlink(), f"{name} was not installed")
            self.assertEqual(link.resolve(), target.resolve())
        self.assertTrue((self.home / ".local" / "bin" / "sapper").is_symlink())
        self._assert_untouched()

    def test_uninstall_removes_both_skills_and_leaves_graph_state(self):
        self.assertEqual(self._run().returncode, 0)
        result = self._run("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("sapper-setup", "ctfy-play"):
            self.assertFalse(
                os.path.lexists(self.home / ".claude" / "skills" / name),
                f"{name} was not removed",
            )
        self.assertFalse(
            os.path.lexists(self.home / ".local" / "bin" / "sapper")
        )
        self._assert_untouched()

    def test_installer_is_syntactically_valid(self):
        result = subprocess.run(
            ["bash", "-n", str(INSTALL)], text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
