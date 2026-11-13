import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import ctfy  # noqa: E402
import ctfy_local  # noqa: E402


def challenge(**overrides):
    value = {
        "id": "challenge-1", "name": "Offline", "description": "Solve it",
        "category": "crypto", "is_qa_only": True, "close_at": None,
        "questions": [{"id": "flag", "prompt": "Find the flag"}],
        "attachments": [],
    }
    value.update(overrides)
    return value


def instance(**overrides):
    value = {
        "id": "instance-1", "challenge_id": "challenge-1",
        "competition_id": "competition-1", "status": "ready",
        "category": "web", "expires_at": 2_000_000_000,
        "questions": [{"id": "flag", "prompt": "Find the flag", "unlocked": True}],
        "services": [{
            "service_type": "https", "host": "target.example", "port": 443,
            "path": "/", "label": "app",
        }],
        "attachments": [], "network_topology": "simple", "shared": False,
    }
    value.update(overrides)
    return value


class FakeClient:
    def __init__(self, challenge_value=None, instance_value=None):
        self.challenge = challenge_value or challenge()
        self.instance = instance_value
        self.events = []
        self.start_error = None

    def get_challenge(self, challenge_id, competition_id=""):
        self.events.append(("challenge", challenge_id, competition_id))
        return self.challenge

    def list_instances(self, challenge_id=""):
        self.events.append(("list", challenge_id))
        if self.instance and self.instance.get("status") in {"ready", "running"}:
            return [self.instance]
        return []

    def get_instance(self, instance_id):
        return self.instance

    def start_instance(self, challenge_id, competition_id, ttl=None, **kwargs):
        self.events.append(("start", challenge_id, competition_id, ttl))
        callback = kwargs.get("on_created")
        if callback:
            callback("instance-1")
        if self.start_error:
            raise self.start_error
        return self.instance

    def stop_instance(self, instance_id):
        self.events.append(("stop", instance_id))

    def submit_answer(self, instance_id, question_id, answer):
        self.events.append(("submit", instance_id, question_id, answer))
        return {"correct": True, "question_id": question_id}

    def submit_qa_answer(self, challenge_id, competition_id, question_id, answer):
        self.events.append(("qa-submit", challenge_id, competition_id, question_id, answer))
        return {"correct": True, "question_id": question_id}

    def download_challenge_attachment(self, challenge_id, name, destination):
        self.events.append(("download", challenge_id, name))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"offline input")


class CtfyLocalTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_instance_task_preserves_access_credentials_and_expiry(self):
        target = instance(services=[{
            "service_type": "tcp", "host": "target.example", "port": 1337,
            "access": "tls_sni", "label": "pwn",
            "credentials": {"username": "user", "password": "pass"},
        }])
        task = ctfy_local.build_task(
            challenge(is_qa_only=False), target, "competition-1",
            "https://platform.example", 5,
        )
        self.assertEqual(task["run_mode"], "instance")
        self.assertEqual(task["target_instance"], "instance-1")
        self.assertEqual(task["target_endpoints"][0]["url"], "target.example:1337")
        self.assertEqual(task["target_endpoints"][0]["access"], "tls_sni")
        self.assertEqual(
            task["target_endpoints"][0]["credentials"],
            {"username": "user", "password": "pass"},
        )
        self.assertTrue(task["challenge"]["close_at"].startswith("2033-"))
        self.assertEqual(task["max_turns"], 5)
        ctfy.require_task(task)

    def test_qa_prepare_downloads_nested_attachment_once_without_instance(self):
        item = challenge(attachments=[{"name": "tools/linux/input.bin", "size": 13}])
        task = ctfy_local.build_task(item, None, "competition-1", "http://platform", 3)
        client = FakeClient(item)
        ctfy_local.prepare_work_dir(client, self.work, task)
        ctfy_local.prepare_work_dir(client, self.work, task)
        self.assertEqual((self.work / "attachments/tools/linux/input.bin").read_bytes(),
                         b"offline input")
        self.assertEqual(len([event for event in client.events if event[0] == "download"]), 1)
        self.assertNotIn("start", [event[0] for event in client.events])
        self.assertEqual(task["target_endpoints"], [])
        self.assertEqual(task["attachments"][0]["path"],
                         "/ctfy/work/attachments/tools/linux/input.bin")

    def test_work_dir_cannot_switch_competition_or_instance(self):
        first = ctfy_local.build_task(
            challenge(is_qa_only=False), instance(), "competition-1", "http://platform", 3,
        )
        ctfy_local.prepare_work_dir(FakeClient(), self.work, first)
        other_instance = ctfy_local.build_task(
            challenge(is_qa_only=False), instance(id="instance-2"),
            "competition-1", "http://platform", 3,
        )
        with self.assertRaisesRegex(ctfy.CtfyError, "another"):
            ctfy_local.prepare_work_dir(FakeClient(), self.work, other_instance)
        saved = json.loads((self.work / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["target_instance"], "instance-1")
        other_competition = ctfy_local.build_task(
            challenge(is_qa_only=False), instance(), "competition-2", "http://platform", 3,
        )
        with self.assertRaisesRegex(ctfy.CtfyError, "another"):
            ctfy_local.prepare_work_dir(FakeClient(), self.work, other_competition)

    def test_engagement_requires_explicit_network_support(self):
        with self.assertRaisesRegex(ctfy.CtfyError, "networking"):
            ctfy_local.build_task(
                challenge(is_qa_only=False), instance(network_topology="engagement"),
                "competition-1", "http://platform", 3,
            )

    def test_handoff_uses_the_matching_grader_once_per_candidate(self):
        for mode in ("instance", "qa"):
            with self.subTest(mode=mode):
                target = None if mode == "qa" else instance()
                task = ctfy_local.build_task(
                    challenge(is_qa_only=mode == "qa"), target,
                    "competition-1", "http://platform", 3,
                )
                client = FakeClient()
                outcomes = ctfy_local.submit_handed_off_answers(
                    client, task, {"answers": {"flag": "candidate"}}, self.work,
                )
                self.assertEqual(outcomes[0]["correct"], True)
                kinds = [event[0] for event in client.events]
                self.assertEqual(kinds, ["qa-submit" if mode == "qa" else "submit"])
                self.assertTrue((self.work / "handoff.json").is_file())


class CtfyLocalRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, *extra):
        return ctfy_local.build_parser().parse_args([
            "run", "challenge-1", "--competition", "competition-1",
            "--work-dir", str(self.work), "--minutes", "3", *extra,
        ])

    def test_run_starts_executes_hands_off_and_stops_without_secret_arguments(self):
        client = FakeClient(challenge(is_qa_only=False), instance(status="starting"))

        def container(command, env=None, timeout=None, **kwargs):
            self.assertNotIn("secret-token", command)
            self.assertEqual(env["CTFY_TOKEN"], "secret-token")
            self.assertEqual(timeout, 210)
            (self.work / "result.json").write_text(json.dumps({
                "schema_version": 3, "answers": {"flag": "candidate"},
                "solved": [], "fully_solved": False, "stop_reason": "incomplete",
            }), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        with mock.patch.dict("os.environ", {"CTFY_TOKEN": "secret-token"}, clear=True), \
                mock.patch.object(ctfy_local.ctfy, "CtfyClient", return_value=client), \
                mock.patch("ctfy_local.subprocess.run", side_effect=container), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = ctfy_local.run_local(self.args())
        self.assertEqual(rc, 0)
        kinds = [event[0] for event in client.events]
        self.assertEqual(kinds, ["challenge", "list", "start", "submit", "stop"])
        for path in self.work.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"secret-token", path.read_bytes())

    def test_stale_result_is_preserved_but_not_submitted_again(self):
        target = instance(status="starting")
        client = FakeClient(challenge(is_qa_only=False), target)
        task = ctfy_local.build_task(
            client.challenge, target, "competition-1", "http://platform", 3,
        )
        ctfy_local.prepare_work_dir(client, self.work, task)
        (self.work / "result.json").write_text(json.dumps({
            "schema_version": 3, "answers": {"flag": "old-candidate"},
        }), encoding="utf-8")
        with mock.patch.dict("os.environ", {"CTFY_TOKEN": "secret-token"}, clear=True), \
                mock.patch.object(ctfy_local.ctfy, "CtfyClient", return_value=client), \
                mock.patch(
                    "ctfy_local.subprocess.run",
                    return_value=subprocess.CompletedProcess(["docker"], 1),
                ):
            with self.assertRaisesRegex(ctfy.CtfyError, "valid result"):
                ctfy_local.run_local(self.args())
        self.assertFalse((self.work / "result.json").exists())
        previous = json.loads((self.work / "result.previous.json").read_text(encoding="utf-8"))
        self.assertEqual(previous["answers"], {"flag": "old-candidate"})
        self.assertNotIn("submit", [event[0] for event in client.events])
        self.assertIn(("stop", "instance-1"), client.events)

    def test_startup_failure_cleans_up_the_created_instance(self):
        client = FakeClient(challenge(is_qa_only=False), instance(status="starting"))
        client.start_error = ctfy.CtfyError("startup timed out", transport=True)
        with mock.patch.dict("os.environ", {"CTFY_TOKEN": "secret-token"}, clear=True), \
                mock.patch.object(ctfy_local.ctfy, "CtfyClient", return_value=client), \
                mock.patch("ctfy_local.subprocess.run") as container:
            with self.assertRaises(ctfy.CtfyError):
                ctfy_local.run_local(self.args())
        container.assert_not_called()
        self.assertIn(("stop", "instance-1"), client.events)

    def test_container_timeout_removes_only_its_container_and_stops_instance(self):
        client = FakeClient(challenge(is_qa_only=False), instance(status="starting"))
        calls = []

        def container(command, env=None, timeout=None, **kwargs):
            calls.append(command)
            if command[1] == "run":
                raise subprocess.TimeoutExpired(command, timeout)
            return subprocess.CompletedProcess(command, 0)

        with mock.patch.dict("os.environ", {"CTFY_TOKEN": "secret-token"}, clear=True), \
                mock.patch.object(ctfy_local.ctfy, "CtfyClient", return_value=client), \
                mock.patch("ctfy_local.subprocess.run", side_effect=container):
            with self.assertRaises(subprocess.TimeoutExpired):
                ctfy_local.run_local(self.args())
        self.assertEqual(calls[1][1:3], ["rm", "-f"])
        self.assertIn("sapper-local-challenge-1-", calls[1][3])
        self.assertIn(("stop", "instance-1"), client.events)

    def test_cleanup_failure_is_not_silent_or_allowed_to_replace_the_primary_error(self):
        client = FakeClient(challenge(is_qa_only=False), instance())
        client.stop_instance = mock.Mock(side_effect=ctfy.CtfyError("stop failed"))
        with mock.patch.dict("os.environ", {"CTFY_TOKEN": "secret-token"}, clear=True), \
                mock.patch.object(ctfy_local.ctfy, "CtfyClient", return_value=client), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ctfy.CtfyError, "stop failed"):
                ctfy_local.run_local(self.args("--prepare-only"))

        client = FakeClient(challenge(is_qa_only=False), instance(status="starting"))
        client.start_error = ctfy.CtfyError("startup timed out", transport=True)
        client.stop_instance = mock.Mock(side_effect=ctfy.CtfyError("stop failed"))
        with mock.patch.dict("os.environ", {"CTFY_TOKEN": "secret-token"}, clear=True), \
                mock.patch.object(ctfy_local.ctfy, "CtfyClient", return_value=client), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaisesRegex(ctfy.CtfyError, "startup timed out"):
                ctfy_local.run_local(self.args())
        self.assertIn("stop failed", stderr.getvalue())

    def test_prepare_only_does_not_stop_shared_or_kept_instance(self):
        for shared, keep in ((True, False), (False, True)):
            with self.subTest(shared=shared, keep=keep):
                client = FakeClient(
                    challenge(is_qa_only=False), instance(shared=shared),
                )
                extra = ["--prepare-only"] + (["--keep-instance"] if keep else [])
                with mock.patch.dict("os.environ", {"CTFY_TOKEN": "secret-token"}, clear=True), \
                        mock.patch.object(ctfy_local.ctfy, "CtfyClient", return_value=client), \
                        mock.patch("ctfy_local.subprocess.run") as container, \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(ctfy_local.run_local(self.args(*extra)), 0)
                container.assert_not_called()
                self.assertNotIn("start", [event[0] for event in client.events])
                self.assertNotIn("stop", [event[0] for event in client.events])


if __name__ == "__main__":
    unittest.main()
