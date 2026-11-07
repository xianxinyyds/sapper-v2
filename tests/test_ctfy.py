import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import ctfy  # noqa: E402


def instance_task(**overrides):
    task = {
        "schema_version": 3,
        "run_mode": "instance",
        "challenge": {"id": "challenge-1", "name": "Web", "description": "Solve it"},
        "questions": [{"id": "flag", "prompt": "Flag"}],
        "gated_questions": 0,
        "target_instance": "instance-1",
        "target_endpoints": [
            {"label": "web", "type": "http", "url": "http://target:80"}
        ],
        "attachments": [],
    }
    task.update(overrides)
    return task


def challenge_payload(**overrides):
    payload = {
        "id": "challenge-1", "name": "Web", "category": "web", "difficulty": "easy",
        "description": "Solve it", "solves_count": 0, "launch_count": 1, "tags": [],
        "questions": [{"id": "flag", "prompt": "Flag"}], "flag_solves": {},
        "attachments": [], "is_qa_only": False, "defence": None, "internet": "deny",
        "internet_required": False, "internet_reason": "", "expected_start_s": None,
        "shared": False, "launch_ttl_s": 3600, "close_at": None, "closed": False,
        "scoring": "scored", "practice_question_ids": [], "hints_released": 0,
        "hint_released_at": None, "pinned": False, "favourited": False,
        "my_solved_question_ids": [], "my_fully_solved": False,
        "my_team_solved_question_ids": [], "my_team_fully_solved": False,
        "my_team_authored": False, "author_display_name": "", "author_user_id": "",
    }
    payload.update(overrides)
    return payload


def submission_payload(correct):
    return {
        "id": "submission-1", "correct": correct, "solve_time_s": 1.0,
        "solve_rank": 1, "question_id": "q1", "challenge_fully_solved": correct,
        "attempts_remaining": None, "ranked": True, "unranked_reason": "",
    }


class LocalPlatform:
    def __init__(self):
        self.seen = []
        self.responses = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                body = json.loads(raw) if raw else None
                owner.seen.append({
                    "method": self.command, "path": self.path,
                    "authorization": self.headers.get("Authorization"), "body": body,
                })
                status, payload, headers = owner.responses.pop(0)
                data = json.dumps(payload).encode() if isinstance(payload, (dict, list)) else payload
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            do_GET = _handle
            do_POST = _handle

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def queue(self, status, payload, headers=None):
        self.responses.append((status, payload, headers or {}))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class CtfyTaskTests(unittest.TestCase):
    def test_task_requires_v3_and_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            valid_http = instance_task()
            (work / "task.json").write_text(json.dumps(valid_http), encoding="utf-8")
            loaded = ctfy.load_task(work)
            self.assertEqual(loaded["challenge"]["id"], "challenge-1")
            ctfy.require_instance_task(loaded)

            valid_tcp = instance_task(
                target_endpoints=[{"label": "pwn", "type": "tcp", "url": "target:1337"}]
            )
            (work / "task.json").write_text(json.dumps(valid_tcp), encoding="utf-8")
            ctfy.require_instance_task(ctfy.load_task(work))

            invalid = [
                instance_task(schema_version=2),
                instance_task(challenge={"id": "", "name": "Web"}),
                instance_task(target_instance=""),
                instance_task(run_mode="qa"),
            ]
            for task in invalid:
                with self.subTest(task=task):
                    (work / "task.json").write_text(json.dumps(task), encoding="utf-8")
                    with self.assertRaises(ctfy.CtfyError):
                        ctfy.require_instance_task(ctfy.load_task(work))

    def test_write_result_is_atomic_and_versioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "result.json").write_text("old", encoding="utf-8")
            ctfy.write_result(
                work,
                answers={"flag": "FLAG{x}"},
                solved=["flag"],
                fully_solved=True,
                stop_reason="done",
            )
            result = json.loads((work / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result, {
                "schema_version": 3,
                "answers": {"flag": "FLAG{x}"},
                "solved": ["flag"],
                "fully_solved": True,
                "stop_reason": "done",
            })
            self.assertEqual(list(work.glob(".result-*.tmp")), [])


class CtfyModelConfigTests(unittest.TestCase):
    def test_anthropic_config_uses_platform_gateway_without_fallback(self):
        engine = __import__("ctfy_engine")
        env = {
            "LLM_FLAVOR": "anthropic", "LLM_API_KEY": "platform-key",
            "LLM_MODEL": "platform-model", "LLM_BASE_URL": "https://gateway.example",
            "ANTHROPIC_AUTH_TOKEN": "personal-key",
            "ANTHROPIC_BASE_URL": "https://personal.example",
            "SAPPER_KEY_POOL": '[{"token":"personal-key"}]',
        }
        self.assertEqual(engine.configure_model(env), "platform-model")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "platform-key")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "platform-key")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://gateway.example")
        self.assertEqual(env["ANTHROPIC_MODEL"], "platform-model")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "platform-model")
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "platform-model")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "platform-model")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "platform-model")
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "platform-model")
        self.assertEqual(env["SAPPER_BIN"], "claude")
        self.assertNotIn("SAPPER_KEY_POOL", env)

    def test_openai_config_selects_codex_adapter(self):
        engine = __import__("ctfy_engine")
        env = {
            "LLM_FLAVOR": "openai", "LLM_API_KEY": "platform-key",
            "LLM_MODEL": "platform-model", "LLM_BASE_URL": "https://gateway.example",
            "SAPPER_KEY_POOL": "personal",
        }
        self.assertEqual(engine.configure_model(env), "platform-model")
        self.assertEqual(env["OPENAI_API_KEY"], "platform-key")
        self.assertEqual(env["OPENAI_BASE_URL"], "https://gateway.example")
        self.assertEqual(env["OPENAI_MODEL"], "platform-model")
        self.assertEqual(
            env["SAPPER_BIN"], str(Path(engine.__file__).resolve().with_name("codex_engine.py"))
        )
        self.assertNotIn("SAPPER_KEY_POOL", env)

    def test_missing_key_or_unknown_flavor_fails(self):
        engine = __import__("ctfy_engine")
        invalid = [
            {},
            {"LLM_FLAVOR": "anthropic", "LLM_MODEL": "m"},
            {"LLM_FLAVOR": "openai", "LLM_API_KEY": "k"},
            {"LLM_FLAVOR": "gemini", "LLM_API_KEY": "k", "LLM_MODEL": "m"},
        ]
        for env in invalid:
            with self.subTest(env=env), self.assertRaises(ctfy.CtfyError):
                engine.configure_model(env)

    def test_missing_base_url_clears_legacy_gateway(self):
        engine = __import__("ctfy_engine")
        env = {
            "LLM_FLAVOR": "anthropic", "LLM_API_KEY": "platform-key",
            "LLM_MODEL": "platform-model", "ANTHROPIC_BASE_URL": "https://personal.example",
        }
        engine.configure_model(env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)


class CodexEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.log = root / "argv.json"
        self.stdin = root / "stdin.txt"
        fake = root / "codex"
        fake.write_text("""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

Path(os.environ['FAKE_CODEX_LOG']).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')
Path(os.environ['FAKE_CODEX_STDIN']).write_text(sys.stdin.read(), encoding='utf-8')
mode = os.environ.get('FAKE_CODEX_MODE', 'success')
if mode == 'fail':
    sys.stderr.write('simulated codex failure')
    raise SystemExit(7)
args = sys.argv[1:]
out = Path(args[args.index('--output-last-message') + 1])
messages = {
    'empty': '',
    'nonjson': 'I could not solve this',
    'prose': 'The response follows.\\n{\"accepted\":true,\"data\":{}}',
}
out.write_text(messages.get(mode, '{\"accepted\":true,\"data\":{}}'), encoding='utf-8')
""", encoding="utf-8")
        fake.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update({
            "PATH": f"{root}{os.pathsep}{self.env.get('PATH', '')}",
            "FAKE_CODEX_LOG": str(self.log), "FAKE_CODEX_STDIN": str(self.stdin),
            "LLM_API_KEY": "llm-super-secret", "OPENAI_API_KEY": "llm-super-secret",
        })
        self.command = [
            sys.executable, str(ROOT / "lib" / "codex_engine.py"),
            "-p", "--output-format", "json", "--model", "platform-model",
            "--dangerously-skip-permissions", "--append-system-prompt", "SYSTEM RULES",
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def test_codex_adapter_returns_claude_envelope(self):
        proc = subprocess.run(
            self.command, input="USER PROMPT", capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        envelope = json.loads(proc.stdout)
        self.assertEqual(json.loads(envelope["result"]), {"accepted": True, "data": {}})
        argv = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertEqual(argv[0], "exec")
        self.assertIn("--skip-git-repo-check", argv)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("--output-last-message", argv)
        self.assertEqual(argv[argv.index("-c") + 1], "model=platform-model")
        self.assertEqual(argv[-1], "-")
        prompt = self.stdin.read_text(encoding="utf-8")
        self.assertIn("SYSTEM RULES", prompt)
        self.assertIn("USER PROMPT", prompt)
        self.assertNotIn("llm-super-secret", proc.stdout + proc.stderr)

    def test_codex_adapter_rejects_empty_or_failed_result(self):
        for mode in ("empty", "fail"):
            with self.subTest(mode=mode):
                env = self.env.copy()
                env["FAKE_CODEX_MODE"] = mode
                self.log.unlink(missing_ok=True)
                proc = subprocess.run(
                    self.command, input="USER PROMPT", capture_output=True, text=True, env=env,
                )
                self.assertTrue(self.log.exists(), "adapter did not invoke codex")
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")
                self.assertNotIn("llm-super-secret", proc.stderr)
    def test_codex_adapter_rejects_non_json_result(self):
        env = self.env.copy()
        env["FAKE_CODEX_MODE"] = "nonjson"
        proc = subprocess.run(
            self.command, input="USER PROMPT", capture_output=True, text=True, env=env,
        )
        self.assertTrue(self.log.exists(), "adapter did not invoke codex")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn("llm-super-secret", proc.stderr)

    def test_codex_adapter_preserves_prose_wrapped_json_for_downstream(self):
        env = self.env.copy()
        env["FAKE_CODEX_MODE"] = "prose"
        proc = subprocess.run(
            self.command, input="USER PROMPT", capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        envelope = json.loads(proc.stdout)
        sapper = __import__("sapper")
        self.assertEqual(
            sapper.extract_json_object(envelope["result"]),
            {"accepted": True, "data": {}},
        )


class CtfyClientTests(unittest.TestCase):
    def setUp(self):
        self.platform = LocalPlatform()
        self.env = mock.patch.dict("os.environ", {
            "CTFY_PLATFORM_URL": self.platform.base_url,
            "CTFY_TOKEN": "tok-x",
        }, clear=True)
        self.env.start()
        self.client = ctfy.CtfyClient.from_env()

    def tearDown(self):
        self.env.stop()
        self.platform.close()

    def test_submission_contract_and_verdict(self):
        self.platform.queue(200, submission_payload(True))
        self.platform.queue(200, submission_payload(False))
        accepted = self.client.submit_answer("i-1", "q1", "FLAG{x}")
        rejected = self.client.submit_answer("i-1", "q1", "FLAG{y}")
        self.assertTrue(accepted["correct"])
        self.assertFalse(rejected["correct"])
        self.assertEqual([call["method"] for call in self.platform.seen], ["POST", "POST"])
        for call, answer in zip(self.platform.seen, ["FLAG{x}", "FLAG{y}"]):
            self.assertEqual(call["path"], "/api/v1/submissions")
            self.assertEqual(call["authorization"], "Bearer tok-x")
            self.assertEqual(call["body"], {
                "instance_id": "i-1", "question_id": "q1", "answer": answer,
            })

    def test_retry_after_and_error_envelope(self):
        self.platform.queue(429, {"code": "rate_limited", "message": "slow", "detail": {}},
                            {"Retry-After": "2"})
        self.platform.queue(200, challenge_payload())
        with mock.patch("ctfy.time.sleep") as sleep:
            challenge = self.client.get_challenge("challenge-1")
        self.assertEqual(challenge["id"], "challenge-1")
        sleep.assert_called_once_with(2.0)

        self.platform.queue(403, {"code": "token_challenge_forbidden", "message": "forbidden"})
        with self.assertRaises(ctfy.CtfyError) as caught:
            self.client.get_challenge("challenge-1")
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(caught.exception.code, "token_challenge_forbidden")
        self.assertNotIn("tok-x", str(caught.exception))

    def test_submission_is_not_retried(self):
        for status in (429, 502):
            self.platform.queue(status, {"code": "temporary", "message": "try later"})
            with self.subTest(status=status), self.assertRaises(ctfy.CtfyError):
                self.client.submit_answer("i-1", "q1", "FLAG{x}")
        posts = [call for call in self.platform.seen if call["method"] == "POST"]
        self.assertEqual(len(posts), 2)

    def test_downloads_only_null_path_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            task = instance_task(attachments=[
                {"name": "crackme", "size": 4, "path": "/ctfy/work/attachments/crackme"},
                {"name": "dump.bin", "size": 4, "path": None},
            ])
            self.platform.queue(200, b"\x00\xff\x10\x80")
            ctfy.ensure_attachments(task, self.client, work)
            self.assertEqual(
                (work / "attachments" / "dump.bin").read_bytes(), b"\x00\xff\x10\x80"
            )
            self.assertEqual(len(self.platform.seen), 1)
            self.assertEqual(
                self.platform.seen[0]["path"],
                "/api/v1/instances/instance-1/attachments/dump.bin",
            )
            self.assertEqual(self.platform.seen[0]["authorization"], "Bearer tok-x")
            self.assertEqual(list(work.glob("attachments/.attachment-*.tmp")), [])

    def test_attachment_name_cannot_escape_work_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            work.mkdir()
            for name in ("../escape", str(Path(tmp) / "absolute")):
                with self.subTest(name=name), self.assertRaises(ctfy.CtfyError):
                    task = instance_task(attachments=[{"name": name, "path": None}])
                    ctfy.ensure_attachments(task, self.client, work)
            self.assertFalse((Path(tmp) / "escape").exists())
            self.assertEqual(self.platform.seen, [])


if __name__ == "__main__":
    unittest.main()
