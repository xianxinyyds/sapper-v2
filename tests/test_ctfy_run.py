import contextlib
import io
import json
import sys
import tempfile
import unittest
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
            ({"schema_version": 3, "run_mode": "qa", "questions": []}, "unsupported_run_mode"),
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

    def run_with_mocks(self, auto_return=0, environ=None):
        with mock.patch.object(
                ctfy_run.ctfy.CtfyClient, "from_env", return_value=self.client), \
                mock.patch.object(ctfy_run, "configure_model", return_value="platform-model") as configure, \
                mock.patch.object(ctfy_run.ctfy, "ensure_attachments") as attachments, \
                mock.patch.object(ctfy_run.sapper, "main", return_value=auto_return) as auto, \
                mock.patch.dict("os.environ", environ or {}, clear=True):
            rc = ctfy_run.run(self.work)
        return rc, configure, attachments, auto

    def test_run_initializes_single_graph_and_invokes_auto(self):
        rc, configure, attachments, auto = self.run_with_mocks()

        self.assertEqual(rc, 0)
        self.client.get_challenge.assert_called_once_with("challenge-1")
        configure.assert_called_once_with()
        attachments.assert_called_once_with(self.task, self.client, self.work)
        auto.assert_called_once_with([
            "--store", str(self.work / ".sapper"), "auto",
            "--model", "platform-model",
            "--templates", str(ROOT / "templates" / "ctfy"),
            "--skip-permissions", "--max-steps", "5", "--max-explore", "5",
        ])
        store = ctfy_run.sapper.Store(self.work / ".sapper")
        self.assertEqual(store.load_project()["origin"], "ctfy:challenge-1")
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["answers"], {})
        self.assertEqual(result["stop_reason"], "done")

    def test_default_steps_and_auto_failure_still_write_result(self):
        self.task["max_turns"] = None
        (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")

        rc, _, _, auto = self.run_with_mocks(auto_return=1)

        self.assertEqual(rc, 1)
        argv = auto.call_args.args[0]
        self.assertEqual(argv[argv.index("--max-steps") + 1], "8")
        self.assertEqual(argv[argv.index("--max-explore") + 1], "6")
        self.assertNotIn("--verify", argv)
        result = json.loads((self.work / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["stop_reason"], "auto_failed")

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

        def complete_again(argv):
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
            rc, configure, auto = self.run_with_auto(lambda argv: 0)

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

        def solve(argv):
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

    def test_tcp_task_seeds_socket_service_without_target_url(self):
        self.task["target_endpoints"] = [
            {"label": "pwn", "type": "tcp", "url": "target:1337"},
        ]
        (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")
        self.platform.queue(200, challenge_payload())

        rc, _, auto = self.run_with_auto(lambda argv: 0)

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

        def record_without_verdict(argv):
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

        second_rc, _, second_auto = self.run_with_auto(lambda argv: 0)

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

    def test_ropper_python314_incompatibility_cannot_block_image_build(self):
        dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        extras = dockerfile.split("# --- python extras", 1)[1]
        mandatory = extras.split("\n#", 1)[0]
        self.assertNotIn("ropper", mandatory)
        self.assertIn(
            "RUN pip3 install --no-cache-dir --break-system-packages ropper \\\n"
            "        || echo \"WARN: ropper unavailable",
            dockerfile,
        )


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
                self.store, templates, "model", 10, True, self.work,
            )
            self.assertEqual(kind, "intents")
            intent = self.store.list_intents()[0]
            fact = ctfy_run.sapper._run_explore_raw(
                self.store, templates, intent, "model", 10, True, self.work,
            )
            verified, issues = ctfy_run.sapper._run_verify_raw(
                self.store, templates, self.store.get_intent(intent["id"]),
                "model", 10, True, self.work,
            )

        self.assertTrue(verified)
        self.assertEqual(issues, "")
        self.assertEqual(fact["data"]["question_id"], "q2")
        self.assertEqual(len(calls), 3)
        reason_prompt, explore_prompt, verify_prompt = [call[0] for call in calls]
        executor_system = calls[1][1]["append_system"]
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
