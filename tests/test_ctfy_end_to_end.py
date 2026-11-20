"""End-to-end local run of play -> commander -> worker -> VPN -> submit.

Uses a fake CTFY HTTP platform, a fake docker binary, a fake wg/wg-quick, and
a fake decision engine. The real commander, WorkerPool, subprocess worker,
run_challenge, VPN manager, handoff and cleanup code all execute.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CHALLENGE = "AUTHORED-TEST01"
COMPETITION = "competition-test"


class FakePlatform:
    def __init__(self):
        self.submissions = []
        self.deleted = []
        self.started = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                return json.loads(raw) if raw else None

            def _send(self, status, payload, content_type="application/json"):
                data = payload if isinstance(payload, bytes) else \
                    json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/api/v1/competitions":
                    self._send(200, {
                        "items": [{
                            "id": COMPETITION,
                            "title": "Local Qualification",
                            "phase": "running",
                            "is_registered": False,
                            "challenge_count": 1,
                            "starts_at": "2026-11-20T02:00:00Z",
                            "ends_at": "2030-01-01T00:00:00Z",
                            "max_instances_per_team_effective": 2,
                        }],
                        "total": 1, "limit": 100, "offset": 0,
                    })
                elif path == f"/api/v1/competitions/{COMPETITION}/challenges":
                    self._send(200, {"items": [owner._challenge()],
                                      "total": 1, "limit": 100, "offset": 0})
                elif path == f"/api/v1/challenges/{CHALLENGE}":
                    self._send(200, owner._challenge())
                elif path == "/api/v1/instances":
                    # Deliberately empty: mirrors the live platform listing bug.
                    self._send(200, {"items": [], "total": 0})
                elif path.endswith("/vpn-config"):
                    self._send(200, b"[Interface]\nPrivateKey = fake\n",
                                "application/octet-stream")
                elif path.endswith("/status"):
                    self._send(200, {"status": "ready"})
                elif path.startswith("/api/v1/instances/"):
                    self._send(200, owner._instance())
                else:
                    self._send(404, {"code": "not_found"})

            def do_POST(self):
                path = self.path.split("?")[0]
                body = self._body() or {}
                if path == "/api/v1/instances":
                    owner.started.append(body)
                    self._send(202, {"id": "instance-1", "status": "starting"})
                elif path == "/api/v1/submissions":
                    owner.submissions.append(body)
                    self._send(200, {
                        "correct": True, "question_id": body.get("question_id"),
                        "challenge_fully_solved": True, "ranked": True,
                    })
                else:
                    self._send(404, {"code": "not_found"})

            def do_DELETE(self):
                path = self.path.split("?")[0]
                owner.deleted.append(path.rsplit("/", 1)[-1])
                self._send(202, {"status": "stopping"})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    @staticmethod
    def _challenge():
        return {
            "id": CHALLENGE, "name": "goal", "category": "web",
            "difficulty": "medium", "description": "solve it", "solves_count": 1,
            "scoring": "scored", "closed": False, "is_qa_only": False,
            "my_team_fully_solved": False, "my_team_solved_question_ids": [],
            "my_team_authored": False,
            "questions": [{"id": "flag", "prompt": "flag?",
                           "unlocked": True, "answered_correctly": False}],
            "attachments": [], "tags": [],
        }

    @staticmethod
    def _instance():
        return {
            "id": "instance-1", "challenge_id": CHALLENGE,
            "competition_id": COMPETITION, "status": "ready", "category": "web",
            "network_topology": "engagement",
            "expires_at": time.time() + 7200,
            "questions": [{"id": "flag", "prompt": "flag?", "unlocked": True,
                           "answered_correctly": False}],
            "services": [{
                "service_type": "http", "host": "target.internal",
                "port": 8080, "path": "/", "label": "web",
            }],
            "vpn_endpoint": {
                "config_url": f"/api/v1/instances/instance-1/vpn-config",
            },
            "entry_urls": [], "attachments": [],
        }

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def write_script(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "runs").mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.wg = self.root / "wg"
        self.wg.mkdir()
        self.engine_count = self.root / "engine-count"
        self.platform = FakePlatform()
        self._write_fakes()

    def tearDown(self):
        self.platform.close()
        self.tmp.cleanup()

    def _write_fakes(self):
        write_script(self.bin / "wg", """#!/usr/bin/env python3
import os, sys
state = os.path.join(os.environ["FAKE_WG_STATE"], "if_" + sys.argv[-1])
sys.exit(0 if os.path.exists(state) else 1)
""")
        write_script(self.bin / "wg-quick", """#!/usr/bin/env python3
