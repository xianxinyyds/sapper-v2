import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import ctfy_run  # noqa: E402
from test_ctfy import (  # noqa: E402
    LocalPlatform, challenge_payload, instance_task, submission_payload,
)


class CtfyDryRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_precedes_config_validation(self):
        (self.work / "task.json").write_text(json.dumps({
            "schema_version": 3, "dry_run": True,
        }), encoding="utf-8")
        with mock.patch.dict("os.environ", {"CTFY_WORK_DIR": str(self.work)}, clear=True), \
                mock.patch.object(ctfy_run, "configure_model") as configure, \
                mock.patch.object(ctfy_run.sapper, "main") as auto:
            rc = ctfy_run.run(self.work)
        self.assertEqual(rc, 0)
        configure.assert_not_called()
        auto.assert_not_called()
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result, {
            "schema_version": 3, "answers": {}, "solved": [],
            "fully_solved": False, "stop_reason": "dry_run",
        })

    def test_environment_dry_run_precedes_task_validation(self):
        (self.work / "task.json").write_text(json.dumps({
            "schema_version": 3,
        }), encoding="utf-8")
        with mock.patch.dict("os.environ", {
                "CTFY_WORK_DIR": str(self.work), "CTFY_EVAL_DRY_RUN": "1",
        }, clear=True), \
                mock.patch.object(ctfy_run, "configure_model") as configure, \
                mock.patch.object(ctfy_run.ctfy.CtfyClient, "from_env") as client, \
                mock.patch.object(ctfy_run.sapper, "main") as auto:
            rc = ctfy_run.main()
        self.assertEqual(rc, 0)
        configure.assert_not_called()
        client.assert_not_called()
        auto.assert_not_called()
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["stop_reason"], "dry_run")

    def test_invalid_task_and_unsupported_mode_fail_cleanly(self):
        cases = [
            ({"schema_version": 2, "run_mode": "instance"}, "config_error"),
            ({"schema_version": 3, "run_mode": "unknown", "questions": []}, "unsupported_run_mode"),
        ]
        for task, reason in cases:
            with self.subTest(reason=reason):
                (self.work / "task.json").write_text(json.dumps(task), encoding="utf-8")
                with mock.patch.object(ctfy_run, "configure_model") as configure, \
                        mock.patch.object(ctfy_run.sapper, "main") as auto, \
                        contextlib.redirect_stderr(io.StringIO()):
                    rc = ctfy_run.run(self.work)
                self.assertNotEqual(rc, 0)
                configure.assert_not_called()
                auto.assert_not_called()
                result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(result["schema_version"], 3)
                self.assertEqual(result["stop_reason"], reason)


class CtfyLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.store = ctfy_run.sapper.Store(self.work / ".sapper")
        self.store.init("ctfy:challenge-1", "solve challenge-1")

    def tearDown(self):
        self.tmp.cleanup()

    def add_answer(self, question, value, status="recorded"):
        return self.store.add_fact(
            description=f"answer for {question}", creator="test", kind="flag",
            data={
                "platform": "ctfy", "challenge_id": "challenge-1",
                "instance_id": "instance-1", "question_id": question,
                "value": value, "status": status,
            },
        )

    def test_collect_ledger_excludes_rejected_and_accepted(self):
        self.add_answer("flag", "old-candidate")
        self.add_answer("flag", "wrong", "rejected")
        self.add_answer("q2", "accepted-answer", "accepted")
        self.add_answer("q3", "already-exported", "handed_off")
        self.store.add_fact(
            description="legacy arena flag", creator="test", kind="flag",
            data={"value": "legacy", "question_id": "legacy", "status": "recorded"},
        )
        latest = self.add_answer("flag", "latest-candidate")
        project = self.store.load_project()
        project["ctfy"] = {
            "challenge_id": "challenge-1", "solved_question_ids": ["q3"],
            "fully_solved": True,
        }
        self.store.save_project(project)

        answers, solved, fully_solved = ctfy_run.collect_ledger(self.store)

        self.assertEqual(answers, {"flag": "latest-candidate"})
        self.assertEqual(solved, ["q2", "q3"])
        self.assertTrue(fully_solved)
        self.assertEqual(self.store.get_fact(latest["id"])["data"]["status"], "recorded")

    def test_checkpoint_is_repeatable_and_does_not_hand_off(self):
        candidate = self.add_answer("flag", "candidate")
        ctfy_run._checkpoint_result(self.work, self.store)
        ctfy_run._checkpoint_result(self.work, self.store)
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["answers"], {"flag": "candidate"})
        self.assertEqual(result["stop_reason"], "incomplete")
        self.assertEqual(self.store.get_fact(candidate["id"])["data"]["status"], "recorded")

    def test_finalize_marks_handed_off_and_does_not_roll_up_twice(self):
        candidate = self.add_answer("flag", "candidate")
        self.add_answer("flag", "wrong", "rejected")

        ctfy_run.finalize_result(self.work, self.store, "done")
        first = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(first["answers"], {"flag": "candidate"})
        self.assertEqual(first["stop_reason"], "done")
        self.assertEqual(self.store.get_fact(candidate["id"])["data"]["status"], "handed_off")

        ctfy_run.finalize_result(self.work, self.store, "done")
        second = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(second["answers"], {})


class CtfyRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.task = instance_task(max_turns=5)
        (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")
        self.client = mock.Mock()
        self.client.get_challenge.return_value = challenge_payload()

    def tearDown(self):
        self.tmp.cleanup()

    def run_with_mocks(self, auto_return=0, environ=None, attachment_error=None,
                       auto_effect=None):
        with mock.patch.object(
                ctfy_run.ctfy.CtfyClient, "from_env", return_value=self.client), \
                mock.patch.object(ctfy_run, "configure_model", return_value="platform-model") as configure, \
                mock.patch.object(
                    ctfy_run.ctfy, "ensure_attachments", side_effect=attachment_error
                ) as attachments, \
                mock.patch.object(
                    ctfy_run.sapper, "main",
                    side_effect=auto_effect, return_value=auto_return,
                ) as auto, \
                mock.patch.dict("os.environ", environ or {}, clear=True):
            rc = ctfy_run.run(self.work)
        return rc, configure, attachments, auto

    def test_run_initializes_single_graph_and_invokes_auto(self):
        rc, configure, attachments, auto = self.run_with_mocks()

        self.assertEqual(rc, 0)
        self.client.get_challenge.assert_called_once_with("challenge-1")
        configure.assert_called_once_with()
        attachments.assert_called_once_with(self.task, self.client, self.work)
        auto.assert_called_once()
        argv = auto.call_args.args[0]
        self.assertEqual(argv[:12], [
            "--store", str(self.work / ".sapper"), "auto",
            "--model", "platform-model",
            "--templates", str(ROOT / "templates" / "ctfy"),
            "--skip-permissions", "--max-steps", "5", "--max-explore", "5",
        ])
        self.assertIn("--deadline", argv)
        self.assertNotIn("--minutes", argv)
        self.assertEqual(argv[argv.index("--timeout") + 1], "300")
        self.assertTrue(callable(auto.call_args.kwargs["checkpoint"]))
        store = ctfy_run.sapper.Store(self.work / ".sapper")
        self.assertEqual(store.load_project()["origin"], "ctfy:challenge-1")
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["answers"], {})
        self.assertEqual(result["stop_reason"], "incomplete")

    def test_default_steps_and_auto_failure_still_write_result(self):
        self.task["max_turns"] = None
        (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")

        rc, _, _, auto = self.run_with_mocks(auto_return=1)

        self.assertEqual(rc, 1)
        argv = auto.call_args.args[0]
        self.assertEqual(argv[argv.index("--max-steps") + 1], "8")
        self.assertEqual(argv[argv.index("--max-explore") + 1], "8")
        self.assertNotIn("--verify", argv)
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["stop_reason"], "auto_failed")

    def test_attachment_transport_failure_is_blocked_by_infra(self):
        error = ctfy_run.ctfy.CtfyError("attachment network unavailable")
        error.transport = True
        rc, _, _, auto = self.run_with_mocks(attachment_error=error)
        self.assertEqual(rc, 0)
        auto.assert_not_called()
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["stop_reason"], "blocked_by_infra")

    def test_platform_budget_safety_cap_and_round_minutes(self):
        cases = [
            (9, {"CTFY_EVAL_MAX_TURNS": "12", "SAPPER_MAX_STEPS": "5"}, 5),
            (None, {"CTFY_EVAL_MAX_TURNS": "12"}, 12),
            (None, {"CTFY_EVAL_MAX_TURNS": "12", "SAPPER_MAX_STEPS": "5"}, 5),
            (None, {}, 8),
        ]
        for configured, environ, expected in cases:
            with self.subTest(configured=configured, environ=environ), \
                    mock.patch.dict("os.environ", environ, clear=True):
                self.assertEqual(
                    ctfy_run._max_steps(instance_task(max_turns=configured)), expected,
                )

        self.task["max_turns"] = None
        (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")
        rc, _, _, auto = self.run_with_mocks(environ={
            "CTFY_EVAL_MAX_TURNS": "12", "SAPPER_MAX_EXPLORE": "3",
            "SAPPER_ROUND_MINUTES": "7",
        })
        self.assertEqual(rc, 0)
        argv = auto.call_args.args[0]
        self.assertEqual(argv[argv.index("--max-steps") + 1], "12")
        self.assertEqual(argv[argv.index("--max-explore") + 1], "3")
        self.assertIn("--deadline", argv)
        self.assertNotIn("--minutes", argv)

    def test_round_deadline_default_override_close_at_and_fallback(self):
        now = 1_800_000_000.0
        with mock.patch.object(ctfy_run.time, "time", return_value=now), \
                mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(ctfy_run._round_deadline({}), now + 165 * 60)
            self.assertEqual(
                ctfy_run._round_deadline({"close_at": "not-a-time"}), now + 165 * 60
            )
        with mock.patch.object(ctfy_run.time, "time", return_value=now), \
                mock.patch.dict("os.environ", {"SAPPER_ROUND_MINUTES": "30"}, clear=True):
            self.assertEqual(ctfy_run._round_deadline({}), now + 30 * 60)
            earlier = datetime.fromtimestamp(now + 60, timezone.utc).isoformat()
            self.assertEqual(ctfy_run._round_deadline({"close_at": earlier}), now + 60)
            past = datetime.fromtimestamp(now - 60, timezone.utc).isoformat()
            self.assertEqual(ctfy_run._round_deadline({"close_at": past}), now - 60)

    def test_dispatch_timeout_default_and_precedence(self):
        cases = [
            ({}, "300"),
            ({"SAPPER_TIMEOUT": "450"}, "450"),
            ({"SAPPER_TIMEOUT": "450", "CTFY_DISPATCH_TIMEOUT": "240"}, "240"),
        ]
        for environ, expected in cases:
            with self.subTest(environ=environ):
                rc, _, _, auto = self.run_with_mocks(environ=environ)
                self.assertEqual(rc, 0)
                argv = auto.call_args.args[0]
                self.assertEqual(argv[argv.index("--timeout") + 1], expected)

    def test_result_checkpoint_exists_before_auto_and_preserves_ledger(self):
        store = ctfy_run.prepare_graph(self.work, self.task, challenge_payload())
        candidate = store.add_fact(
            "candidate", "test", kind="flag",
            data={
                "platform": "ctfy", "challenge_id": "challenge-1",
                "instance_id": "instance-1", "question_id": "flag",
                "value": "preserved", "status": "recorded",
            },
        )

        def inspect_checkpoint(argv, **kwargs):
            result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["answers"], {"flag": "preserved"})
            self.assertEqual(result["stop_reason"], "incomplete")
            self.assertEqual(store.get_fact(candidate["id"])["data"]["status"], "recorded")
            return 0

        rc, _, _, auto = self.run_with_mocks(auto_effect=inspect_checkpoint)
        self.assertEqual(rc, 0)
        auto.assert_called_once()

    def test_invalid_budget_environment_fails_with_config_error(self):
        self.task["max_turns"] = None
        (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")
        for variable in (
            "CTFY_EVAL_MAX_TURNS", "SAPPER_MAX_STEPS",
            "SAPPER_MAX_EXPLORE", "SAPPER_ROUND_MINUTES", "CTFY_DISPATCH_TIMEOUT",
        ):
            with self.subTest(variable=variable), contextlib.redirect_stderr(io.StringIO()):
                rc, _, _, auto = self.run_with_mocks(environ={variable: "0"})
                self.assertNotEqual(rc, 0)
                auto.assert_not_called()
                result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(result["stop_reason"], "config_error")

    def test_structured_auto_outcome_controls_stop_reason(self):
        store = ctfy_run.prepare_graph(self.work, self.task, challenge_payload())
        project = store.load_project()
        project["last_auto"] = {
            "reason": "no_progress", "explore_attempts": 0, "detail": "noop",
        }
        store.save_project(project)
        rc, _, _, _ = self.run_with_mocks()
        self.assertEqual(rc, 0)
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["stop_reason"], "no_progress")

        project = store.load_project()
        project["last_auto"] = {
            "reason": "explore_failed", "explore_attempts": 1, "detail": "timeout",
        }
        store.save_project(project)
        rc, _, _, _ = self.run_with_mocks(auto_return=1)
        self.assertEqual(rc, 1)
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["stop_reason"], "explore_failed")

    def test_new_round_clears_stale_blocker_before_sync(self):
        store = ctfy_run.prepare_graph(self.work, self.task, challenge_payload())
        project = store.load_project()
        project["ctfy"]["blocker"] = {
            "kind": "instance_unavailable", "created_at": "old", "detail": "stale",
        }
        store.save_project(project)

        ctfy_run.prepare_graph(self.work, self.task, challenge_payload())

        self.assertIsNone(store.load_project()["ctfy"].get("blocker"))

    def test_graph_from_other_challenge_is_rejected(self):
        store = ctfy_run.sapper.Store(self.work / ".sapper")
        store.init("ctfy:another-challenge", "solve another challenge")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc, configure, _, auto = self.run_with_mocks()

        self.assertNotEqual(rc, 0)
        self.assertIn("different CTFY challenge", stderr.getvalue())
        auto.assert_not_called()
        self.assertEqual(store.load_project()["origin"], "ctfy:another-challenge")
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["stop_reason"], "config_error")

    def test_malformed_task_containers_fail_cleanly_with_result(self):
        cases = [
            ("target_endpoints", None),
            ("attachments", None),
            ("scope", 1),
            ("rules_of_engagement", 1),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                self.task = instance_task(max_turns=5, **{field: value})
                (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")
                (self.work / "result.json").unlink(missing_ok=True)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    rc, configure, _, auto = self.run_with_mocks()

                self.assertNotEqual(rc, 0)
                self.assertIn(field, stderr.getvalue())
                configure.assert_not_called()
                auto.assert_not_called()
                result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(result["stop_reason"], "config_error")

    def test_completed_graph_reopens_when_new_question_unlocked(self):
        store = ctfy_run.prepare_graph(
            self.work, self.task,
            challenge_payload(my_solved_question_ids=["flag"], my_fully_solved=True),
        )
        evidence = store.add_fact("evidence", "test")
        original_edge = store.complete_project([evidence["id"]], "done", "test")
        self.client.get_challenge.return_value = challenge_payload(
            questions=[
                {"id": "flag", "prompt": "Flag"},
                {"id": "q2", "prompt": "Newly unlocked"},
            ],
            my_solved_question_ids=["flag"], my_fully_solved=False,
        )

        def complete_again(argv, **kwargs):
            self.assertEqual(store.load_project()["status"], "active")
            question_ids = {
                (fact.get("data") or {}).get("question_id")
                for fact in store.list_facts()
                if (fact.get("data") or {}).get("record_type") == "question"
            }
            self.assertIn("q2", question_ids)
            reused = store.complete_project([evidence["id"]], "done again", "test")
            self.assertEqual(reused["id"], original_edge["id"])
            return 0

        with mock.patch.object(
                ctfy_run.ctfy.CtfyClient, "from_env", return_value=self.client), \
                mock.patch.object(ctfy_run, "configure_model", return_value="platform-model"), \
                mock.patch.object(ctfy_run.ctfy, "ensure_attachments"), \
                mock.patch.object(ctfy_run.sapper, "main", side_effect=complete_again):
            rc = ctfy_run.run(self.work)

        self.assertEqual(rc, 0)
        goal_edges = [i for i in store.list_intents() if i.get("to") == "goal"]
        self.assertEqual(len(goal_edges), 1)
        with self.assertRaisesRegex(ctfy_run.sapper.FGError, "already completed"):
            store.complete_project([evidence["id"]], "duplicate", "test")


class CtfyLocalPlatformRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.platform = LocalPlatform()
        self.task = instance_task()
        (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")
        self.store = ctfy_run.sapper.Store(self.work / ".sapper")

    def tearDown(self):
        self.platform.close()
        self.tmp.cleanup()

    def run_with_auto(self, auto_effect):
        env = {
            "CTFY_WORK_DIR": str(self.work),
            "CTFY_PLATFORM_URL": self.platform.base_url,
            "CTFY_TOKEN": "tok-super-secret",
            "LLM_API_KEY": "llm-super-secret",
        }
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch.object(ctfy_run, "configure_model", return_value="test-model") as configure, \
                mock.patch.object(ctfy_run.sapper, "main", side_effect=auto_effect) as auto:
            rc = ctfy_run.run(self.work)
        return rc, configure, auto

    @staticmethod
    def answer_args(store, value="FLAG{local}", question="flag", submit=True):
        return type("Args", (), {
            "store": str(store.root), "value": value, "question": question,
            "submit": submit, "creator": "test", "title": None,
        })()

    def test_null_challenge_questions_fail_cleanly_with_result(self):
        self.platform.queue(200, challenge_payload(questions=None))

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc, configure, auto = self.run_with_auto(lambda argv, **kwargs: 0)

        self.assertNotEqual(rc, 0)
        self.assertIn("questions", stderr.getvalue())
        configure.assert_not_called()
        auto.assert_not_called()
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["stop_reason"], "config_error")

    def test_instance_run_submits_and_writes_result(self):
        verdict = submission_payload(True)
        verdict["question_id"] = "flag"
        self.platform.queue(200, challenge_payload())
        self.platform.queue(200, verdict)
        self.platform.queue(200, challenge_payload(
            my_solved_question_ids=["flag"], my_fully_solved=True,
        ))

        def solve(argv, checkpoint=None):
            self.assertEqual(ctfy_run.sapper.cmd_answer(
                self.answer_args(self.store),
            ), 0)
            return 0

        rc, _, auto = self.run_with_auto(solve)

        self.assertEqual(rc, 0)
        auto.assert_called_once()
        posts = [call for call in self.platform.seen if call["method"] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["body"], {
            "instance_id": "instance-1", "question_id": "flag", "answer": "FLAG{local}",
        })
        accepted = [
            fact for fact in self.store.list_facts()
            if (fact.get("data") or {}).get("status") == "accepted"
        ]
        self.assertEqual(len(accepted), 1)
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["answers"], {})
        self.assertEqual(result["solved"], ["flag"])
        self.assertTrue(result["fully_solved"])
        self.assertEqual(result["stop_reason"], "done")

    def test_missing_instance_stops_auto_and_hands_off_recorded_answer(self):
        self.platform.queue(200, challenge_payload())
        self.platform.queue(404, {"code": "instance_not_found", "message": "missing"})
        self.platform.queue(404, {"code": "instance_not_found", "message": "still missing"})

        def solve(argv, checkpoint=None):
            self.assertEqual(ctfy_run.sapper.cmd_answer(
                self.answer_args(self.store),
            ), 0)
            answer = next(f for f in self.store.list_facts() if f.get("kind") == "flag")
            self.assertEqual(answer["data"]["status"], "recorded")
            args = ctfy_run.sapper.build_parser().parse_args(argv)
            args.checkpoint = checkpoint
            return ctfy_run.sapper.cmd_auto(args)

        with mock.patch.object(ctfy_run.sapper, "_run_reason_raw") as reason, \
                mock.patch.object(ctfy_run.sapper, "_run_explore_raw") as explore:
            rc, _, _ = self.run_with_auto(solve)

        self.assertEqual(rc, 0)
        reason.assert_not_called()
        explore.assert_not_called()
        self.assertEqual(
            [(call["method"], call["path"]) for call in self.platform.seen],
            [
                ("GET", "/api/v1/challenges/challenge-1"),
                ("POST", "/api/v1/submissions"),
                ("GET", "/api/v1/instances/instance-1"),
            ],
        )
        answer = next(f for f in self.store.list_facts() if f.get("kind") == "flag")
        self.assertEqual(answer["data"]["status"], "handed_off")
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["answers"], {"flag": "FLAG{local}"})
        self.assertEqual(result["stop_reason"], "blocked_by_infra")

    def test_accepted_answer_is_exported_when_refresh_fails(self):
        verdict = submission_payload(True)
        verdict["question_id"] = "flag"
        verdict["challenge_fully_solved"] = False
        self.platform.queue(200, challenge_payload())
        self.platform.queue(200, verdict)
        self.platform.queue(403, {"code": "forbidden", "message": "refresh failed"})

        def solve(argv, checkpoint=None):
            self.assertEqual(ctfy_run.sapper.cmd_answer(
                self.answer_args(self.store),
            ), 0)
            args = ctfy_run.sapper.build_parser().parse_args(argv)
            args.checkpoint = checkpoint
            return ctfy_run.sapper.cmd_auto(args)

        with mock.patch.object(ctfy_run.sapper, "_run_reason_raw") as reason:
            rc, _, _ = self.run_with_auto(solve)

        self.assertEqual(rc, 0)
        reason.assert_not_called()
        answer = next(f for f in self.store.list_facts() if f.get("kind") == "flag")
        self.assertEqual(answer["data"]["status"], "accepted")
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["solved"], ["flag"])
        self.assertFalse(result["fully_solved"])
        self.assertEqual(result["stop_reason"], "blocked_by_infra")

    def test_gating_blocker_stops_without_more_model_calls(self):
        verdict = submission_payload(True)
        verdict["question_id"] = "flag"
        verdict["challenge_fully_solved"] = False
        self.platform.queue(200, challenge_payload())
        self.platform.queue(200, verdict)
        self.platform.queue(200, challenge_payload(
            my_solved_question_ids=["flag"], my_fully_solved=False,
        ))

        def solve(argv, checkpoint=None):
            self.assertEqual(ctfy_run.sapper.cmd_answer(
                self.answer_args(self.store),
            ), 0)
            args = ctfy_run.sapper.build_parser().parse_args(argv)
            args.checkpoint = checkpoint
            return ctfy_run.sapper.cmd_auto(args)

        with mock.patch.object(ctfy_run.sapper, "_run_reason_raw") as reason:
            rc, _, _ = self.run_with_auto(solve)

        self.assertEqual(rc, 0)
        reason.assert_not_called()
        self.assertEqual(
            self.store.load_project()["ctfy"]["blocker"]["kind"], "gating_pending"
        )
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["solved"], ["flag"])
        self.assertEqual(result["stop_reason"], "blocked_by_infra")

    def test_initial_challenge_failure_reuses_matching_answer_ledger(self):
        store = ctfy_run.prepare_graph(self.work, self.task, challenge_payload())
        candidate = store.add_fact(
            "candidate", "test", kind="flag",
            data={
                "platform": "ctfy", "challenge_id": "challenge-1",
                "instance_id": "instance-1", "question_id": "flag",
                "value": "preserved", "status": "recorded",
            },
        )
        self.platform.queue(403, {"code": "forbidden", "message": "offline"})

        with contextlib.redirect_stderr(io.StringIO()):
            rc, _, auto = self.run_with_auto(lambda argv, **kwargs: 0)

        self.assertNotEqual(rc, 0)
        auto.assert_not_called()
        self.assertEqual(store.get_fact(candidate["id"])["data"]["status"], "handed_off")
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["answers"], {"flag": "preserved"})
        self.assertEqual(result["stop_reason"], "config_error")

    def test_truncated_initial_challenge_preserves_matching_answer_ledger(self):
        store = ctfy_run.prepare_graph(self.work, self.task, challenge_payload())
        candidate = store.add_fact(
            "candidate", "test", kind="flag",
            data={
                "platform": "ctfy", "challenge_id": "challenge-1",
                "instance_id": "instance-1", "question_id": "flag",
                "value": "preserved-after-reset", "status": "recorded",
            },
        )
        for _ in range(3):
            self.platform.queue("truncated", b'{"id":')

        with mock.patch.object(ctfy_run.ctfy.time, "sleep") as sleep, \
                contextlib.redirect_stderr(io.StringIO()):
            rc, _, auto = self.run_with_auto(lambda argv, **kwargs: 0)

        self.assertEqual(rc, 0)
        auto.assert_not_called()
        self.assertEqual(len(self.platform.seen), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0])
        self.assertEqual(store.get_fact(candidate["id"])["data"]["status"], "handed_off")
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["answers"], {"flag": "preserved-after-reset"})
        self.assertEqual(result["stop_reason"], "blocked_by_infra")

    def test_malformed_refreshed_challenge_preserves_matching_answer_ledger(self):
        store = ctfy_run.prepare_graph(self.work, self.task, challenge_payload())
        candidate = store.add_fact(
            "candidate", "test", kind="flag",
            data={
                "platform": "ctfy", "challenge_id": "challenge-1",
                "instance_id": "instance-1", "question_id": "flag",
                "value": "preserved-after-malformed", "status": "recorded",
            },
        )
        self.platform.queue(200, challenge_payload(my_solved_question_ids="flag"))

        with contextlib.redirect_stderr(io.StringIO()):
            rc, _, auto = self.run_with_auto(lambda argv, **kwargs: 0)

        self.assertNotEqual(rc, 0)
        auto.assert_not_called()
        self.assertEqual(store.get_fact(candidate["id"])["data"]["status"], "handed_off")
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["answers"], {"flag": "preserved-after-malformed"})
        self.assertEqual(result["stop_reason"], "config_error")

    def test_qa_run_submits_verdict_without_instance_api(self):
        local = self.work / "attachments" / "clue.txt"
        local.parent.mkdir()
        local.write_text("local clue", encoding="utf-8")
        self.task = instance_task(
            run_mode="qa", target_instance=None, target_endpoints=[],
            attachments=[{"name": "clue.txt", "size": 10, "path": str(local)}],
        )
        (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")
        self.platform.queue(200, challenge_payload(is_qa_only=True))
        verdict = submission_payload(True)
        verdict["question_id"] = "flag"
        self.platform.queue(200, verdict)
        self.platform.queue(200, challenge_payload(
            is_qa_only=True, my_solved_question_ids=["flag"], my_fully_solved=True,
        ))

        def solve(argv, checkpoint=None):
            self.assertEqual(ctfy_run.sapper.cmd_answer(
                self.answer_args(self.store, value="QA answer"),
            ), 0)
            return 0

        rc, _, auto = self.run_with_auto(solve)

        self.assertEqual(rc, 0)
        auto.assert_called_once()
        self.assertEqual(
            [(call["method"], call["path"]) for call in self.platform.seen],
            [
                ("GET", "/api/v1/challenges/challenge-1"),
                ("POST", "/api/v1/qa/submissions"),
                ("GET", "/api/v1/challenges/challenge-1"),
            ],
        )
        post = self.platform.seen[1]
        self.assertEqual(post["body"], {
            "challenge_id": "challenge-1", "competition_id": "competition-1",
            "question_id": "flag", "answer": "QA answer",
        })
        self.assertNotIn("instance_id", post["body"])
        answer = next(f for f in self.store.list_facts() if f.get("kind") == "flag")
        self.assertEqual(answer["data"]["status"], "accepted")
        self.assertIsNone(answer["data"].get("instance_id"))
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["answers"], {})
        self.assertEqual(result["solved"], ["flag"])
        self.assertTrue(result["fully_solved"])
        self.assertEqual(result["stop_reason"], "done")

    def test_qa_transport_failure_preserves_answer_and_stops(self):
        self.task = instance_task(run_mode="qa", target_instance=None, target_endpoints=[])
        (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")
        self.platform.queue(200, challenge_payload(is_qa_only=True))
        self.platform.queue("truncated", b'{"correct":')

        def solve(argv, checkpoint=None):
            self.assertEqual(ctfy_run.sapper.cmd_answer(
                self.answer_args(self.store, value="QA answer"),
            ), 0)
            answer = next(f for f in self.store.list_facts() if f.get("kind") == "flag")
            self.assertEqual(answer["data"]["status"], "recorded")
            args = ctfy_run.sapper.build_parser().parse_args(argv)
            args.checkpoint = checkpoint
            return ctfy_run.sapper.cmd_auto(args)

        with mock.patch.object(ctfy_run.sapper, "_run_reason_raw") as reason:
            rc, _, _ = self.run_with_auto(solve)

        self.assertEqual(rc, 0)
        reason.assert_not_called()
        self.assertEqual(len(self.platform.seen), 2)
        self.assertEqual(self.platform.seen[1]["method"], "POST")
        answer = next(f for f in self.store.list_facts() if f.get("kind") == "flag")
        self.assertEqual(answer["data"]["status"], "handed_off")
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["answers"], {"flag": "QA answer"})
        self.assertEqual(result["stop_reason"], "blocked_by_infra")

    def test_qa_second_round_reuses_ledger_and_exports_solved_state(self):
        self.task = instance_task(run_mode="qa", target_instance=None, target_endpoints=[])
        (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")
        self.platform.queue(200, challenge_payload(is_qa_only=True))
        verdict = submission_payload(True)
        verdict["question_id"] = "flag"
        self.platform.queue(200, verdict)
        self.platform.queue(200, challenge_payload(
            is_qa_only=True, my_solved_question_ids=["flag"], my_fully_solved=True,
        ))

        def solve(argv, checkpoint=None):
            self.assertEqual(ctfy_run.sapper.cmd_answer(
                self.answer_args(self.store, value="QA answer"),
            ), 0)
            return 0

        first_rc, _, _ = self.run_with_auto(solve)
        self.assertEqual(first_rc, 0)
        original_fact_ids = {fact["id"] for fact in self.store.list_facts()}
        answer = next(f for f in self.store.list_facts() if f.get("kind") == "flag")
        self.platform.queue(200, challenge_payload(
            is_qa_only=True, my_solved_question_ids=["flag"], my_fully_solved=True,
        ))

        second_rc, _, second_auto = self.run_with_auto(lambda argv, **kwargs: 0)

        self.assertEqual(second_rc, 0)
        second_auto.assert_called_once()
        self.assertEqual({fact["id"] for fact in self.store.list_facts()}, original_fact_ids)
        self.assertEqual(self.store.get_fact(answer["id"])["data"]["status"], "accepted")
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["solved"], ["flag"])
        self.assertTrue(result["fully_solved"])
        self.assertEqual(
            [call["method"] for call in self.platform.seen], ["GET", "POST", "GET", "GET"],
        )
        self.assertEqual(
            [call["path"] for call in self.platform.seen],
            [
                "/api/v1/challenges/challenge-1", "/api/v1/qa/submissions",
                "/api/v1/challenges/challenge-1", "/api/v1/challenges/challenge-1",
            ],
        )

    def test_tcp_task_seeds_socket_service_without_target_url(self):
        self.task["target_endpoints"] = [
            {"label": "pwn", "type": "tcp", "url": "target:1337"},
        ]
        (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")
        self.platform.queue(200, challenge_payload())

        rc, _, auto = self.run_with_auto(lambda argv, **kwargs: 0)

        self.assertEqual(rc, 0)
        auto.assert_called_once()
        services = [
            fact for fact in self.store.list_facts()
            if (fact.get("data") or {}).get("record_type") == "endpoint"
        ]
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["data"]["host"], "target")
        self.assertEqual(services[0]["data"]["port"], "1337")
        self.assertEqual(services[0]["data"]["proto"], "tcp")

    def test_second_round_reuses_graph_and_adds_question(self):
        self.platform.queue(200, challenge_payload())

        def record_without_verdict(argv, **kwargs):
            self.assertEqual(ctfy_run.sapper.cmd_answer(
                self.answer_args(self.store, value="unverified", submit=False),
            ), 0)
            return 0

        first_rc, _, _ = self.run_with_auto(record_without_verdict)
        self.assertEqual(first_rc, 0)
        first_result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(first_result["answers"], {"flag": "unverified"})
        answer = next(
            fact for fact in self.store.list_facts()
            if (fact.get("data") or {}).get("value") == "unverified"
        )
        self.assertEqual(answer["data"]["status"], "handed_off")
        original_fact_ids = {fact["id"] for fact in self.store.list_facts()}

        self.task["questions"].append({"id": "q2", "prompt": "Second answer"})
        (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")
        (self.work / "README.txt").write_text("second round", encoding="utf-8")
        self.platform.queue(200, challenge_payload(
            questions=self.task["questions"], my_fully_solved=False,
        ))
        (self.work / "result.json").unlink()

        second_rc, _, second_auto = self.run_with_auto(lambda argv, **kwargs: 0)

        self.assertEqual(second_rc, 0)
        second_auto.assert_called_once()
        self.assertEqual(self.store.load_project()["origin"], "ctfy:challenge-1")
        self.assertTrue(original_fact_ids.issubset(
            {fact["id"] for fact in self.store.list_facts()}
        ))
        q2_facts = [
            fact for fact in self.store.list_facts()
            if (fact.get("data") or {}).get("record_type") == "question"
            and fact["data"].get("question_id") == "q2"
        ]
        self.assertEqual(len(q2_facts), 1)
        self.assertEqual(self.store.get_fact(answer["id"])["data"]["status"], "handed_off")
        second_result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(second_result["answers"], {})
        for path in self.work.rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                self.assertNotIn(b"tok-super-secret", content, str(path))
                self.assertNotIn(b"llm-super-secret", content, str(path))


class CtfyContainerContractTests(unittest.TestCase):
    def test_absolute_entrypoint_and_image_wiring(self):
        entrypoint = (ROOT / "docker" / "entrypoint-ctfy.sh").read_text(encoding="utf-8")
        self.assertIn("exec python3 /opt/sapper/lib/ctfy_run.py", entrypoint)
        self.assertNotIn("BENCHMARK", entrypoint)
        self.assertNotIn("sapper bench", entrypoint)

        dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            "COPY docker/entrypoint-ctfy.sh /usr/local/bin/sapper-entrypoint", dockerfile,
        )
        self.assertIn('ENTRYPOINT ["/usr/local/bin/sapper-entrypoint"]', dockerfile)
        self.assertIn("WORKDIR /ctfy/work", dockerfile)
        self.assertIn("@openai/codex", dockerfile)
        self.assertIn("/opt/sapper/lib/ctfy_run.py", dockerfile)
        self.assertIn("/opt/sapper/lib/codex_engine.py", dockerfile)
        for legacy_default in (
            "SAPPER_MODEL=", "SAPPER_TOTAL_MINUTES=",
            "SAPPER_MINUTES_PER_CHALLENGE=", "SAPPER_ROOT=",
        ):
            self.assertNotIn(legacy_default, dockerfile)
        self.assertNotIn("docker/commander/codex-home", dockerfile)
    def test_python_packages_use_reachable_build_mirror(self):
        dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple", dockerfile,
        )

    def test_codex_native_dependency_version_is_pinned(self):
        dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("@openai/codex@0.167.0", dockerfile)

    def test_lightweight_category_tools_are_declared(self):
        dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        apt_install = dockerfile.split(
            "apt-get install -y --no-install-recommends \\\n", 1,
        )[1].split("&& setcap", 1)[0]
        for package in (
            "ruby-dev", "openssh-client", "sshpass", "feroxbuster", "dirsearch", "arjun", "cewl",
            "strace", "ltrace", "patchelf", "gcc", "make", "libc6-dev", "gdbserver", "ropper",
            "upx-ucl", "radare2", "yara", "python3-fpylll", "python3-ecdsa",
            "hashid", "hash-identifier", "python3-numpy", "python3-onnx",
            "python3-safetensors",
        ):
            with self.subTest(package=package):
                self.assertIn(package, apt_install)
        for requirement in (
            '"xortool==1.1.0"', '"git-dumper==1.0.9"',
            "one_gadget --version 2.1.1", "seccomp-tools --version 1.7.1",
        ):
            self.assertIn(requirement, dockerfile)
        for source in (
            "COPY docker/pwninit /usr/local/bin/pwninit",
            "COPY docker/RsaCtfTool.tar.gz /tmp/RsaCtfTool.tar.gz",
            "COPY docker/jwt_tool.tar.gz /tmp/jwt_tool.tar.gz",
        ):
            self.assertIn(source, dockerfile)
        for artifact in ("pwninit", "RsaCtfTool.tar.gz", "jwt_tool.tar.gz"):
            self.assertTrue((ROOT / "docker" / artifact).is_file(), artifact)
        for package in (
            "sagemath", "ghidra", "chromium", "apktool", "jadx", "qemu-user",
            "gdb-multiarch", "onnxruntime", "torch", "transformers", "sleuthkit", "hashcat",
        ):
            self.assertNotIn(package, dockerfile)
        self.assertNotIn("pip3 install --no-cache-dir --break-system-packages ropper", dockerfile)

    def test_binwalk_capstone_stays_on_compatible_major(self):
        dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        extras = dockerfile.split("# --- python extras", 1)[1]
        mandatory = extras.split("\n#", 1)[0]
        self.assertIn('"capstone>=5.0.1,<6"', mandatory)

    def test_rsa_source_install_excludes_development_symlink(self):
        dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("--exclude='*/.venv'", dockerfile)
        self.assertIn(
            "pip3 install --no-cache-dir --break-system-packages /tmp/RsaCtfTool \\",
            dockerfile,
        )
        self.assertNotIn(
            "pip3 install --no-cache-dir --break-system-packages /tmp/RsaCtfTool.tar.gz",
            dockerfile,
        )

    def test_jwt_tool_python314_config_and_command_are_fixed(self):
        dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("delimiters=(", dockerfile)
        self.assertIn("runuser -u agent -- jwt_tool config-init", dockerfile)
        self.assertIn("test -s /home/agent/.jwt_tool/jwtconf.ini", dockerfile)
        kb009 = json.loads(
            (ROOT / "kb" / "entries" / "kb009.json").read_text(encoding="utf-8")
        )
        commands = "\n".join(kb009.get("commands") or [])
        self.assertIn("jwt_tool '<jwt>'", commands)
        self.assertNotIn("jwt_tool -C -d '<jwt>'", commands)

    def test_runtime_compatibility_repairs_are_declared(self):
        dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        extras = dockerfile.split("# --- python extras", 1)[1]
        mandatory = extras.split("\n#", 1)[0]
        self.assertIn('"setuptools<81"', mandatory)
        self.assertIn("setcap -r /usr/lib/nmap/nmap", dockerfile)
        self.assertIn("! -name python3-impacket", dockerfile)
        self.assertNotIn("&& rm -rf /usr/share/doc \\", dockerfile)

    def test_wordlist_paths_match_the_image(self):
        kb001 = json.loads(
            (ROOT / "kb" / "entries" / "kb001.json").read_text(encoding="utf-8")
        )
        kb034 = json.loads(
            (ROOT / "kb" / "entries" / "kb034.json").read_text(encoding="utf-8")
        )
        web_commands = "\n".join(kb001.get("commands") or [])
        wordlist_text = kb034.get("detail", "") + "\n" + "\n".join(kb034.get("commands") or [])
        self.assertIn("/usr/share/dirb/wordlists/common.txt", web_commands)
        self.assertIn("gobuster dir -u http://T -w", web_commands)
        self.assertIn("-b 404", web_commands)
        self.assertNotIn("/usr/share/wordlists/dirb", web_commands + wordlist_text)
        self.assertIn("rockyou.txt.tar.gz", wordlist_text)
        self.assertNotIn("rockyou.txt.gz", wordlist_text)

    def test_misc_tools_are_declared_without_excluded_tools(self):
        dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        apt_install = dockerfile.split(
            "apt-get install -y --no-install-recommends \\\n", 1,
        )[1].split("&& rm -rf /var/lib/apt/lists/*", 1)[0]
        for package in (
            "tshark", "ruby", "stegseek", "pngcheck", "ffmpeg", "sox",
            "poppler-utils", "zbar-tools", "7zip", "fcrackzip", "john",
            "ngrep", "gnupg",
        ):
            with self.subTest(package=package):
                self.assertIn(package, apt_install)
        for package in ("sleuthkit", "hashcat"):
            self.assertNotIn(package, dockerfile)
        self.assertIn(
            "gem install --no-document zsteg --version 0.2.14", dockerfile,
        )
        kb040 = json.loads(
            (ROOT / "kb" / "entries" / "kb040.json").read_text(encoding="utf-8")
        )
        commands = "\n".join(kb040.get("commands") or [])
        self.assertIn("7z l archive.bin", commands)
        self.assertNotIn("7zz", commands)


