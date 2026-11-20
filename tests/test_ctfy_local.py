import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import ctfy  # noqa: E402
import ctfy_commander  # noqa: E402
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

    def get_instance(self, instance_id, **kwargs):
        self.events.append(("get", instance_id))
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

    def test_engagement_builds_a_task_and_delegates_vpn_to_the_worker(self):
        target = instance(network_topology="engagement")
        task = ctfy_local.build_task(
            challenge(is_qa_only=False), target,
            "competition-1", "http://platform", 3,
        )
        self.assertEqual(task["run_mode"], "instance")
        self.assertEqual(task["target_endpoints"][0]["url"],
                         "https://target.example:443")
        args = ctfy_local.build_parser().parse_args([
            "run", "challenge-1", "--competition", "competition-1",
            "--work-dir", str(self.work), "--prepare-only", "--keep-instance",
        ])
        with mock.patch("ctfy_vpn.ensure_vpn", return_value="ctfy-instance1") as ensure:
            outcome = ctfy_local.run_challenge(
                args, client=FakeClient(challenge(is_qa_only=False), target),
            )
        ensure.assert_called_once()
        self.assertEqual(outcome["vpn_interface"], "ctfy-instance1")

    def test_handoff_uses_the_matching_grader_once_per_candidate(self):
        for mode in ("instance", "qa"):
            with self.subTest(mode=mode):
                target = None if mode == "qa" else instance()
                task = ctfy_local.build_task(
                    challenge(is_qa_only=mode == "qa"), target,
                    "competition-1", "http://platform", 3,
                )
                client = FakeClient()
                mode_work = self.work / mode
                outcomes = ctfy_local.submit_handed_off_answers(
                    client, task, {"answers": {"flag": "candidate"}}, mode_work,
                )
                self.assertEqual(outcomes[0]["correct"], True)
                kinds = [event[0] for event in client.events]
                self.assertEqual(kinds, ["qa-submit" if mode == "qa" else "submit"])
                self.assertTrue((mode_work / "handoff.json").is_file())


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
        client = FakeClient(challenge(is_qa_only=False), instance(status="starting"))
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

    def test_fallback_selects_exact_listed_challenge_for_unsupported_catalog(self):
        client = mock.Mock()
        client.get_challenge.side_effect = ctfy.CtfyError(
            "catalog unsupported", code="go_unsupported_feature",
        )
        client.list_challenges.return_value = [
            challenge(id="challenge-2"), challenge(id="challenge-1", name="Listed"),
        ]

        selected = ctfy_local.get_challenge_for_run(
            client, "challenge-1", "competition-1",
        )

        self.assertEqual(selected["name"], "Listed")
        client.list_challenges.assert_called_once_with("competition-1")

    def test_fallback_does_not_hide_other_challenge_errors(self):
        client = mock.Mock()
        client.get_challenge.side_effect = ctfy.CtfyError(
            "missing", code="challenge_not_found",
        )

        with self.assertRaisesRegex(ctfy.CtfyError, "missing"):
            ctfy_local.get_challenge_for_run(client, "challenge-1", "competition-1")

        client.list_challenges.assert_not_called()

    def test_structured_prepare_only_reports_ownership_and_keeps_instances(self):
        for created, keep in ((True, True), (False, True), (False, False)):
            with self.subTest(created=created, keep=keep):
                client = FakeClient(
                    challenge(is_qa_only=False),
                    instance(status="starting" if created else "ready"),
                )
                events = []
                extra = ["--prepare-only"] + (["--keep-instance"] if keep else [])

                outcome = ctfy_local.run_challenge(
                    self.args(*extra),
                    client=client, on_event=lambda name, data: events.append((name, data)),
                )

                self.assertEqual(outcome, {
                    "work_dir": str(self.work), "instance_id": "instance-1",
                    "created_instance": created, "prepare_only": True,
                    "container_exit_code": None, "result": None, "handoff": [],
                    "vpn_interface": None,
                })
                self.assertEqual(events, [("prepared", {
                    "work_dir": str(self.work), "instance_id": "instance-1",
                    "created_instance": created, "vpn_interface": None,
                })])
                self.assertNotIn("stop", [event[0] for event in client.events])

    def test_structured_run_challenge_returns_result_handoff_and_events(self):
        client = FakeClient(challenge(is_qa_only=False), instance(status="starting"))
        events = []
        container_names = []

        def container(command, env=None, timeout=None, **kwargs):
            container_names.append(command[command.index("--name") + 1])
            (self.work / "result.json").write_text(json.dumps({
                "schema_version": 3, "answers": {"flag": "candidate"},
                "solved": [], "fully_solved": False, "stop_reason": "incomplete",
            }), encoding="utf-8")
            return subprocess.CompletedProcess(command, 7)

        with mock.patch("ctfy_local.subprocess.run", side_effect=container):
            outcome = ctfy_local.run_challenge(
                self.args("--keep-instance"), client=client, challenge=client.challenge,
                on_event=lambda name, data: events.append((name, data)),
            )

        self.assertEqual(set(outcome), {
            "work_dir", "instance_id", "created_instance", "prepare_only",
            "container_exit_code", "result", "handoff", "vpn_interface",
        })
        self.assertEqual(outcome["work_dir"], str(self.work))
        self.assertEqual(outcome["instance_id"], "instance-1")
        self.assertTrue(outcome["created_instance"])
        self.assertFalse(outcome["prepare_only"])
        self.assertEqual(outcome["container_exit_code"], 7)
        self.assertEqual(outcome["result"]["answers"], {"flag": "candidate"})
        self.assertEqual(outcome["handoff"][0]["correct"], True)
        self.assertEqual([name for name, _ in events], ["prepared", "container"])
        self.assertEqual(events[0][1], {
            "work_dir": str(self.work), "instance_id": "instance-1",
            "created_instance": True, "vpn_interface": None,
        })
        self.assertEqual(events[1][1], {"name": container_names[0]})
        self.assertNotIn("stop", [event[0] for event in client.events])

    def test_known_instance_id_is_reused_when_listing_is_unavailable(self):
        client = FakeClient(challenge(is_qa_only=False), instance())
        client.list_instances = mock.Mock(return_value=[])
        args = self.args("--prepare-only", "--keep-instance",
                         "--instance-id", "instance-1")
        outcome = ctfy_local.run_challenge(args, client=client)
        self.assertEqual(outcome["instance_id"], "instance-1")
        self.assertFalse(outcome["created_instance"])
        client.list_instances.assert_not_called()
        self.assertNotIn("start", [event[0] for event in client.events])

    def test_reused_instance_is_preserved_on_worker_error_without_keep(self):
        client = FakeClient(challenge(is_qa_only=False), instance())

        with mock.patch(
                "ctfy_local.subprocess.run",
                return_value=subprocess.CompletedProcess(["docker"], 1),
        ):
            with self.assertRaisesRegex(ctfy.CtfyError, "valid result"):
                ctfy_local.run_challenge(self.args(), client=client)

        self.assertFalse(self.args().keep_instance)
        self.assertNotIn("stop", [event[0] for event in client.events])

    def test_structured_run_local_prints_outcome_and_returns_container_code(self):
        outcome = {
            "work_dir": str(self.work), "instance_id": "instance-1",
            "created_instance": False, "prepare_only": False,
            "container_exit_code": 9, "result": {"schema_version": 3}, "handoff": [],
        }
        client = FakeClient()
        with mock.patch.object(ctfy_local.ctfy, "CtfyClient", return_value=client), \
                mock.patch.object(ctfy_local, "run_challenge", return_value=outcome), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            rc = ctfy_local.run_local(self.args())

        self.assertEqual(rc, 9)
        self.assertEqual(json.loads(stdout.getvalue()), outcome)

    def test_structured_path_bound_ids_are_validated_before_platform_calls(self):
        for challenge_id, competition_id in (
            ("../challenge", "competition-1"), ("challenge-1", "../competition"),
        ):
            with self.subTest(challenge_id=challenge_id, competition_id=competition_id):
                args = ctfy_local.build_parser().parse_args([
                    "run", challenge_id, "--competition", competition_id,
                    "--work-dir", str(self.work),
                ])
                client = mock.Mock()
                with self.assertRaisesRegex(ctfy.CtfyError, "safe nonempty identifier"):
                    ctfy_local.run_challenge(args, client=client)
                client.get_challenge.assert_not_called()

    def test_engine_is_copied_executable_and_forwarded_only_to_container(self):
        engine = Path(self.tmp.name) / "engine"
        engine.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        engine.chmod(0o755)
        captured = {}

        def container(command, env=None, timeout=None, **kwargs):
            captured["command"] = command
            captured["env"] = env
            (self.work / "result.json").write_text(json.dumps({
                "schema_version": 3, "answers": {}, "solved": [],
                "fully_solved": False, "stop_reason": "incomplete",
            }), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        with mock.patch.dict("os.environ", {
                "CTFY_TOKEN": "secret-token", "SAPPER_BIN": "host-engine",
        }, clear=True), mock.patch("ctfy_local.subprocess.run", side_effect=container):
            outcome = ctfy_local.run_challenge(
                self.args("--engine", str(engine)), client=FakeClient(),
            )

        copied = self.work / ".ctfy-engine"
        self.assertEqual(outcome["container_exit_code"], 0)
        self.assertEqual(copied.read_bytes(), engine.read_bytes())
        self.assertTrue(copied.stat().st_mode & 0o111)
        self.assertEqual(captured["env"]["SAPPER_BIN"], "/ctfy/work/.ctfy-engine")
        self.assertEqual(captured["env"]["CTFY_TOKEN"], "secret-token")
        self.assertNotIn(str(engine), captured["command"])
        self.assertNotIn("secret-token", captured["command"])
        self.assertIn("SAPPER_BIN", ctfy_local._FORWARDED_ENV)

    def test_engine_replaces_existing_symlink_without_touching_target(self):
        engine = Path(self.tmp.name) / "engine"
        engine.write_bytes(b"new engine")
        engine.chmod(0o755)
        sentinel = Path(self.tmp.name) / "external-sentinel"
        sentinel.write_bytes(b"do not overwrite")
        (self.work / ".ctfy-engine").symlink_to(sentinel)

        def container(command, env=None, timeout=None, **kwargs):
            (self.work / "result.json").write_text(json.dumps({
                "schema_version": 3, "answers": {}, "solved": [],
                "fully_solved": False, "stop_reason": "incomplete",
            }), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        with mock.patch("ctfy_local.subprocess.run", side_effect=container):
            ctfy_local.run_challenge(
                self.args("--engine", str(engine)), client=FakeClient(),
            )

        self.assertEqual(sentinel.read_bytes(), b"do not overwrite")
        self.assertFalse((self.work / ".ctfy-engine").is_symlink())
        self.assertEqual((self.work / ".ctfy-engine").read_bytes(), b"new engine")

    def test_add_host_and_read_only_source_lib_mount_reach_docker(self):
        args = self.args(
            "--add-host", "gateway.internal:192.0.2.10",
            "--add-host", "model.internal:192.0.2.20",
        )

        command = ctfy_local._docker_command(args, self.work)

        self.assertIn(
            f"type=bind,src={(ROOT / 'lib').resolve()},dst=/opt/sapper/lib,readonly",
            command,
        )
        for mapping in (
            "gateway.internal:192.0.2.10", "model.internal:192.0.2.20",
        ):
            index = command.index(mapping)
            self.assertEqual(command[index - 1], "--add-host")

    def test_handoff_replay_is_idempotent_and_transport_uncertainty_is_not_retried(self):
        task = ctfy_local.build_task(
            challenge(), None, "competition-1", "http://platform", 3,
        )
        client = FakeClient()
        calls = []

        def submit(challenge_id, competition_id, question_id, answer):
            calls.append((challenge_id, competition_id, question_id, answer))
            if answer == "uncertain":
                raise ctfy.CtfyError("submission timed out", transport=True)
            return {"correct": answer == "new-answer", "question_id": question_id}

        client.submit_qa_answer = submit

        first = ctfy_local.submit_handed_off_answers(
            client, task, {"answers": {"flag": "candidate"}}, self.work,
        )
        self.assertFalse(first[0]["correct"])
        ctfy_local.submit_handed_off_answers(
            client, task, {"answers": {"flag": "candidate"}}, self.work,
        )
        self.assertEqual(len(calls), 1)

        uncertain = ctfy_local.submit_handed_off_answers(
            client, task, {"answers": {"flag": "uncertain"}}, self.work,
        )
        unresolved = next(item for item in uncertain if item["answer"] == "uncertain")
        self.assertIsNone(unresolved["correct"])
        self.assertEqual(unresolved["status"], "unresolved")
        self.assertTrue(unresolved["transport_uncertain"])
        ctfy_local.submit_handed_off_answers(
            client, task, {"answers": {"flag": "uncertain"}}, self.work,
        )
        self.assertEqual(len(calls), 2)

        merged = ctfy_local.submit_handed_off_answers(
            client, task, {"answers": {"flag": "new-answer"}}, self.work,
        )
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1][-1], "new-answer")
        self.assertEqual(
            {(item["question_id"], item["answer"]) for item in merged},
            {("flag", "candidate"), ("flag", "uncertain"), ("flag", "new-answer")},
        )
        saved = json.loads((self.work / "handoff.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["submissions"], merged)


NOW = 1_800_000_000.0


class FakeDiscoveryClient:
    def __init__(self, competitions=()):
        self.competitions = [dict(item) for item in competitions]
        self.discovery_calls = 0
        self.phases = []

    def list_competitions(self, phase=None):
        self.discovery_calls += 1
        self.phases.append(phase)
        return [dict(item) for item in self.competitions]


class FakeStopClient:
    def __init__(self):
        self.stopped = []
        self.started = False

    def stop_instance(self, instance_id):
        self.stopped.append(instance_id)

    def start_instance(self, *args, **kwargs):
        self.started = True
        raise AssertionError("cleanup must never start an instance")


class PlayParserTests(unittest.TestCase):
    def test_play_needs_no_challenge_id_and_defaults_limits(self):
        args = ctfy_local.build_parser().parse_args(["play"])
        self.assertEqual(args.command, "play")
        self.assertFalse(hasattr(args, "challenge_id"))
        self.assertEqual(args.competition, "")
        self.assertIsNone(args.minutes)
        self.assertEqual(args.stall_minutes, 30)
        self.assertEqual(args.max_workers, 2)
        self.assertEqual(args.poll_seconds, 30)

    def test_play_accepts_worker_and_budget_options(self):
        args = ctfy_local.build_parser().parse_args([
            "play", "--competition", "comp-1", "--minutes", "120",
            "--stall-minutes", "10", "--max-workers", "4", "--poll-seconds", "5",
            "--image", "custom:image", "--engine", "/tmp/engine",
            "--add-host", "gw.internal:192.0.2.1", "--max-turns", "5",
            "--dispatch-timeout", "9", "--startup-timeout", "7", "--ttl", "60",
        ])
        self.assertEqual(args.competition, "comp-1")
        self.assertEqual(args.minutes, 120)
        self.assertEqual(args.stall_minutes, 10)
        self.assertEqual(args.max_workers, 4)
        self.assertEqual(args.poll_seconds, 5)
        self.assertEqual(args.image, "custom:image")
        self.assertEqual(args.engine, Path("/tmp/engine"))
        self.assertEqual(args.add_host, ["gw.internal:192.0.2.1"])
        self.assertEqual(args.max_turns, 5)
        self.assertEqual(args.dispatch_timeout, 9)
        self.assertEqual(args.startup_timeout, 7)
        self.assertEqual(args.ttl, 60)

    def test_cleanup_parser_requires_competition(self):
        args = ctfy_local.build_parser().parse_args(
            ["cleanup", "--competition", "comp-1"])
        self.assertEqual(args.competition, "comp-1")
        with self.assertRaises(SystemExit):
            ctfy_local.build_parser().parse_args(["cleanup"])


class ResolveCampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name) / "runs"

    def competition(self, competition_id="competition-1", **overrides):
        value = {
            "id": competition_id, "title": competition_id, "phase": "running",
            "is_registered": True, "challenge_count": 3,
            "ends_at": "2030-01-01T00:00:00Z",
        }
        value.update(overrides)
        return value

    def write_campaign(self, competition_id, deadline, **extra):
        root = self.runs / competition_id
        root.mkdir(parents=True, exist_ok=True)
        state = {
            "schema_version": 1, "competition_id": competition_id,
            "competition_title": competition_id, "created_at": NOW,
            "deadline": deadline, "stall_minutes": 30, "max_workers": 2,
            "challenges": {}, "workers": {}, "instances": {},
        }
        state.update(extra)
        (root / "campaign.json").write_text(json.dumps(state), encoding="utf-8")
        return root

    def parse(self, *extra):
        return ctfy_local.build_parser().parse_args(["play", *extra])

    def test_resolve_campaign_reopens_single_existing_manifest(self):
        original_deadline = NOW + 12345.0
        self.write_campaign("competition-a", original_deadline)
        client = FakeDiscoveryClient([
            self.competition("competition-a"),
            self.competition("competition-b"),
        ])
        store, manifest, competition = ctfy_local._resolve_campaign(
            client, self.parse(), self.runs, now=NOW)
        self.assertEqual(manifest["competition_id"], "competition-a")
        self.assertEqual(manifest["deadline"], original_deadline)
        self.assertEqual(competition["id"], "competition-a")
        self.assertEqual(store.root, self.runs / "competition-a")

    def test_resolve_campaign_multiple_manifests_require_competition(self):
        self.write_campaign("competition-a", NOW + 3600)
        self.write_campaign("competition-b", NOW + 3600)
        client = FakeDiscoveryClient([])
        with self.assertRaisesRegex(ctfy.CtfyError, "--competition"):
            ctfy_local._resolve_campaign(client, self.parse(), self.runs, now=NOW)

    def test_resolve_campaign_explicit_id_picks_manifest_over_discovery(self):
        self.write_campaign("competition-a", NOW + 1111)
        self.write_campaign("competition-b", NOW + 2222)
        client = FakeDiscoveryClient([])
        store, manifest, competition = ctfy_local._resolve_campaign(
            client, self.parse("--competition", "competition-b"), self.runs, now=NOW)
        self.assertEqual(manifest["competition_id"], "competition-b")
        self.assertEqual(manifest["deadline"], NOW + 2222)
        self.assertNotIn("running", client.phases)

    def test_resolve_campaign_fresh_discovery_creates_manifest(self):
        client = FakeDiscoveryClient([self.competition("competition-1")])
        store, manifest, competition = ctfy_local._resolve_campaign(
            client, self.parse("--minutes", "120"), self.runs, now=NOW)
        self.assertEqual(manifest["competition_id"], "competition-1")
        self.assertEqual(manifest["deadline"], NOW + 120 * 60)
        self.assertEqual(manifest["max_workers"], 2)
        self.assertEqual(manifest["stall_minutes"], 30)
        self.assertTrue((self.runs / "competition-1" / "campaign.json").is_file())

    def test_resolve_campaign_ignores_expired_manifest_and_creates_fresh(self):
        self.write_campaign("competition-1", NOW - 3600)
        client = FakeDiscoveryClient([self.competition("competition-1")])
        store, manifest, competition = ctfy_local._resolve_campaign(
            client, self.parse("--minutes", "120"), self.runs, now=NOW)
        self.assertEqual(manifest["competition_id"], "competition-1")
        self.assertEqual(manifest["deadline"], NOW + 120 * 60)
        self.assertGreater(manifest["deadline"], NOW)
        self.assertEqual(competition["id"], "competition-1")
        self.assertEqual(store.root, self.runs / "competition-1")

    def test_resolve_campaign_explicit_expired_manifest_creates_fresh(self):
        self.write_campaign("competition-1", NOW - 1)
        client = FakeDiscoveryClient([self.competition("competition-1")])
        store, manifest, competition = ctfy_local._resolve_campaign(
            client, self.parse("--competition", "competition-1", "--minutes", "120"),
            self.runs, now=NOW)
        self.assertEqual(manifest["competition_id"], "competition-1")
        self.assertEqual(manifest["deadline"], NOW + 120 * 60)
        self.assertEqual(competition["id"], "competition-1")


class PlayCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name) / "runs"

    def write_campaign(self, competition_id, deadline):
        root = self.runs / competition_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "campaign.json").write_text(json.dumps({
            "schema_version": 1, "competition_id": competition_id,
            "competition_title": competition_id, "created_at": NOW,
            "deadline": deadline, "stall_minutes": 30, "max_workers": 2,
            "challenges": {}, "workers": {}, "instances": {},
        }), encoding="utf-8")
        return root

    def test_play_constructs_commander_installs_and_restores_signals(self):
        self.write_campaign("competition-a", NOW + 3600)
        fake_client = FakeDiscoveryClient([])
        fake_commander = mock.Mock()
        fake_commander.run.return_value = 7
        fake_signals = mock.Mock()
        args = ctfy_local.build_parser().parse_args(["play"])
        with mock.patch.object(ctfy_local, "_runs_root", return_value=self.runs), \
                mock.patch.object(ctfy_local.ctfy, "CtfyClient", return_value=fake_client), \
                mock.patch("ctfy_commander.Commander",
                           return_value=fake_commander) as commander_cls, \
                mock.patch.object(ctfy_local, "_PlaySignals",
                                  return_value=fake_signals) as signals_cls:
            rc = ctfy_local.play(args)
        self.assertEqual(rc, 7)
        commander_cls.assert_called_once()
        signals_cls.assert_called_once_with(fake_commander)
        fake_signals.install.assert_called_once()
        fake_signals.restore.assert_called_once()
        fake_commander.run.assert_called_once()

    def test_play_routes_commander_decisions_through_the_engine(self):
        self.write_campaign("competition-a", NOW + 3600)
        fake_client = FakeDiscoveryClient([])
        fake_commander = mock.Mock()
        fake_commander.run.return_value = 0
        args = ctfy_local.build_parser().parse_args([
            "play", "--engine", str(ROOT / "openai_http_engine.py"),
        ])
        with mock.patch.dict("os.environ", {}, clear=False), \
                mock.patch.object(ctfy_local, "_runs_root", return_value=self.runs), \
                mock.patch.object(ctfy_local.ctfy, "CtfyClient", return_value=fake_client), \
                mock.patch("ctfy_commander.Commander", return_value=fake_commander), \
                mock.patch.object(ctfy_local, "_PlaySignals", return_value=mock.Mock()):
            self.assertEqual(ctfy_local.play(args), 0)
            self.assertEqual(
                os.environ["SAPPER_BIN"], str((ROOT / "openai_http_engine.py").resolve()))
            self.assertEqual(os.environ["CTFY_ENGINE_NO_TOOLS"], "1")

    def test_play_can_adopt_an_existing_instance_for_reuse(self):
        root = self.write_campaign("competition-a", NOW + 3600)
        fake_client = FakeDiscoveryClient([])
        fake_commander = mock.Mock()
        fake_commander.run.return_value = 0
        args = ctfy_local.build_parser().parse_args([
            "play", "--adopt", "challenge-1=instance-1",
        ])
        with mock.patch.object(ctfy_local, "_runs_root", return_value=self.runs), \
                mock.patch.object(ctfy_local.ctfy, "CtfyClient", return_value=fake_client), \
                mock.patch("ctfy_commander.Commander", return_value=fake_commander), \
                mock.patch.object(ctfy_local, "_PlaySignals", return_value=mock.Mock()):
            self.assertEqual(ctfy_local.play(args), 0)
        state = json.loads((root / "campaign.json").read_text(encoding="utf-8"))
        self.assertEqual(state["instances"]["instance-1"]["challenge_id"], "challenge-1")
        self.assertFalse(state["instances"]["instance-1"]["created_by_campaign"])

    def test_play_expired_manifest_smoke_test_creates_fresh_campaign(self):
        self.write_campaign("competition-1", NOW - 3600)
        fake_client = FakeDiscoveryClient([{
            "id": "competition-1", "title": "competition-1", "phase": "running",
            "is_registered": True, "challenge_count": 3,
            "ends_at": "2030-01-01T00:00:00Z",
        }])
        captured = {}
        fake_commander = mock.Mock()
        fake_commander.run.return_value = 0

        def make_commander(store, client, competition, args, **kwargs):
            captured["store"] = store
            captured["competition"] = competition
            return fake_commander

        args = ctfy_local.build_parser().parse_args(["play", "--minutes", "120"])
        with mock.patch.object(ctfy_local, "_runs_root", return_value=self.runs), \
                mock.patch.object(ctfy_local.ctfy, "CtfyClient", return_value=fake_client), \
                mock.patch.object(ctfy_local.time, "time", return_value=NOW), \
                mock.patch("ctfy_commander.Commander", side_effect=make_commander), \
                mock.patch.object(ctfy_local, "_PlaySignals", return_value=mock.Mock()):
            rc = ctfy_local.play(args)
        self.assertEqual(rc, 0)
        self.assertEqual(captured["store"].load()["deadline"], NOW + 120 * 60)
        self.assertGreater(captured["store"].load()["deadline"], NOW)
        self.assertEqual(captured["competition"]["id"], "competition-1")
        fake_commander.run.assert_called_once()


class CleanupCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name) / "runs"

    def write_campaign(self, competition_id, instances):
        root = self.runs / competition_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "campaign.json").write_text(json.dumps({
            "schema_version": 1, "competition_id": competition_id,
            "competition_title": competition_id, "created_at": NOW,
            "deadline": NOW + 3600, "stall_minutes": 30, "max_workers": 2,
            "challenges": {}, "workers": {}, "instances": instances,
        }), encoding="utf-8")
        return root

    def test_cleanup_command_stops_only_owned_leftover_instances(self):
        self.write_campaign("competition-1", {
            "owned-1": {"challenge_id": "c1", "created_by_campaign": True},
            "owned-closed": {"challenge_id": "c2", "created_by_campaign": True,
                             "closed": True, "status": "stopped"},
            "reused-1": {"challenge_id": "c3", "created_by_campaign": False},
        })
        fake_client = FakeStopClient()
        args = ctfy_local.build_parser().parse_args(
            ["cleanup", "--competition", "competition-1"])
        with mock.patch.object(ctfy_local, "_runs_root", return_value=self.runs), \
                mock.patch.object(ctfy_local.ctfy, "CtfyClient", return_value=fake_client), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = ctfy_local.cleanup(args)
        self.assertEqual(rc, 0)
        self.assertEqual(fake_client.stopped, ["owned-1"])
        self.assertFalse(fake_client.started)
        saved = json.loads(
            (self.runs / "competition-1" / "campaign.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["instances"]["owned-1"]["closed"])

    def test_cleanup_command_missing_manifest_fails(self):
        fake_client = FakeStopClient()
        args = ctfy_local.build_parser().parse_args(
            ["cleanup", "--competition", "competition-1"])
        with mock.patch.object(ctfy_local, "_runs_root", return_value=self.runs), \
                mock.patch.object(ctfy_local.ctfy, "CtfyClient", return_value=fake_client):
            with self.assertRaises(ctfy.CtfyError):
                ctfy_local.cleanup(args)
        self.assertEqual(fake_client.stopped, [])


class PlaySignalTests(unittest.TestCase):
    def test_signal_handler_invokes_commander_interrupt(self):
        commander = mock.Mock()
        signals = ctfy_local._PlaySignals(commander)
        signals._handle(signal.SIGINT, None)
        signals._handle(signal.SIGTERM, None)
        self.assertEqual(commander.interrupt.call_count, 2)


if __name__ == "__main__":
    unittest.main()