import os, sys
state = os.path.join(os.environ["FAKE_WG_STATE"], "if_" + sys.argv[-1])
if sys.argv[1] == "up":
    open(state, "w").close()
else:
    try: os.unlink(state)
    except FileNotFoundError: pass
""")
        write_script(self.bin / "docker", """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if not args or args[0] == "rm":
    sys.exit(0)
src = None
for i, arg in enumerate(args):
    if arg == "--mount" and i + 1 < len(args):
        mount = args[i + 1]
        if "dst=/ctfy/work" not in mount:
            continue
        for part in mount.split(","):
            if part.startswith("src="):
                src = part[4:]
if src:
    os.makedirs(src, exist_ok=True)
    state = os.environ.get("FAKE_WG_STATE", "")
    with open(os.path.join(src, "wg_at_run.txt"), "w") as fh:
        fh.write(",".join(sorted(os.listdir(state))) if state else "")
    with open(os.path.join(src, "result.json"), "w") as fh:
        json.dump({"schema_version": 3,
                   "answers": {"flag": "FLAG{local-integration}"},
                   "solved": ["flag"], "fully_solved": True,
                   "stop_reason": "done"}, fh)
sys.exit(0)
""")
        write_script(self.bin / "engine", f"""#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
path = {str(self.engine_count)!r}
count = int(open(path).read()) if os.path.exists(path) else 0
count += 1
open(path, "w").write(str(count))
if count == 1:
    decision = {{"accepted": True, "data": {{
        "action": "work", "target": {CHALLENGE!r}, "minutes": 5,
        "note": "local integration run"}}}}
else:
    decision = {{"accepted": True, "data": {{"action": "stop",
                "reason": "integration complete"}}}}