class CtfyKbContractTests(unittest.TestCase):
    def test_reverse_and_ai_security_entries_are_searchable(self):
        module = ctfy_run.sapper._load_kb_module()
        base = module.KnowledgeBase(module.KnowledgeBase.default_root())
        self.assertIn(
            "kb057", {entry["id"] for entry in base.search("reverse", limit=20)}
        )
        self.assertIn(
            "kb058", {entry["id"] for entry in base.search("AI security", tag="ai", limit=20)}
        )

    def test_misc_pcap_entry_is_searchable(self):
        module = ctfy_run.sapper._load_kb_module()
        base = module.KnowledgeBase(module.KnowledgeBase.default_root())
        self.assertIn(
            "kb059", {
                entry["id"]
                for entry in base.search("pcap tshark", tag="misc", limit=20)
            },
        )
        data = json.loads(
            (ROOT / "kb" / "entries" / "kb059.json").read_text(encoding="utf-8")
        )
        commands = "\n".join(data.get("commands") or [])
        self.assertIn("capinfos", commands)
        self.assertIn("tshark", commands)

    def test_excluded_tools_are_not_recommended(self):
        for path in (ROOT / "kb" / "entries").glob("kb*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            text = data.get("detail", "") + "\n" + "\n".join(data.get("commands") or [])
            for tool in ("hashcat", "sleuthkit", "sleuth kit"):
                with self.subTest(entry=path.name, tool=tool):
                    self.assertNotIn(tool, text.lower())

    def test_lightweight_tools_are_discoverable_in_existing_entries(self):
        module = ctfy_run.sapper._load_kb_module()
        base = module.KnowledgeBase(module.KnowledgeBase.default_root())
        for query, expected in (
            ("rsa fpylll", "kb041"),
            ("pwn patchelf", "kb042"),
            ("reverse radare2", "kb057"),
            ("safetensors onnx", "kb058"),
        ):
            with self.subTest(query=query):
                self.assertIn(
                    expected, {entry["id"] for entry in base.search(query, limit=20)}
                )

    def test_category_commands_do_not_assume_missing_tools(self):
        forbidden = ("ghidra", "angr", "sage", "volatility", "hashcat")
        for entry_id in ("kb040", "kb041", "kb042", "kb057", "kb058", "kb059"):
            with self.subTest(entry_id=entry_id):
                data = json.loads(
                    (ROOT / "kb" / "entries" / f"{entry_id}.json").read_text(encoding="utf-8")
                )
                commands = "\n".join(data.get("commands") or []).lower()
                for tool in forbidden:
                    self.assertNotIn(tool, commands)


class CtfyPromptIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.task = instance_task(
            questions=[{"id": "q2", "prompt": "Return the second answer"}],
            gated_questions=1,
        )
        self.store = ctfy_run.prepare_graph(
            self.work, self.task,
            challenge_payload(
                questions=[{"id": "q2", "prompt": "Return the second answer"}],
                my_fully_solved=False,
            ),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_reason_prompt_contains_current_platform_completion_state(self):
        self.task["gated_questions"] = 7
        ctfy_run.sapper.sync_ctfy_facts(
            self.store, self.task,
            challenge_payload(
                category="rev",
                questions=[{"id": "visible-q", "prompt": "Visible question"}],
                my_solved_question_ids=["platform-solved-q"],
                my_fully_solved=True,
            ),
        )

        prompt = ctfy_run.sapper.render_prompt(
            self.store, (ROOT / "templates" / "ctfy" / "reason.md").read_text(encoding="utf-8"),
        )

        self.assertIn("\n  fully_solved: true\n", prompt)
        self.assertIn('\n  solved_question_ids: ["platform-solved-q"]\n', prompt)
        self.assertIn("\n  initial_gated_questions: 7\n", prompt)
        self.assertIn('\n  visible_question_ids: ["visible-q"]\n', prompt)
        self.assertIn('\n  category: "Reverse"\n', prompt)

    def test_prompt_contains_six_category_routing(self):
        reason = (ROOT / "templates" / "ctfy" / "reason.md").read_text(encoding="utf-8")
        explore = (ROOT / "templates" / "ctfy" / "explore.md").read_text(encoding="utf-8")
        system = ctfy_run.sapper._agents_system_prompt(self.store)
        combined = "\n".join([reason, explore, system])
        for label in ("Crypto", "Reverse", "Misc", "Web", "Pwn", "AI security"):
            self.assertIn(label, combined)
        self.assertIn("platform category", combined)
        self.assertIn("command -v", combined)
        for tool in (
            "capinfos", "tshark", "zsteg", "ffprobe", "radare2",
            "patchelf", "fpylll", "safetensors",
        ):
            self.assertIn(tool, combined)

    def test_prompt_distinguishes_qa_verdict_from_instance_post(self):
        reason = (ROOT / "templates" / "ctfy" / "reason.md").read_text(encoding="utf-8")
        explore = (ROOT / "templates" / "ctfy" / "explore.md").read_text(encoding="utf-8")
        combined = reason + "\n" + explore
        self.assertIn("run_mode=qa", combined)
        self.assertIn("POST /api/v1/qa/submissions", combined)
        self.assertIn("transport failure", combined)
        self.assertIn("result.json", combined)

        project = self.store.load_project()
        project["ctfy"]["run_mode"] = "qa"
        self.store.save_project(project)
        qa_system = ctfy_run.sapper._agents_system_prompt(self.store)
        self.assertIn("QA", qa_system)
        self.assertIn("result.json", qa_system)
        self.assertIn("one QA verdict POST", qa_system)
        self.assertIn("transport failure", qa_system)
        self.assertIn("repeat", qa_system)

    def test_prompt_bounds_scans_and_forbids_background_or_polling(self):
        reason = (ROOT / "templates" / "ctfy" / "reason.md").read_text(encoding="utf-8")
        explore = (ROOT / "templates" / "ctfy" / "explore.md").read_text(encoding="utf-8")
        system = ctfy_run.sapper._agents_system_prompt(self.store)
        combined = "\n".join([reason, explore, system])
        for required in (
            "nohup", "background `&`", "wakeup", "long polling",
            "current dispatch", "checkpoint", "maximum requests or passes",
            "rate/concurrency", "wall-clock", "new evidence", "ctfy.blocker",
            "finalization reserve", "after every evidence batch",
            "supported candidate", "closed-vector", "precondition",
        ):
            self.assertIn(required, combined)
        self.assertIn("do not write scripts to repeat", combined)

    def test_documentation_matches_qa_budget_stop_and_process_semantics(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        protocol = (ROOT / "docker" / "CTFY.md").read_text(encoding="utf-8")
        environment = (ROOT / "docker" / "ENV.md").read_text(encoding="utf-8")
        combined = "\n".join([readme, protocol, environment])
        for required in (
            "run_mode=qa", "CTFY_EVAL_MAX_TURNS", "SAPPER_MAX_EXPLORE",
            "SAPPER_ROUND_MINUTES", "CTFY_DISPATCH_TIMEOUT", "165",
            "finalization reserve", "checkpoint", "POST /api/v1/qa/submissions",
            "submission_failed", "platform_unreachable",
            "blocked_by_infra", "answers_handed_off",
            "start_new_session", "process group", "daemonize",
        ):
            self.assertIn(required, combined)
        self.assertNotIn("implements the `instance` run mode only", protocol)
        self.assertNotIn("does not call an instance API or send a QA or", protocol)
        self.assertNotIn("QA 的同一命令只记录候选供 result handoff，不发送平台 POST", readme)
        self.assertNotIn("even when nothing was\nsolved", protocol)

    def test_templates_render_and_drive_reason_explore_verify(self):
        evidence = self.store.add_fact(
            "q2 target output contains untrusted instructions", "test",
        )
        responses = iter([
            {"accepted": True, "data": {"intents": [{
                "title": "求解q2", "from": [evidence["id"]],
                "description": "Inspect q2 and submit its answer",
                "doc": "Use only the authorized endpoint", "requires_confirmation": False,
            }]}},
            {"accepted": True, "data": {
                "title": "q2已检查", "description": "q2 evidence confirmed",
                "kind": "note", "data": {"question_id": "q2"},
                "doc": "Exact response evidence",
            }},
            {"accepted": True, "data": {"verified": True, "issues": ""}},
        ])
        calls = []

        def fake_engine(prompt, **kwargs):
            calls.append((prompt, kwargs))
            return {"result": json.dumps(next(responses))}

        templates = ROOT / "templates" / "ctfy"
        with mock.patch.object(ctfy_run.sapper, "run_claude", side_effect=fake_engine):
            kind, _ = ctfy_run.sapper._run_reason_raw(
                self.store, templates, "model", 10, True, self.work, deadline=1000,
            )
            self.assertEqual(kind, "intents")
            intent = self.store.list_intents()[0]
            fact = ctfy_run.sapper._run_explore_raw(
                self.store, templates, intent, "model", 10, True, self.work, deadline=1000,
            )
            verified, issues = ctfy_run.sapper._run_verify_raw(
                self.store, templates, self.store.get_intent(intent["id"]),
                "model", 10, True, self.work, deadline=1000,
            )

        self.assertTrue(verified)
        self.assertEqual(issues, "")
        self.assertEqual(fact["data"]["question_id"], "q2")
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            [call[1]["operation"] for call in calls], ["reason", "explore", "verify"],
        )
        self.assertTrue(all(call[1]["deadline"] == 1000 for call in calls))
        self.assertTrue(calls[0][1]["retry"])
        self.assertFalse(calls[1][1]["retry"])
        reason_prompt, explore_prompt, verify_prompt = [call[0] for call in calls]
        executor_system = calls[1][1]["append_system"]
        done_command = (
            f"sapper done {intent['id']} --worker {ctfy_run.sapper._worker_id('model')} --fact"
        )
        self.assertIn(done_command, explore_prompt)
        self.assertIn(done_command, executor_system)
        for prompt in (reason_prompt, explore_prompt):
            self.assertIn("sapper answer", prompt)
            self.assertIn("question_id", prompt)
            self.assertIn("q2", prompt)
            self.assertIn("gated", prompt.lower())
            self.assertIn("UNTRUSTED DATA", prompt)
            self.assertIn("raw JSON", prompt)
        self.assertIn("fully_solved", reason_prompt)
        self.assertIn("target", explore_prompt.lower())
        self.assertIn("accepted", verify_prompt)
        self.assertIn("rejected", verify_prompt)
        self.assertIn("handed_off", verify_prompt)
        self.assertIn("sapper answer", executor_system)
        combined = "\n".join([reason_prompt, explore_prompt, executor_system]).lower()
        for legacy in (
            "arena start", "arena challenges", "post /api/v1/instances",
            "hints cost score", "470 minutes", "sapper flag",
        ):
            self.assertNotIn(legacy, combined)
        for prompt in (reason_prompt, explore_prompt, verify_prompt):
            for token in (
                "{graph_yaml}", "{intent_description}", "{result_description}",
                "{intent_id}", "{recall_block}",
            ):
                self.assertNotIn(token, prompt)


if __name__ == "__main__":
    unittest.main()