print(json.dumps({{"result": json.dumps(decision)}}))
""")

    def run_play(self):
        argv = [
            sys.executable, str(ROOT / "lib" / "ctfy_local.py"),
            "--platform-url", self.platform.base_url,
            "play",
            "--competition", COMPETITION,
            "--minutes", "10",
            "--max-workers", "1",
            "--poll-seconds", "1",
            "--image", "fake-image",
            "--engine", str(self.bin / "engine"),
            "--dispatch-timeout", "10",
            "--startup-timeout", "5",
        ]
        env = {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "CTFY_TOKEN": "test-token",
            "CTFY_PLATFORM_URL": self.platform.base_url,
            "CTFY_RUNS_ROOT": str(self.project / "runs"),
            "CTFY_WG_DIR": str(self.wg),
            "FAKE_WG_STATE": str(self.wg),
            "LLM_MODEL": "deepseek-v4-pro",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return subprocess.run(
                argv, cwd=str(self.project), env=os.environ.copy(),
                capture_output=True, text=True, timeout=120,
            )

    def test_full_autonomous_campaign(self):
        completed = self.run_play()

        self.assertEqual(completed.returncode, 0,
                         msg=completed.stdout + completed.stderr)
        campaign = json.loads(
            (self.project / "runs" / COMPETITION / "campaign.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(set(campaign["challenges"]), {CHALLENGE})
        logs = "\n".join(
            f"--- {p}\n{p.read_text(encoding='utf-8', errors='replace')}"
            for p in sorted((self.project / "runs").rglob("worker.log")))
        self.assertTrue(
            campaign["challenges"][CHALLENGE]["result_summary"]["accepted"] >= 1,
            msg=json.dumps(campaign, indent=2) + "\n" + completed.stdout
            + completed.stderr + "\n" + logs)
        decisions = [
            json.loads(line) for line in
            (self.project / "runs" / COMPETITION / "decisions.jsonl")
            .read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([d["action"] for d in decisions], ["work", "stop"])
        self.assertEqual(self.platform.started[0]["challenge_id"], CHALLENGE)
        self.assertEqual(
            self.platform.submissions[-1]["answer"], "FLAG{local-integration}")
        attempt = next((self.project / "runs").rglob("wg_at_run.txt"))
        self.assertRegex(attempt.read_text(encoding="utf-8"), r"if_ctfy-[0-9a-f]{8}")
        self.assertIn("instance-1", self.platform.deleted)
        self.assertEqual(list(self.wg.glob("if_ctfy-*")), [])
        self.assertGreater((self.project / "runs" / COMPETITION / "heartbeat").stat().st_size, 0)

    def test_launcher_runs_the_campaign_without_manual_setup(self):
        project = self.root / "launcher-project"
        project.mkdir()
        (project / "lib").symlink_to(ROOT / "lib", target_is_directory=True)
        env_file = self.root / "launcher.env"
        env_file.write_text(
            "\n".join([
                "CTFY_TOKEN=test-token",
                "CTFY_INITIAL_TOKEN=test-token",
                f"CTFY_PLATFORM_URL={self.platform.base_url}",
                f"OPENAI_BASE_URL={self.platform.base_url}/v1",
                "OPENAI_API_KEY=dummy",
                "LLM_MODEL=deepseek-v4-pro",
            ]) + "\n",
            encoding="utf-8",
        )
        env = {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "CTFY_PROJECT": str(project),
            "CTFY_ENV_FILE": str(env_file),
            "CTFY_LOG": str(self.root / "launcher.log"),
            "CTFY_LOCK_FILE": str(self.root / "launcher.lock"),
            "CTFY_COMPETITION_ID": COMPETITION,
            "CTFY_MINUTES": "10",
            "CTFY_ENGINE": str(self.bin / "engine"),
            "CTFY_RUNS_ROOT": str(project / "runs"),
            "CTFY_WG_DIR": str(self.wg),
            "FAKE_WG_STATE": str(self.wg),
            "CTFY_POLL_SECONDS": "1",
            "CTFY_MAX_WORKERS": "1",
            "CTFY_IMAGE": "fake-image",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            completed = subprocess.run(
                ["bash", str(ROOT / "scripts" / "ctfy-qualifier-launcher.sh")],
                cwd=str(project), env=os.environ.copy(),
                capture_output=True, text=True, timeout=120,
            )
        self.assertEqual(completed.returncode, 0,
                         msg=completed.stdout + completed.stderr)
        self.assertEqual(
            self.platform.submissions[-1]["answer"], "FLAG{local-integration}")
        self.assertTrue((project / "runs" / COMPETITION / "decisions.jsonl").is_file())

    def test_adopted_instance_is_reused_and_never_closed(self):
        runs = self.project / "runs" / COMPETITION
        runs.mkdir(parents=True)
        (runs / "campaign.json").write_text(json.dumps({
            "schema_version": 1, "competition_id": COMPETITION,
            "competition_title": "Local Qualification",
            "created_at": time.time(), "deadline": time.time() + 600,
            "stall_minutes": 30, "max_workers": 1,
            "challenges": {}, "workers": {}, "instances": {},
        }), encoding="utf-8")
        argv = [
            sys.executable, str(ROOT / "lib" / "ctfy_local.py"),
            "--platform-url", self.platform.base_url,
            "play", "--competition", COMPETITION,
            "--minutes", "10", "--max-workers", "1", "--poll-seconds", "1",
            "--image", "fake-image", "--engine", str(self.bin / "engine"),
            "--dispatch-timeout", "10", "--startup-timeout", "5",
            "--adopt", f"{CHALLENGE}=instance-1",
        ]
        env = {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "CTFY_TOKEN": "test-token",
            "CTFY_PLATFORM_URL": self.platform.base_url,
            "CTFY_RUNS_ROOT": str(self.project / "runs"),
            "CTFY_WG_DIR": str(self.wg),
            "FAKE_WG_STATE": str(self.wg),
            "LLM_MODEL": "deepseek-v4-pro",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            completed = subprocess.run(
                argv, cwd=str(self.project), env=os.environ.copy(),
                capture_output=True, text=True, timeout=120,
            )
        self.assertEqual(completed.returncode, 0,
                         msg=completed.stdout + completed.stderr)
        self.assertEqual(self.platform.started, [])
        self.assertEqual(self.platform.deleted, [])
        self.assertEqual(
            self.platform.submissions[-1]["answer"], "FLAG{local-integration}")
        state = json.loads((runs / "campaign.json").read_text(encoding="utf-8"))
        self.assertFalse(state["instances"]["instance-1"]["created_by_campaign"])

    def test_resume_keeps_the_campaign_and_never_duplicates_instances(self):
        first = self.run_play()
        self.assertEqual(first.returncode, 0, msg=first.stdout + first.stderr)
        manifest_path = self.project / "runs" / COMPETITION / "campaign.json"
        deadline = json.loads(manifest_path.read_text(encoding="utf-8"))["deadline"]

        second = self.run_play()
        self.assertEqual(second.returncode, 0, msg=second.stdout + second.stderr)
        resumed = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(resumed["deadline"], deadline)
        self.assertEqual(len(self.platform.started), 1)


if __name__ == "__main__":
    unittest.main()
