import contextlib
import io
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import sapper  # noqa: E402
import ctfy  # noqa: E402

SETTINGS_PATH = ROOT / "lib" / "_settings_patch.py"
_spec = importlib.util.spec_from_file_location("settings_patch_for_tests", SETTINGS_PATH)
settings_patch = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(settings_patch)


class GraphIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = sapper.Store(Path(self.tmp.name) / ".sapper")
        self.store.init("test", "ship it")

    def tearDown(self):
        self.tmp.cleanup()

    def test_corrupt_node_is_skipped_and_reported_by_validate(self):
        (self.store.facts_dir / "f001.json").write_text('{"id":"f001"}\n', encoding="utf-8")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rendered = sapper.render_graph_text(self.store)
        self.assertIn("goal: ship it", rendered)
        self.assertIn("missing string 'description'", stderr.getvalue())
        errors = sapper.validate_graph(self.store)
        self.assertTrue(any("f001.json" in error for error in errors))

    def test_init_refuses_non_empty_directory(self):
        root = Path(self.tmp.name) / "new-store"
        root.mkdir()
        (root / "unrelated.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(sapper.FGError, "non-empty directory"):
            sapper.Store(root).init("", "goal")
        self.assertTrue((root / "unrelated.txt").exists())

    def test_validate_detects_goal_metadata_drift(self):
        goal = self.store.facts_dir / "goal.json"
        data = json.loads(goal.read_text(encoding="utf-8"))
        data["description"] = "different goal"
        goal.write_text(json.dumps(data), encoding="utf-8")
        errors = sapper.validate_graph(self.store)
        self.assertIn("goal fact description does not match project goal", errors)

    def test_validate_detects_intent_state_mismatch(self):
        seed = self.store.add_fact("seed", "test", None)
        intent = self.store.add_intent([seed["id"]], "work", "test")
        path = self.store.intents_dir / f"{intent['id']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "done"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = sapper.validate_graph(self.store)
        self.assertTrue(any("completed status requires a target" in error for error in errors))

    def test_confirmation_is_enforced_at_write_boundary(self):
        fact = self.store.add_fact("starting point", "test", None)
        intent = self.store.add_intent(
            [fact["id"]], "delete data", "test", requires_confirmation=True
        )
        with self.assertRaisesRegex(sapper.FGError, "requires confirmation"):
            self.store.conclude_with_new_fact(intent["id"], "must not run", "test")
        self.store.confirm_intent(intent["id"])
        self.store.claim_intent(intent["id"], "owner")
        with self.assertRaisesRegex(sapper.FGError, "claimed by owner"):
            self.store.conclude_with_new_fact(intent["id"], "wrong worker", "other")
        result, concluded = self.store.conclude_with_new_fact(
            intent["id"], "approval was recorded", "owner"
        )
        self.assertEqual(concluded["to"], result["id"])

    def test_lookup_ids_cannot_escape_store(self):
        with self.assertRaisesRegex(sapper.FGError, "invalid fact id"):
            self.store.get_fact("../../outside")

    def test_stale_counter_never_overwrites_existing_node(self):
        first = self.store.add_fact("first", "test", None)
        project = self.store.load_project()
        project["counters"]["fact"] = 0
        self.store.save_project(project)
        second = self.store.add_fact("second", "test", None)
        self.assertEqual(first["id"], "f001")
        self.assertEqual(second["id"], "f002")
        self.assertEqual(self.store.get_fact("f001")["description"], "first")

    def test_fact_from_intent_closes_the_intent_atomically(self):
        seed = self.store.add_fact("seed", "test", None)
        intent = self.store.add_intent([seed["id"]], "observe", "test")
        fact = self.store.add_fact("observed", "test", intent["id"])
        self.assertEqual(self.store.get_intent(intent["id"])["to"], fact["id"])
        self.assertEqual(sapper.validate_graph(self.store), [])

    def test_linking_existing_fact_sets_its_source_edge(self):
        seed = self.store.add_fact("seed", "test", None)
        result = self.store.add_fact("already observed", "test", None)
        intent = self.store.add_intent([seed["id"]], "link result", "test")
        self.store.conclude_intent(intent["id"], result["id"], "test")
        self.assertEqual(self.store.get_fact(result["id"])["source_intent"], intent["id"])
        self.assertEqual(sapper.validate_graph(self.store), [])

    def test_pick_claim_skips_another_workers_lease(self):
        seed = self.store.add_fact("seed", "test", None)
        first = self.store.add_intent([seed["id"]], "first", "test")
        second = self.store.add_intent([seed["id"]], "second", "test")
        self.store.claim_intent(first["id"], "other-worker")
        args = type("Args", (), {
            "store": str(self.store.root), "claim": True,
            "worker": "my-worker", "any": False,
        })()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            sapper.cmd_pick(args)
        self.assertEqual(output.getvalue().strip(), second["id"])

    def test_static_view_escapes_script_terminators(self):
        self.store.add_fact("</script><script>alert(1)</script>", "test", None)
        html = sapper._render_static_html(self.store)
        self.assertNotIn("</script><script>", html)
        self.assertIn(r"\u003c/script\u003e", html)

    def test_prompt_substitution_does_not_rewrite_inserted_braces(self):
        self.store.add_fact("literal {goal} marker", "test", None)
        prompt = sapper.render_prompt(self.store, "{graph_yaml}\nGOAL={goal}")
        self.assertIn("literal {goal} marker", prompt)
        self.assertTrue(prompt.endswith("GOAL=ship it"))

    def test_brainstorm_template_token_does_not_rewrite_graph_text(self):
        self.store.add_fact("literal {max_options} marker", "test", None)
        args = type("Args", (), {
            "max_options": 2, "dry_run": True,
        })()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            sapper._dispatch_brainstorm(
                self.store, ROOT / "templates", "sonnet", 10, False, ROOT, args
            )
        self.assertIn("literal {max_options} marker", output.getvalue())

    def test_verify_payload_requires_actionable_issues_on_failure(self):
        with self.assertRaisesRegex(sapper.FGError, "explain"):
            sapper._parse_verify_payload({"verified": False, "issues": ""})
        with self.assertRaisesRegex(sapper.FGError, "leave 'issues' empty"):
            sapper._parse_verify_payload({"verified": True, "issues": "warning"})

    def test_dispatch_verify_returns_failure_status(self):
        seed = self.store.add_fact("seed", "test", None)
        intent = self.store.add_intent([seed["id"]], "check", "test")
        result, _ = self.store.conclude_with_new_fact(intent["id"], "result", "test")
        args = type("Args", (), {
            "intent": intent["id"], "dry_run": False,
        })()
        with mock.patch.object(
            sapper,
            "run_claude",
            return_value={"result": '{"accepted": true, "data": {"verified": false, "issues": "mismatch"}}'},
        ):
            rc = sapper._dispatch_verify(
                self.store, ROOT / "templates", "sonnet", 10, False, ROOT, args
            )
        self.assertEqual(rc, 1)

    def test_completed_graph_validates_and_duplicate_completion_is_rejected(self):
        fact = self.store.add_fact("evidence", "test", None)
        self.store.complete_project([fact["id"]], "finish", "test")
        self.assertEqual(self.store.load_project()["status"], "completed")
        self.assertEqual(sapper.validate_graph(self.store), [])
        with self.assertRaisesRegex(sapper.FGError, "already completed"):
            self.store.complete_project([fact["id"]], "finish again", "test")

    def test_brainstorm_batches_are_unique(self):
        seed = self.store.add_fact("seed", "test", None)
        payload = {
            "accepted": True,
            "data": {
                "options": [
                    {"from": [seed["id"]], "title": "one", "description": "first"}
                ]
            },
        }
        sapper._apply_brainstorm(self.store, payload)
        sapper._apply_brainstorm(self.store, payload)
        docs = [i["doc"] for i in self.store.list_intents()]
        self.assertEqual(len(docs), 2)
        tags = [line for doc in docs for line in doc.splitlines() if line.startswith("[brainstorm batch:")]
        self.assertEqual(len(set(tags)), 2)

    def test_brainstorm_pick_preserves_abandoned_edges(self):
        seed = self.store.add_fact("seed", "test", None)
        payload = {
            "accepted": True,
            "data": {"options": [
                {"from": [seed["id"]], "title": "one", "description": "first"},
                {"from": [seed["id"]], "title": "two", "description": "second"},
            ]},
        }
        sapper._apply_brainstorm(self.store, payload)
        picked = self.store.list_intents()[0]
        args = type("Args", (), {"store": str(self.store.root), "id": picked["id"], "purge": False})()
        sapper.cmd_brainstorm_pick(args)
        intents = self.store.list_intents()
        self.assertEqual(sum(i["to"] is None for i in intents), 1)
        self.assertEqual(sum(i["to"] is not None for i in intents), 1)
        self.assertEqual(sapper.validate_graph(self.store), [])


class SettingsPatchTests(unittest.TestCase):
    def test_settings_patch_is_atomic_and_preserves_unrelated_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(
                json.dumps({"hooks": {"SessionStart": [{"matcher": "x", "hooks": [
                    {"type": "command", "command": "echo keep"
                    }
                ]}]}}),
                encoding="utf-8",
            )
            changed, _ = settings_patch.add(path, "/tmp/sapper-hook.py")
            self.assertTrue(changed)
            data = json.loads(path.read_text(encoding="utf-8"))
            commands = [
                h["command"]
                for groups in data["hooks"].values()
                for group in groups
                for h in group.get("hooks", [])
            ]
            self.assertIn("echo keep", commands)
            self.assertEqual(sum("sapper-hook.py" in command for command in commands), 2)
            settings_patch.remove(path, "/tmp/sapper-hook.py")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["hooks"]["SessionStart"][0]["hooks"][0]["command"], "echo keep")

    def test_settings_patch_rejects_non_object_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                settings_patch.add(path, "/tmp/sapper-hook.py")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                settings_patch.remove(path, "/tmp/sapper-hook.py")

    def test_settings_validation_catches_malformed_event_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"hooks": {"SessionStart": {}}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hooks.SessionStart"):
                settings_patch.validate(path)


class PeerConfigTests(unittest.TestCase):
    def test_malformed_peer_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = sapper.Store(Path(tmp) / ".sapper")
            store.init("", "peers")
            (store.root / sapper.PEERS_FILE).write_text(
                json.dumps({"version": 1, "channel": "x", "peers": {"bad name": {"target": "x"}}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(sapper.FGError, "invalid peer name"):
                sapper._load_peers(store)

    def test_setup_preflights_invalid_settings_before_creating_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".claude").mkdir()
            (project / ".claude" / "settings.json").write_text("[]", encoding="utf-8")
            old_cwd = Path.cwd()
            try:
                import os
                os.chdir(project)
                args = type("Args", (), {
                    "store": ".sapper", "goal": "should not initialize",
                    "origin": "", "agents": False, "force": False,
                })()
                with self.assertRaisesRegex(sapper.FGError, "JSON object"):
                    sapper.cmd_setup(args)
            finally:
                os.chdir(old_cwd)
            self.assertFalse((project / ".sapper" / "project.json").exists())


if __name__ == "__main__":
    unittest.main()


class TypedFactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = sapper.Store(Path(self.tmp.name) / ".sapper")
        self.store.init("test", "capture the flag")

    def tearDown(self):
        self.tmp.cleanup()

    def test_typed_fact_roundtrip_and_render(self):
        fact = self.store.add_fact(
            "nginx 1.18 at 10.0.2.15:80", "user",
            kind="service", data={"host": "10.0.2.15", "port": 80},
        )
        self.assertEqual(fact["kind"], "service")
        self.assertEqual(fact["data"]["port"], "80")
        text = sapper.render_graph_text(self.store)
        self.assertIn("<service>", text)
        self.assertIn("port=80", text)
        yaml = sapper._to_yaml_like(sapper.build_graph_view(self.store))
        self.assertIn("<service>", yaml)
        # prompt rendering scrubs braces so flag{...} cannot eat placeholders
        self.assertIn("port=80", yaml)

    def test_unknown_kind_and_bad_data_rejected(self):
        with self.assertRaises(sapper.FGError):
            self.store.add_fact("x", "user", kind="exploit")
        with self.assertRaises(sapper.FGError):
            self.store.add_fact("x", "user", kind="cred", data={"bad key!": "1"})

    def test_validate_rejects_invalid_kind_on_disk(self):
        fact = self.store.add_fact("x", "user", kind="cred")
        p = self.store.facts_dir / f"{fact['id']}.json"
        node = json.loads(p.read_text(encoding="utf-8"))
        node["kind"] = "exploit"
        p.write_text(json.dumps(node), encoding="utf-8")
        errors = sapper.validate_graph(self.store)
        self.assertTrue(any("kind" in e for e in errors))

    def test_explore_payload_carries_kind_and_data(self):
        payload = {"accepted": True, "data": {
            "description": "RCE confirmed",
            "kind": "vuln",
            "data": {"cve": "CVE-2021-41773"},
        }}
        title, desc, doc, kind, data = sapper._parse_explore_payload(payload)
        self.assertEqual(kind, "vuln")
        self.assertEqual(data, {"cve": "CVE-2021-41773"})


class FlagLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = sapper.Store(Path(self.tmp.name) / ".sapper")
        self.store.init("test", "capture the flag")

    def tearDown(self):
        self.tmp.cleanup()

    def test_flag_record_and_dedupe(self):
        fact = self.store.add_fact(
            "flag captured: flag{a}", "agent",
            kind="flag", data={"value": "flag{a}", "challenge": "c01", "status": "recorded"},
        )
        dup = sapper._find_flag_fact(self.store, "flag{a}")
        self.assertEqual(dup["id"], fact["id"])
        self.assertIsNone(sapper._find_flag_fact(self.store, "flag{b}"))

    def test_flag_status_lifecycle_and_type_guard(self):
        fact = self.store.add_fact(
            "flag captured: flag{a}", "agent",
            kind="flag", data={"value": "flag{a}", "status": "recorded"},
        )
        updated = self.store.update_flag_fact(fact["id"], "submitted", '{"ok":true}')
        self.assertEqual(updated["data"]["status"], "submitted")
        self.assertEqual(updated["data"]["result"], '{"ok":true}')
        plain = self.store.add_fact("just text", "agent")
        with self.assertRaises(sapper.FGError):
            self.store.update_flag_fact(plain["id"], "submitted", None)
        with self.assertRaises(sapper.FGError):
            self.store.update_flag_fact(fact["id"], "hacked-status", None)

    def test_status_shows_flag_ledger(self):
        self.store.add_fact(
            "flag captured: flag{z}", "agent",
            kind="flag", data={"value": "flag{z}", "challenge": "c09", "status": "submitted"},
        )
        text = sapper.render_status(self.store)
        self.assertIn("flag(s)", text)
        self.assertIn("status=submitted", text)
        self.assertIn("challenge=c09", text)


class CtfyAnswerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.store = sapper.Store(self.work / ".sapper")
        self.store.init("ctfy:challenge-1", "Solve current CTFY questions")
        self.task = self._write_task(["flag", "q2"])
        self.challenge = {
            "id": "challenge-1", "name": "Web", "description": "Solve it",
            "questions": list(self.task["questions"]),
            "my_solved_question_ids": [], "my_fully_solved": False,
        }

        test = self

        class FakeClient:
            def __init__(self):
                self.responses = []
                self.calls = []
                self.challenge_calls = []
                self.instance_calls = []
                self.instance_responses = []
                self.qa_calls = []
                self.qa_responses = []
                self.observed_statuses = []

            def submit_answer(self, instance_id, question_id, answer):
                self.calls.append((instance_id, question_id, answer))
                answers = [f for f in test.store.list_facts()
                           if f.get("kind") == "flag" and (f.get("data") or {}).get("platform") == "ctfy"]
                self.observed_statuses.append(answers[-1]["data"]["status"] if answers else None)
                response = self.responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

            def get_challenge(self, challenge_id):
                self.challenge_calls.append(challenge_id)
                return test.challenge

            def get_instance(self, instance_id):
                self.instance_calls.append(instance_id)
                response = self.instance_responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

            def submit_qa_answer(self, challenge_id, competition_id, question_id, answer):
                self.qa_calls.append((challenge_id, competition_id, question_id, answer))
                answers = [f for f in test.store.list_facts()
                           if f.get("kind") == "flag" and (f.get("data") or {}).get("platform") == "ctfy"]
                self.observed_statuses.append(answers[-1]["data"]["status"] if answers else None)
                response = self.qa_responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

        self.client = FakeClient()
        self.client.instance_responses = [self._instance()]

    def tearDown(self):
        self.tmp.cleanup()

    def _write_task(self, question_ids):
        task = {
            "schema_version": 3, "run_mode": "instance", "competition_id": "competition-1",
            "challenge": {"id": "challenge-1", "name": "Web", "description": "Solve it"},
            "questions": [{"id": qid, "prompt": f"Prompt {qid}"} for qid in question_ids],
            "gated_questions": 0, "target_instance": "instance-1",
            "target_endpoints": [{"label": "web", "type": "http", "url": "http://target:80"}],
            "attachments": [], "scope": [], "rules_of_engagement": [],
        }
        (self.work / "task.json").write_text(json.dumps(task), encoding="utf-8")
        return task

    def _use_qa(self):
        self.task.update({
            "run_mode": "qa", "target_instance": None, "target_endpoints": [],
        })
        (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")

    @staticmethod
    def _reply(correct):
        return {
            "id": "submission-1", "correct": correct, "solve_time_s": 1.0,
            "solve_rank": 1, "question_id": "flag", "challenge_fully_solved": correct,
            "attempts_remaining": None, "ranked": True, "unranked_reason": "",
        }

    def _instance(self, questions=None, solved=None, fully=False):
        questions = self.task["questions"] if questions is None else questions
        solved = {"flag"} if solved is None else set(solved)
        return {
            "id": "instance-1", "challenge_id": "challenge-1", "name": "Web",
            "category": "web", "description": "Solve it",
            "grace_kind": "solved" if fully else "",
            "questions": [
                {
                    **question, "unlocked": True,
                    "answered_correctly": question["id"] in solved,
                }
                for question in questions
            ],
        }

    def _answer(self, value="FLAG{x}", question="flag", submit=True, client_factory=None):
        module = types.SimpleNamespace(
            load_task=ctfy.load_task,
            require_task=ctfy.require_task,
            require_instance_task=ctfy.require_instance_task,
            CtfyError=ctfy.CtfyError,
            CtfyClient=types.SimpleNamespace(
                from_env=client_factory or (lambda: self.client)
            ),
        )
        args = types.SimpleNamespace(
            store=str(self.store.root), value=value, question=question,
            submit=submit, title=None, creator="test",
        )
        with mock.patch.object(sapper, "_load_ctfy_module", return_value=module, create=True), \
                mock.patch.dict("os.environ", {
                    "CTFY_WORK_DIR": str(self.work),
                    "CTFY_EVAL_CHALLENGE_ID": "different-challenge",
                    "CTFY_EVAL_TARGET_INSTANCE": "different-instance",
                }, clear=True):
            return sapper.cmd_answer(args)

    def _health(self):
        module = types.SimpleNamespace(
            load_task=ctfy.load_task,
            require_task=ctfy.require_task,
            CtfyError=ctfy.CtfyError,
            CtfyClient=types.SimpleNamespace(from_env=lambda: self.client),
        )
        args = types.SimpleNamespace(store=str(self.store.root))
        with mock.patch.object(sapper, "_load_ctfy_module", return_value=module, create=True), \
                mock.patch.dict("os.environ", {"CTFY_WORK_DIR": str(self.work)}, clear=True):
            return sapper.cmd_ctfy_health(args)

    def test_answer_records_before_submit_and_applies_verdict(self):
        self.client.responses = [self._reply(True)]
        self.assertEqual(self._answer(), 0)
        self.assertEqual(self.client.observed_statuses, ["recorded"])
        fact = [f for f in self.store.list_facts() if f.get("kind") == "flag"][0]
        self.assertEqual(fact["data"]["status"], "accepted")
        self.assertEqual(fact["data"]["question_id"], "flag")
        self.assertTrue(json.loads(fact["data"]["result"])["correct"])
        self.assertEqual(self.client.instance_calls, ["instance-1"])
        self.assertEqual(self.client.challenge_calls, [])

    def test_incorrect_answer_is_rejected(self):
        self.client.responses = [self._reply(False)]
        self.assertEqual(self._answer(), 1)
        fact = [f for f in self.store.list_facts() if f.get("kind") == "flag"][0]
        self.assertEqual(fact["data"]["status"], "rejected")
        self.assertEqual(self.client.challenge_calls, [])

    def test_transport_failure_keeps_recorded_answer(self):
        self.client.responses = [ctfy.CtfyError("network unavailable")]
        with self.assertRaisesRegex(sapper.FGError, "network unavailable"):
            self._answer()
        fact = [f for f in self.store.list_facts() if f.get("kind") == "flag"][0]
        self.assertEqual(fact["data"]["status"], "recorded")

    def test_instance_transport_failure_sets_blocker_and_preserves_answer(self):
        self.client.responses = [
            ctfy.CtfyError("network unavailable", transport=True)
        ]
        self.assertEqual(self._answer(), 0)
        fact = next(f for f in self.store.list_facts() if f.get("kind") == "flag")
        self.assertEqual(fact["data"]["status"], "recorded")
        self.assertEqual(
            self.store.load_project()["ctfy"]["blocker"]["kind"],
            "platform_unreachable",
        )

    def test_missing_instance_is_confirmed_once_and_answer_is_kept(self):
        missing = ctfy.CtfyError("instance missing", status=404, code="instance_not_found")
        self.client.responses = [missing]
        self.client.instance_responses = [missing]
        self.assertEqual(self._answer(), 0)
        self.assertEqual(self.client.calls, [("instance-1", "flag", "FLAG{x}")])
        self.assertEqual(self.client.instance_calls, ["instance-1"])
        fact = next(f for f in self.store.list_facts() if f.get("kind") == "flag")
        self.assertEqual(fact["data"]["status"], "recorded")
        blocker = self.store.load_project()["ctfy"]["blocker"]
        self.assertEqual(blocker["kind"], "instance_unavailable")

    def test_ctfy_health_uses_one_get_without_exposing_credentials(self):
        self.client.instance_responses = [{
            "id": "instance-1", "status": "running",
            "credentials": {"password": "credential-secret"},
        }]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(self._health(), 0)
        self.assertEqual(self.client.instance_calls, ["instance-1"])
        self.assertEqual(self.client.calls, [])
        self.assertIn("instance available", stdout.getvalue())
        self.assertNotIn("credential-secret", stdout.getvalue())

    def test_ctfy_health_transport_failure_sets_structured_blocker(self):
        self.client.instance_responses = [
            ctfy.CtfyError("health network unavailable", transport=True)
        ]
        self.assertEqual(self._health(), 0)
        self.assertEqual(self.client.instance_calls, ["instance-1"])
        self.assertEqual(
            self.store.load_project()["ctfy"]["blocker"]["kind"],
            "instance_health_unreachable",
        )

    def test_accepted_answer_survives_challenge_refresh_failure(self):
        self.client.responses = [self._reply(True)]
        self.client.instance_responses = [ctfy.CtfyError("refresh offline")]
        self.assertEqual(self._answer(), 0)
        fact = next(f for f in self.store.list_facts() if f.get("kind") == "flag")
        self.assertEqual(fact["data"]["status"], "accepted")
        blocker = self.store.load_project()["ctfy"]["blocker"]
        self.assertEqual(blocker["kind"], "challenge_refresh_failed")

    def test_qa_submit_applies_accepted_verdict_and_refreshes(self):
        self._use_qa()
        self.challenge["my_solved_question_ids"] = ["flag"]
        self.challenge["my_fully_solved"] = True
        self.client.qa_responses = [self._reply(True)]
        self.assertEqual(self._answer(), 0)
        self.assertEqual(self.client.observed_statuses, ["recorded"])
        self.assertEqual(self.client.qa_calls, [
            ("challenge-1", "competition-1", "flag", "FLAG{x}")
        ])
        self.assertEqual(self.client.calls, [])
        fact = next(f for f in self.store.list_facts() if f.get("kind") == "flag")
        self.assertEqual(fact["data"]["status"], "accepted")
        self.assertEqual(self.client.challenge_calls, ["challenge-1"])

    def test_qa_submit_applies_rejected_verdict(self):
        self._use_qa()
        self.client.qa_responses = [self._reply(False)]
        self.assertEqual(self._answer(), 1)
        fact = next(f for f in self.store.list_facts() if f.get("kind") == "flag")
        self.assertEqual(fact["data"]["status"], "rejected")
        self.assertEqual(self.client.challenge_calls, [])

    def test_qa_transport_failure_sets_blocker_and_preserves_answer(self):
        self._use_qa()
        self.client.qa_responses = [
            ctfy.CtfyError("network unavailable", transport=True)
        ]
        self.assertEqual(self._answer(), 0)
        fact = next(f for f in self.store.list_facts() if f.get("kind") == "flag")
        self.assertEqual(fact["data"]["status"], "recorded")
        self.assertEqual(
            self.store.load_project()["ctfy"]["blocker"]["kind"],
            "platform_unreachable",
        )

    def test_qa_http_failure_is_not_handed_off(self):
        self._use_qa()
        self.client.qa_responses = [ctfy.CtfyError("forbidden", status=403)]
        with self.assertRaisesRegex(sapper.FGError, "forbidden"):
            self._answer()
        fact = next(f for f in self.store.list_facts() if f.get("kind") == "flag")
        self.assertEqual(fact["data"]["status"], "submission_failed")

    def test_qa_submit_requires_competition_id(self):
        self._use_qa()
        self.task.pop("competition_id")
        (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")
        factory = mock.Mock(return_value=self.client)
        with self.assertRaisesRegex(sapper.FGError, "competition_id"):
            self._answer(client_factory=factory)
        factory.assert_not_called()

    def test_answer_dedupe_key_includes_question(self):
        self._answer(submit=False)
        with self.assertRaisesRegex(sapper.FGError, "already recorded"):
            self._answer(submit=False)
        self._answer(question="q2", submit=False)
        answers = [f for f in self.store.list_facts() if f.get("kind") == "flag"]
        self.assertEqual(len(answers), 2)
        self.assertEqual({f["data"]["question_id"] for f in answers}, {"flag", "q2"})

    def test_answer_is_rejected_when_question_already_accepted(self):
        self.store.add_fact(
            "accepted answer", "test", kind="flag",
            data={
                "platform": "ctfy", "challenge_id": "challenge-1",
                "instance_id": "instance-1", "question_id": "flag",
                "value": "old", "status": "accepted",
            },
        )
        with self.assertRaisesRegex(sapper.FGError, "already solved"):
            self._answer(value="new-value")
        self.assertEqual(self.client.calls, [])

    def test_answer_is_rejected_when_platform_solved_but_other_question_allowed(self):
        project = self.store.load_project()
        project["ctfy"] = {"solved_question_ids": ["flag"]}
        self.store.save_project(project)
        with self.assertRaisesRegex(sapper.FGError, "already solved"):
            self._answer(value="new-value")
        self.assertEqual(self._answer(value="other", question="q2", submit=False), 0)
        self.assertEqual(self.client.calls, [])

    def test_scoped_identity_comes_from_task_not_environment(self):
        self.client.responses = [self._reply(True)]
        self._answer()
        self.assertEqual(self.client.calls, [("instance-1", "flag", "FLAG{x}")])

    def test_newly_unlocked_question_is_valid_against_old_task_snapshot(self):
        self.store.add_fact(
            "Question q3 unlocked", "test", kind="note",
            data={"platform": "ctfy", "record_type": "question", "question_id": "q3"},
        )
        self._answer(question="q3", submit=False)
        answers = [f for f in self.store.list_facts() if f.get("kind") == "flag"]
        self.assertEqual(answers[0]["data"]["question_id"], "q3")

    def test_sync_ctfy_facts_is_idempotent_and_supports_tcp(self):
        self.task = self._write_task(["flag"])
        self.task.update({
            "target_endpoints": [{"label": "pwn", "type": "tcp", "url": "target:1337"}],
            "attachments": [{"name": "crackme", "size": 10, "path": "/ctfy/work/attachments/crackme"}],
            "scope": ["target:1337"], "rules_of_engagement": ["no brute force"],
        })
        (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")
        self.challenge["questions"] = [{"id": "q3", "prompt": "Second stage"}]
        self.challenge["my_solved_question_ids"] = ["flag"]
        sapper.sync_ctfy_facts(self.store, self.task, self.challenge)
        first_count = len(self.store.list_facts())
        sapper.sync_ctfy_facts(self.store, self.task, self.challenge)
        facts = self.store.list_facts()
        self.assertEqual(len(facts), first_count)
        service = next(f for f in facts if (f.get("data") or {}).get("record_type") == "endpoint")
        self.assertEqual(service["kind"], "service")
        self.assertEqual(service["data"]["proto"], "tcp")
        self.assertEqual(service["data"]["host"], "target")
        self.assertEqual(service["data"]["port"], "1337")
        record_types = {(f.get("data") or {}).get("record_type") for f in facts}
        self.assertTrue({"challenge", "endpoint", "attachment", "question", "scope", "rule"} <= record_types)
        self.assertTrue(any((f.get("data") or {}).get("question_id") == "q3" for f in facts))
        metadata = self.store.load_project()["ctfy"]
        self.assertEqual(metadata["run_mode"], "instance")
        self.assertEqual(metadata["solved_question_ids"], ["flag"])
        self.assertFalse(metadata["fully_solved"])

    def test_category_aliases_are_normalized(self):
        cases = {
            "crypto": "Crypto", "rev": "Reverse", "reverse": "Reverse",
            "misc": "Misc", "web": "Web", "pwn": "Pwn",
            "ai": "AI security", "ai_security": "AI security", "AI security": "AI security",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(sapper._normalize_ctfy_category(raw), expected)
        self.assertIsNone(sapper._normalize_ctfy_category("unknown"))

    def test_sync_ctfy_facts_normalizes_category_and_ignores_unknown(self):
        self.task["challenge"]["category"] = "web"
        self.task["challenge"]["tags"] = ["task-tag"]
        self.challenge["category"] = "rev"
        self.challenge["tags"] = ["reverse", "elf"]
        sapper.sync_ctfy_facts(self.store, self.task, self.challenge)
        metadata = self.store.load_project()["ctfy"]
        self.assertEqual(metadata["category"], "Reverse")
        self.assertEqual(metadata["tags"], ["reverse", "elf"])

        self.challenge["category"] = "not-a-category"
        self.challenge["tags"] = "not-a-list"
        sapper.sync_ctfy_facts(self.store, self.task, self.challenge)
        metadata = self.store.load_project()["ctfy"]
        self.assertNotIn("category", metadata)
        self.assertNotIn("tags", metadata)

    def test_sync_ctfy_facts_uses_task_category_when_refresh_omits_it(self):
        self.task["challenge"]["category"] = "crypto"
        self.task["challenge"]["tags"] = ["rsa"]
        self.challenge.pop("category", None)
        self.challenge.pop("tags", None)
        sapper.sync_ctfy_facts(self.store, self.task, self.challenge)
        metadata = self.store.load_project()["ctfy"]
        self.assertEqual(metadata["category"], "Crypto")
        self.assertEqual(metadata["tags"], ["rsa"])

    def test_correct_answer_refreshes_gated_questions(self):
        questions = self.task["questions"] + [{"id": "q3", "prompt": "Unlocked"}]
        self.client.instance_responses = [self._instance(questions=questions)]
        reply = self._reply(True)
        reply["challenge_fully_solved"] = False
        self.client.responses = [reply]
        self.assertEqual(self._answer(), 0)
        self.assertEqual(self.client.instance_calls, ["instance-1"])
        self.assertEqual(self.client.challenge_calls, [])
        self.assertTrue(any(
            (f.get("data") or {}).get("record_type") == "question"
            and (f.get("data") or {}).get("question_id") == "q3"
            for f in self.store.list_facts()
        ))
        self.assertEqual(self.store.load_project()["ctfy"]["solved_question_ids"], ["flag"])
        self.assertIsNone(self.store.load_project()["ctfy"].get("blocker"))

    def test_instance_answer_state_does_not_require_static_full_flag(self):
        self.task = self._write_task(["flag"])
        self.client.instance_responses = [self._instance(fully=False)]
        self.client.responses = [self._reply(True)]
        self.assertEqual(self._answer(), 0)
        metadata = self.store.load_project()["ctfy"]
        self.assertEqual(metadata["question_state_source"], "instance")
        self.assertEqual(metadata["solved_question_ids"], ["flag"])
        self.assertFalse(metadata["fully_solved"])
        self.assertTrue(sapper._ctfy_questions_complete(self.store))

    def test_gating_blocker_survives_sync_and_clears_when_question_unlocks(self):
        self.task = self._write_task(["flag"])
        self.task["gated_questions"] = 1
        (self.work / "task.json").write_text(json.dumps(self.task), encoding="utf-8")
        self.client.instance_responses = [self._instance()]
        self.challenge["questions"] = self.task["questions"]
        self.challenge["my_solved_question_ids"] = ["flag"]
        reply = self._reply(True)
        reply["challenge_fully_solved"] = False
        self.client.responses = [reply]
        self.assertEqual(self._answer(), 0)
        metadata = self.store.load_project()["ctfy"]
        self.assertEqual(metadata["blocker"]["kind"], "gating_pending")

        sapper.sync_ctfy_facts(self.store, self.task, self.challenge)
        self.assertEqual(
            self.store.load_project()["ctfy"]["blocker"]["kind"], "gating_pending"
        )

        self.challenge["questions"].append({"id": "q2", "prompt": "Unlocked"})
        sapper.sync_ctfy_facts(self.store, self.task, self.challenge)
        self.assertIsNone(self.store.load_project()["ctfy"].get("blocker"))

    def test_ctfy_handoff_status_is_supported(self):
        fact = self.store.add_fact(
            "candidate", "test", kind="flag",
            data={"platform": "ctfy", "value": "FLAG{x}", "status": "recorded"},
        )
        updated = self.store.update_flag_fact(fact["id"], "handed_off", None)
        self.assertEqual(updated["data"]["status"], "handed_off")

    def test_health_parser_uses_current_scoped_task(self):
        args = sapper.build_parser().parse_args([
            "--store", str(self.store.root), "ctfy-health",
        ])
        self.assertIs(args.func, sapper.cmd_ctfy_health)

    def test_answer_parser_has_no_cross_challenge_overrides(self):
        args = sapper.build_parser().parse_args([
            "--store", str(self.store.root), "answer", "FLAG{x}",
            "--question", "q2", "--submit",
        ])
        self.assertIs(args.func, sapper.cmd_answer)
        self.assertEqual(args.question, "q2")
        self.assertTrue(args.submit)
        self.assertFalse(hasattr(args, "challenge"))
        self.assertFalse(hasattr(args, "instance"))
        with self.assertRaises(SystemExit):
            sapper.build_parser().parse_args([
                "answer", "FLAG{x}", "--question", "q2", "--challenge", "other",
            ])


class DeadlineTests(unittest.TestCase):
    def test_minutes_and_iso_and_conflict(self):
        import time as _t
        a = type("A", (), {"deadline": None, "minutes": 5})()
        d = sapper.deadline_from_args(a)
        self.assertGreater(d, _t.time())
        b = type("B", (), {"deadline": "2026-10-17T13:00:00", "minutes": None})()
        self.assertIsNotNone(sapper.deadline_from_args(b))
        c = type("C", (), {"deadline": "2026-10-17T13:00:00+08:00", "minutes": 5})()
        with self.assertRaises(sapper.FGError):
            sapper.deadline_from_args(c)
        bad = type("D", (), {"deadline": "not-a-date", "minutes": None})()
        with self.assertRaises(sapper.FGError):
            sapper.deadline_from_args(bad)
        self.assertEqual(sapper.deadline_from_args(type("E", (), {"deadline": None, "minutes": None})()), None)

    def test_format_time_budget(self):
        self.assertEqual(sapper.format_time_budget(None), "")
        self.assertIn("TIME IS UP",
                      sapper.format_time_budget(0))  # 0 -> epoch 1970, always past
        self.assertIn("remaining", sapper.format_time_budget(_t.time() + 600))


import time as _t  # noqa: E402  (DeadlineTests)


class ArenaClientTests(unittest.TestCase):
    """Verify the exact TSecBench Challenges API contract (header, paths,
    params/body, error envelope)."""

    def _arena(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("_arena_test", ROOT / "lib" / "arena.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_client_contract_against_local_server(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        arena = self._arena()
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                seen.append((self.command, self.path, self.headers.get("BENCHMARK_TOKEN"), body))

            def do_GET(self):
                self._record()
                if self.path.startswith("/openapi/v1/challenges/hint"):
                    return self._reply({"unique_code": "c1", "hint": "h"})
                return self._reply([{"unique_code": "c1", "difficulty": "easy", "level": "1",
                                     "total_score": 100, "flag_count": 2, "correct_flag_count": 0,
                                     "is_completed": False, "description": "d",
                                     "container_status": "stopped", "container_addr": []}])

            def do_POST(self):
                self._record()
                if self.path.startswith("/openapi/v1/challenges/submit"):
                    return self._reply({"correct": True, "awarded": 50, "cumulative_score": 50,
                                        "correct_flag_count": 1, "total_flag_count": 2,
                                        "matched_flag_index": 0})
                if self.path.startswith("/openapi/v1/challenges/close"):
                    return self._reply({"unique_code": "c1", "closed": True})
                return self._reply({"unique_code": "c1", "container_addr": ["10.0.0.9:80"]})

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = arena.ArenaClient(f"http://127.0.0.1:{server.server_address[1]}", "tok-x")
            chs = client.list_challenges()
            self.assertEqual(chs[0]["unique_code"], "c1")
            started = client.start_challenge("c1")
            self.assertEqual(started["container_addr"], ["10.0.0.9:80"])
            self.assertEqual(client.get_hint("c1")["hint"], "h")
            res = client.submit_flag("c1", "flag{a}")
            self.assertTrue(res["correct"])
            self.assertTrue(client.close_challenge("c1")["closed"])
        finally:
            server.shutdown()

        # every call carried the BENCHMARK_TOKEN header
        self.assertTrue(all(entry[2] == "tok-x" for entry in seen), seen)
        # exact paths + param/body shapes
        submit = [e for e in seen if e[1] == "/openapi/v1/challenges/submit"][0]
        self.assertEqual(submit[3], {"unique_code": "c1", "flag": "flag{a}"})
        start = [e for e in seen if "start" in e[1]][0]
        self.assertIn("unique_code=c1", start[1])
        hint = [e for e in seen if "hint" in e[1]][0]
        self.assertIn("unique_code=c1", hint[1])
        close = [e for e in seen if "close" in e[1]][0]
        self.assertIn("unique_code=c1", close[1])

    def test_error_envelope_surfaces_code(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        arena = self._arena()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = json.dumps({"code": "invalid_state", "message": "max active", "detail": {}}).encode()
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = arena.ArenaClient(f"http://127.0.0.1:{server.server_address[1]}", "t")
            with self.assertRaises(arena.ArenaError) as ctx:
                client.start_challenge("c1")
            self.assertEqual(ctx.exception.code, "invalid_state")
            self.assertEqual(ctx.exception.status, 409)
        finally:
            server.shutdown()

    def test_config_resolution_env_and_file(self):
        arena = self._arena()
        with mock.patch.dict("os.environ", {"BENCHMARK_BASE_URL": "http://env-base",
                                            "BENCHMARK_TOKEN": "env-tok"}):
            c = arena.ArenaClient.load(None)
            self.assertEqual(c.base_url, "http://env-base")
            self.assertEqual(c.token, "env-tok")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "arena.json").write_text(json.dumps(
                {"base_url": "http://file-base", "token": "ENV:BENCHMARK_TOKEN"}), encoding="utf-8")
            with mock.patch.dict("os.environ", {"BENCHMARK_TOKEN": "env-tok"}):
                c = arena.ArenaClient.load(Path(tmp))
                self.assertEqual(c.base_url, "http://file-base")
                self.assertEqual(c.token, "env-tok")
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(arena.ArenaError):
                arena.ArenaClient.load(None)
        with self.assertRaises(arena.ArenaError):
            arena.ArenaClient("http://x", "")

    def test_rejects_unsafe_unique_code(self):
        arena = self._arena()
        client = arena.ArenaClient("http://x", "t")
        with self.assertRaises(arena.ArenaError):
            client.start_challenge("../etc/passwd")


class ArenaErrorBridgeTests(unittest.TestCase):
    """Regression for the live 409 crash: the platform answered
    "max active challenge instances reached (3)" and the raw ArenaError
    escaped the `except FGError` guard in _bench_parallel, killing the
    whole bench. Modules loaded through sapper._load_arena_module() must
    raise ArenaError catchable as FGError."""

    def test_loaded_arena_error_subclasses_fgerror(self):
        arena = sapper._load_arena_module()
        self.assertTrue(issubclass(arena.ArenaError, sapper.FGError))

    def test_409_max_active_caught_as_fgerror(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        arena = sapper._load_arena_module()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = json.dumps({
                    "code": "invalid_state",
                    "message": "max active challenge instances reached (3), "
                               "please close an existing challenge before starting a new one",
                    "detail": {},
                }).encode()
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = arena.ArenaClient(
                f"http://127.0.0.1:{server.server_address[1]}", "t")
            with self.assertRaises(sapper.FGError) as ctx:
                client.start_challenge("c1")
            self.assertIn("max active", str(ctx.exception).lower())
        finally:
            server.shutdown()


class CommanderTests(unittest.TestCase):
    def test_parse_command_payload(self):
        ok = {"accepted": True, "data": {"action": "work", "target": "c03", "minutes": 25, "note": "close"}}
        self.assertEqual(sapper._parse_command_payload(ok), ("work", "c03", 25, "close"))
        stop = {"accepted": True, "data": {"action": "stop", "reason": "all done"}}
        self.assertEqual(sapper._parse_command_payload(stop), ("stop", "", 0, "all done"))
        rej = {"accepted": False, "reason": "bad board"}
        self.assertEqual(sapper._parse_command_payload(rej)[0], "reject")
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "hack"}})
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "work", "minutes": 10}})
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "work", "target": "c1", "minutes": 0}})

    def test_board_summary_and_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("c01", "c02"):
                store = sapper.Store(root / name / ".sapper")
                store.init("t", f"flag {name}")
            sapper.Store(root / "c01" / ".sapper").add_fact(
                "flag captured: flag{a}", "agent",
                kind="flag", data={"value": "flag{a}", "challenge": "c01", "status": "submitted"})
            rows = sapper._board_summary(root)
            self.assertEqual([r["name"] for r in rows], ["c01", "c02"])
            by_name = {r["name"]: r for r in rows}
            self.assertEqual(by_name["c01"]["flags_submitted"], 1)
            self.assertEqual(by_name["c02"]["flags_submitted"], 0)
            board = sapper.render_board(rows)
            self.assertIn("c01", board)
            self.assertIn("flags(sub/rec)", board)


class FleetTests(unittest.TestCase):
    def test_fleet_pane_commands(self):
        cfg = {"session": "s", "root": "runs", "deadline": "2026-10-17T13:00:00",
               "panes": []}
        engine = sapper._sapper_engine_prefix()
        self.assertEqual(
            sapper._fleet_pane_command(cfg, {"type": "command"}),
            f"{engine} command --root runs --deadline 2026-10-17T13:00:00")
        self.assertEqual(
            sapper._fleet_pane_command(cfg, {"type": "auto:c02", "extra_args": "--verify"}),
            f"{engine} --store runs/c02/.sapper auto --deadline 2026-10-17T13:00:00 --verify")
        self.assertEqual(
            sapper._fleet_pane_command(cfg, {"type": "auto:c02"}, base_dir=Path("/w")),
            f"cd /w && {engine} --store runs/c02/.sapper auto --deadline 2026-10-17T13:00:00")
        self.assertEqual(
            sapper._fleet_pane_command({"root": "runs", "panes": []}, {"type": "shell", "command": "top"}),
            "top")
        with self.assertRaises(sapper.FGError):
            sapper._fleet_pane_command(cfg, {"type": "warp"})
        with self.assertRaises(sapper.FGError):
            sapper._fleet_pane_command(cfg, {"type": "shell"})

    def test_fleet_init_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path.cwd()
            try:
                import os
                os.chdir(tmp)
                args = type("A", (), {"action": "init", "file": "fleet.json"})()
                self.assertEqual(sapper.cmd_fleet(args), 0)
                cfg = sapper._fleet_load(Path("fleet.json"))
                self.assertTrue(cfg["panes"])
                args2 = type("A", (), {"action": "init", "file": "fleet.json"})()
                with self.assertRaises(sapper.FGError):
                    sapper.cmd_fleet(args2)
            finally:
                os.chdir(old)


class HireFireTests(unittest.TestCase):
    def test_parse_command_payload_hire_fire(self):
        hire = {"accepted": True, "data": {"action": "hire", "target": "c07", "minutes": 30}}
        self.assertEqual(sapper._parse_command_payload(hire), ("hire", "c07", 30, ""))
        fire = {"accepted": True, "data": {"action": "fire", "target": "c07", "note": "stuck"}}
        self.assertEqual(sapper._parse_command_payload(fire), ("fire", "c07", 0, "stuck"))
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "warp", "target": "c"}})
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "hire", "minutes": 10}})
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "hire", "target": "c", "minutes": -1}})

    def test_active_workers_parsing(self):
        with mock.patch.object(sapper, "_tmux") as tm:
            tm.return_value = mock.Mock(returncode=1)
            self.assertEqual(sapper._active_workers("nope"), set())
            tm.return_value = mock.Mock(returncode=0, stdout="commander\nw-c01\nw-c07\nboard\n")
            self.assertEqual(sapper._active_workers("s"), {"c01", "c07"})

    def test_board_render_worker_column(self):
        rows = [
            {"name": "c01", "status": "active", "flags_submitted": 1, "flags_recorded": 0,
             "kinds": "1 flag", "open_intents": 2, "last_activity": "t", "goal": "g", "worker": "yes"},
            {"name": "c02", "status": "active", "flags_submitted": 0, "flags_recorded": 0,
             "kinds": "-", "open_intents": 0, "last_activity": "t", "goal": "g", "worker": "-"},
        ]
        board = sapper.render_board(rows)
        self.assertIn("worker", board)
        self.assertIn("worker=yes means", board)


import os  # noqa: E402  (KeyRotationTests)


class KeyRotationTests(unittest.TestCase):
    def test_quota_matcher(self):
        self.assertTrue(sapper._is_quota_error("insufficient balance (quota exceeded)"))
        self.assertTrue(sapper._is_quota_error("Error 401: Unauthorized"))
        self.assertTrue(sapper._is_quota_error("账户欠费，请充值"))
        self.assertFalse(sapper._is_quota_error("connection timed out"))
        self.assertFalse(sapper._is_quota_error("claude exited 2: bad args"))

    def test_pool_load_and_rotate(self):
        pool_json = json.dumps([
            {"base_url": "http://a", "token": "t1", "model": "m1"},
            {"base_url": "http://b", "token": "t2", "model": "m2"},
        ])
        with mock.patch.dict("os.environ", {
            "SAPPER_KEY_POOL": pool_json, "ANTHROPIC_AUTH_TOKEN": "t1",
            "ANTHROPIC_BASE_URL": "http://a", "SAPPER_MODEL": "m1"}):
            self.assertTrue(sapper._rotate_key())
            self.assertEqual(os.environ["ANTHROPIC_AUTH_TOKEN"], "t2")
            self.assertEqual(os.environ["ANTHROPIC_BASE_URL"], "http://b")
            self.assertEqual(os.environ["SAPPER_MODEL"], "m2")
        with mock.patch.dict("os.environ", {"SAPPER_KEY_POOL": "not json"}):
            self.assertEqual(sapper._load_key_pool(), [])
        with mock.patch.dict("os.environ", {"SAPPER_KEY_POOL": ""}):
            self.assertFalse(sapper._rotate_key())
        # single-entry pool: rotate returns True but keeps the key
        with mock.patch.dict("os.environ", {
            "SAPPER_KEY_POOL": json.dumps([{"base_url": "http://a", "token": "t1"}]),
            "ANTHROPIC_AUTH_TOKEN": "t1"}):
            self.assertTrue(sapper._rotate_key())
            self.assertEqual(os.environ["ANTHROPIC_AUTH_TOKEN"], "t1")


class ReasonJsonRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = sapper.Store(Path(self.tmp.name) / ".sapper")
        self.store.init("ctfy:challenge-1", "solve")
        self.templates = ROOT / "templates" / "ctfy"

    def tearDown(self):
        self.tmp.cleanup()

    def reason(self, text):
        with mock.patch.object(sapper, "run_claude", return_value={"result": text}):
            return sapper._run_reason_raw(
                self.store, self.templates, "model", 10, True, Path(self.tmp.name),
            )

    def test_reason_repairs_exactly_one_missing_terminal_brace(self):
        kind, _ = self.reason('{"accepted": true, "data": {}')
        self.assertEqual(kind, "noop")

    def test_reason_rejects_multiple_missing_braces_and_truncated_strings(self):
        for text in ('{"accepted": true, "data": {', '{"accepted": true, "data": "x'):
            with self.subTest(text=text), self.assertRaises(sapper.FGError):
                self.reason(text)

    def test_reason_repair_still_enforces_schema(self):
        with self.assertRaises(sapper.FGError):
            self.reason('{"accepted": true, "data": {"intents": "bad"}')


class AutoLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.store = sapper.Store(self.work / ".sapper")
        self.store.init("ctfy:challenge-1", "solve")
        self.seed = self.store.add_fact("seed", "test")
        self.templates = ROOT / "templates" / "ctfy"

    def tearDown(self):
        self.tmp.cleanup()

    def add_intent(self, description="work"):
        return self.store.add_intent([self.seed["id"]], description, "test")

    def accept(self, visible=("flag",), gated=0):
        project = self.store.load_project()
        project["ctfy"] = {
            "challenge_id": "challenge-1", "run_mode": "instance",
            "solved_question_ids": [], "fully_solved": False,
            "initial_gated_questions": gated, "visible_question_ids": list(visible),
            "question_state_source": "instance",
        }
        self.store.save_project(project)
        for question_id in visible:
            self.store.add_fact(
                f"question {question_id}", "ctfy", kind="note",
                data={
                    "platform": "ctfy", "record_type": "question",
                    "challenge_id": "challenge-1", "question_id": question_id,
                },
            )
        self.store.add_fact(
            "accepted flag", "test", kind="flag",
            data={
                "platform": "ctfy", "challenge_id": "challenge-1",
                "question_id": "flag", "value": "flag-value", "status": "accepted",
            },
        )

    def args(self, **overrides):
        values = {
            "store": str(self.store.root), "templates": str(self.templates),
            "model": "model", "timeout": 10, "skip_permissions": True,
            "max_steps": 2, "max_explore": 4, "verify": False,
            "dry_run": False, "deadline": None, "minutes": None,
        }
        values.update(overrides)
        return types.SimpleNamespace(**values)

    def run_auto(self, args):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = sapper.cmd_auto(args)
        outcome = self.store.load_project().get("last_auto")
        return rc, outcome

    def conclude(self, store, templates, intent, *args, **kwargs):
        fact, _ = self.store.conclude_with_new_fact(
            intent["id"], f"completed {intent['id']}", sapper._worker_id("model"),
        )
        return fact

    def test_noop_still_drains_owned_ready_frontier(self):
        intent = self.add_intent()
        with mock.patch.object(sapper, "_run_reason_raw", return_value=("noop", "no new work")), \
                mock.patch.object(sapper, "_run_explore_raw", side_effect=self.conclude) as explore:
            rc, outcome = self.run_auto(self.args(max_steps=1))
        self.assertEqual(rc, 0)
        explore.assert_called_once()
        self.assertEqual(explore.call_args.args[2]["id"], intent["id"])
        self.assertEqual(outcome["reason"], "max_steps")

    def test_rejected_does_not_execute_pending_work(self):
        self.add_intent()
        with mock.patch.object(
            sapper, "_run_reason_raw", return_value=("rejected", "not now")
        ) as reason, mock.patch.object(sapper, "_run_explore_raw") as explore:
            rc, outcome = self.run_auto(self.args())
        self.assertEqual(rc, 0)
        self.assertEqual(reason.call_count, 2)
        explore.assert_not_called()
        self.assertEqual(outcome["reason"], "no_progress")

    def test_noop_without_ready_work_stops_as_no_progress(self):
        with mock.patch.object(sapper, "_run_reason_raw", return_value=("noop", "none")) as reason, \
                mock.patch.object(sapper, "_run_explore_raw") as explore:
            rc, outcome = self.run_auto(self.args(max_steps=3))
        self.assertEqual(rc, 0)
        self.assertEqual(reason.call_count, 2)
        explore.assert_not_called()
        self.assertEqual(outcome["reason"], "no_progress")

    def test_failed_intent_is_suppressed_while_independent_work_continues(self):
        first = self.add_intent("times out")
        second = self.add_intent("independent")
        seen = []

        def explore_effect(store, templates, intent, *args, **kwargs):
            seen.append(intent["id"])
            if intent["id"] == first["id"]:
                raise sapper.EngineTimeout("timed out")
            return self.conclude(self.store, self.templates, intent)

        with mock.patch.object(sapper, "_run_reason_raw", return_value=("intents", "ready")) as reason, \
                mock.patch.object(sapper, "_run_explore_raw", side_effect=explore_effect):
            rc, outcome = self.run_auto(self.args(max_steps=4, max_explore=3))
        self.assertEqual(rc, 1)
        self.assertEqual(seen, [first["id"], second["id"]])
        self.assertEqual(reason.call_count, 2)
        self.assertEqual(outcome["reason"], "explore_failed")
        self.assertEqual(outcome["explore_attempts"], 2)

    def test_explore_cap_exits_without_another_reason(self):
        first = self.add_intent("first")
        self.add_intent("second")
        with mock.patch.object(sapper, "_run_reason_raw", return_value=("intents", "ready")) as reason, \
                mock.patch.object(sapper, "_run_explore_raw", side_effect=self.conclude) as explore:
            rc, outcome = self.run_auto(self.args(max_steps=4, max_explore=1))
        self.assertEqual(rc, 0)
        self.assertEqual(reason.call_count, 1)
        self.assertEqual(explore.call_count, 1)
        self.assertEqual(explore.call_args.args[2]["id"], first["id"])
        self.assertEqual(outcome["reason"], "explore_cap")
        self.assertEqual(outcome["explore_attempts"], 1)

    def test_blocker_after_explore_skips_remaining_frontier(self):
        first = self.add_intent("submits and discovers blocker")
        self.add_intent("must not run")

        def explore_effect(store, templates, intent, *args, **kwargs):
            fact = self.conclude(store, templates, intent)
            project = store.load_project()
            project["ctfy"] = {"blocker": {"kind": "instance_unavailable"}}
            store.save_project(project)
            return fact

        with mock.patch.object(sapper, "_run_reason_raw", return_value=("intents", "ready")), \
                mock.patch.object(sapper, "_run_explore_raw", side_effect=explore_effect) as explore:
            rc, outcome = self.run_auto(self.args(max_steps=3, max_explore=4))
        self.assertEqual(rc, 0)
        self.assertEqual(explore.call_count, 1)
        self.assertEqual(explore.call_args.args[2]["id"], first["id"])
        self.assertEqual(outcome["reason"], "blocker")

    def test_typed_blocker_stops_remaining_frontier(self):
        first = self.add_intent("first")
        self.add_intent("second")

        def conclude_blocker(store, templates, intent, *args, **kwargs):
            fact, _ = store.conclude_with_new_fact(
                intent["id"], "confirmed DNS outage", sapper._worker_id("model"),
                kind="note", data={"ctfy_blocker": "target_dns_unavailable"},
            )
            return fact

        with mock.patch.object(sapper, "_run_reason_raw", return_value=("intents", "ready")), \
                mock.patch.object(sapper, "_run_explore_raw", side_effect=conclude_blocker) as explore:
            rc, outcome = self.run_auto(self.args(max_steps=3))
        self.assertEqual(rc, 0)
        self.assertEqual(outcome["reason"], "blocker")
        self.assertEqual(explore.call_count, 1)
        self.assertEqual(explore.call_args.args[2]["id"], first["id"])
        self.assertEqual(
            self.store.load_project()["ctfy"]["blocker"]["kind"],
            "target_dns_unavailable",
        )

    def test_instance_down_status_becomes_structured_blocker(self):
        sapper._apply_executor_blocker(self.store, {
            "description": "origin remains unavailable",
            "data": {"status": "instance_down"},
        })
        blocker = self.store.load_project()["ctfy"]["blocker"]
        self.assertEqual(blocker["kind"], "instance_unavailable")
        self.assertEqual(blocker["detail"], "origin remains unavailable")

    def test_prose_and_unknown_blockers_do_not_stop_auto(self):
        for marker in (None, "not-a-blocker"):
            with self.subTest(marker=marker):
                data = {"ctfy_blocker": marker} if marker else None
                intent = self.add_intent()
                with mock.patch.object(sapper, "_run_reason_raw", return_value=("intents", "ready")), \
                        mock.patch.object(sapper, "_run_explore_raw") as explore:
                    explore.side_effect = lambda *args, **kwargs: args[0].conclude_with_new_fact(
                        intent["id"], "prose instance_unavailable only",
                        sapper._worker_id("model"), kind="note", data=data,
                    )[0]
                    rc, outcome = self.run_auto(self.args(max_steps=1))
                self.assertEqual(rc, 0)
                self.assertEqual(outcome["reason"], "max_steps")
                self.assertNotIn("blocker", self.store.load_project().get("ctfy", {}))

    def test_blocker_from_executor_completed_malformed_response(self):
        self.add_intent()

        def done_then_malformed(*args, **kwargs):
            fact, _ = self.store.conclude_with_new_fact(
                "i001", "completed with blocker", sapper._worker_id("model"),
                kind="note", data={"ctfy_blocker": "platform_unreachable"},
            )
            return {"result": "not-json"}

        with mock.patch.object(sapper, "_run_reason_raw", return_value=("intents", "ready")), \
                mock.patch.object(sapper, "run_claude", side_effect=done_then_malformed):
            rc, outcome = self.run_auto(self.args(max_steps=1))
        self.assertEqual(rc, 0)
        self.assertEqual(outcome["reason"], "blocker")
        self.assertEqual(
            self.store.load_project()["ctfy"]["blocker"]["kind"],
            "platform_unreachable",
        )

    def test_main_injects_checkpoint_into_auto(self):
        callback = mock.Mock()
        with mock.patch.object(sapper, "cmd_auto", return_value=0) as auto:
            rc = sapper.main([
                "--store", str(self.store.root), "auto", "--model", "model",
                "--templates", str(self.templates),
            ], checkpoint=callback)
        self.assertEqual(rc, 0)
        self.assertIs(auto.call_args.args[0].checkpoint, callback)

    def test_checkpoint_runs_after_reason_and_explore_and_errors_are_nonfatal(self):
        self.add_intent()
        callback = mock.Mock(side_effect=RuntimeError("checkpoint failed"))
        args = self.args(max_steps=1)
        args.checkpoint = callback
        with mock.patch.object(sapper, "_run_reason_raw", return_value=("noop", "ready")), \
                mock.patch.object(sapper, "_run_explore_raw", side_effect=self.conclude):
            rc, outcome = self.run_auto(args)
        self.assertEqual(rc, 0)
        self.assertEqual(outcome["reason"], "max_steps")
        self.assertEqual(callback.call_count, 2)

    def test_deadline_after_reason_skips_explore_and_claim(self):
        intent = self.add_intent()
        with mock.patch.object(sapper, "deadline_from_args", return_value=1305), \
                mock.patch.object(sapper.time, "time", return_value=1000) as clock, \
                mock.patch.object(sapper, "_run_reason_raw") as reason, \
                mock.patch.object(sapper, "_run_explore_raw") as explore:
            reason.side_effect = lambda *args, **kwargs: (
                clock.configure_mock(return_value=1005) or ("intents", "ready")
            )
            rc, outcome = self.run_auto(self.args(max_steps=3))
        self.assertEqual(rc, 0)
        self.assertEqual(outcome["reason"], "deadline")
        explore.assert_not_called()
        self.assertEqual(self.store.get_intent(intent["id"])["status"], "open")

    def test_deadline_between_explores_stops_remaining_frontier(self):
        first = self.add_intent("first")
        self.add_intent("second")
        with mock.patch.object(sapper, "deadline_from_args", return_value=1305), \
                mock.patch.object(sapper.time, "time", return_value=1000) as clock, \
                mock.patch.object(sapper, "_run_reason_raw", return_value=("intents", "ready")), \
                mock.patch.object(sapper, "_run_explore_raw") as explore:
            explore.side_effect = lambda *args, **kwargs: (
                clock.configure_mock(return_value=1005)
                or self.conclude(*args, **kwargs)
            )
            rc, outcome = self.run_auto(self.args(max_steps=3))
        self.assertEqual(rc, 0)
        self.assertEqual(outcome["reason"], "deadline")
        self.assertEqual(explore.call_count, 1)
        self.assertEqual(explore.call_args.args[2]["id"], first["id"])

    def test_deadline_after_explore_skips_verify(self):
        self.add_intent()
        with mock.patch.object(sapper, "deadline_from_args", return_value=1305), \
                mock.patch.object(sapper.time, "time", return_value=1000) as clock, \
                mock.patch.object(sapper, "_run_reason_raw", return_value=("intents", "ready")), \
                mock.patch.object(sapper, "_run_explore_raw") as explore, \
                mock.patch.object(sapper, "_run_verify_raw") as verify:
            explore.side_effect = lambda *args, **kwargs: (
                clock.configure_mock(return_value=1005)
                or self.conclude(*args, **kwargs)
            )
            rc, outcome = self.run_auto(self.args(max_steps=3, verify=True))
        self.assertEqual(rc, 0)
        self.assertEqual(outcome["reason"], "deadline")
        verify.assert_not_called()

    def test_engine_timeout_after_soft_deadline_is_not_model_error(self):
        with mock.patch.object(sapper, "deadline_from_args", return_value=1305), \
                mock.patch.object(sapper.time, "time", return_value=1000) as clock, \
                mock.patch.object(sapper, "_run_reason_raw") as reason, \
                mock.patch.object(sapper.time, "sleep"):
            reason.side_effect = lambda *args, **kwargs: (
                clock.configure_mock(return_value=1006)
                or (_ for _ in ()).throw(sapper.EngineTimeout("deadline"))
            )
            rc, outcome = self.run_auto(self.args(max_steps=3))
        self.assertEqual(rc, 0)
        self.assertEqual(outcome["reason"], "deadline")
        self.assertEqual(reason.call_count, 1)

    def test_apply_reason_dedupes_only_exact_normalized_intent(self):
        self.add_intent("Inspect target")
        other = self.store.add_fact("new evidence", "test")
        data = [
            {"from": [self.seed["id"]], "description": "  inspect   TARGET "},
            {"from": [other["id"]], "description": "Inspect target"},
            {"from": [self.seed["id"]], "description": "Different work"},
        ]
        summary = sapper._apply_reason(self.store, "intents", data)
        intents = self.store.list_intents()
        self.assertEqual(len(intents), 3)
        self.assertIn("suppressed=1", summary)
        self.assertEqual(intents[-1]["description"], "Different work")

    def test_intent_with_two_persistent_failures_is_not_retried(self):
        intent = self.add_intent()
        intent["failures"] = [
            {"created_at": "old-1", "category": "timeout", "message": "one", "worker": "w1"},
            {"created_at": "old-2", "category": "timeout", "message": "two", "worker": "w2"},
        ]
        self.store.save_intent(intent)
        with mock.patch.object(sapper, "_run_reason_raw", return_value=("intents", "ready")), \
                mock.patch.object(sapper, "_run_explore_raw") as explore:
            rc, outcome = self.run_auto(self.args(max_steps=1))
        self.assertEqual(rc, 0)
        self.assertEqual(outcome["reason"], "no_progress")
        explore.assert_not_called()

    def test_model_error_is_a_structured_failure(self):
        with mock.patch.object(sapper, "_run_reason_raw", side_effect=sapper.FGError("model down")) as reason, \
                mock.patch.object(sapper.time, "sleep"):
            rc, outcome = self.run_auto(self.args(max_steps=3))
        self.assertEqual(rc, 1)
        self.assertEqual(reason.call_count, 2)
        self.assertEqual(outcome["reason"], "model_error")

    def test_completed_graph_has_completed_outcome(self):
        def complete(*args, **kwargs):
            self.store.complete_project([self.seed["id"]], "solved", "test")
            return "complete", "solved"

        with mock.patch.object(sapper, "_run_reason_raw", side_effect=complete):
            rc, outcome = self.run_auto(self.args())
        self.assertEqual(rc, 0)
        self.assertEqual(outcome["reason"], "completed")

    def test_accepted_visible_question_completes_without_static_full_flag(self):
        self.accept()
        with mock.patch.object(sapper, "_run_reason_raw") as reason:
            rc, outcome = self.run_auto(self.args())
        self.assertEqual(rc, 0)
        self.assertEqual(outcome["reason"], "completed")
        reason.assert_not_called()

    def test_unsolved_visible_question_prevents_accepted_completion(self):
        self.accept(visible=("flag", "q2"))
        with mock.patch.object(sapper, "_run_reason_raw", return_value=("noop", "none")):
            rc, outcome = self.run_auto(self.args(max_steps=3))
        self.assertEqual(rc, 0)
        self.assertEqual(outcome["reason"], "no_progress")

    def test_initially_gated_question_prevents_accepted_completion(self):
        self.accept(gated=1)
        with mock.patch.object(sapper, "_run_reason_raw", return_value=("noop", "none")):
            rc, outcome = self.run_auto(self.args(max_steps=3))
        self.assertEqual(rc, 0)
        self.assertEqual(outcome["reason"], "no_progress")

    def test_deadline_and_blocker_do_not_call_reason(self):
        project = self.store.load_project()
        project["ctfy"] = {"blocker": {"kind": "instance_unavailable"}}
        self.store.save_project(project)
        with mock.patch.object(sapper, "_run_reason_raw") as reason:
            rc, outcome = self.run_auto(self.args())
        self.assertEqual(rc, 0)
        reason.assert_not_called()
        self.assertEqual(outcome["reason"], "blocker")

        project = self.store.load_project()
        project["ctfy"].pop("blocker")
        self.store.save_project(project)
        with mock.patch.object(sapper, "deadline_from_args", return_value=0), \
                mock.patch.object(sapper, "_run_reason_raw") as reason:
            rc, outcome = self.run_auto(self.args())
        self.assertEqual(rc, 0)
        reason.assert_not_called()
        self.assertEqual(outcome["reason"], "deadline")

    def test_step_budget_and_planner_countdown_are_real(self):
        def add_evidence(*args, **kwargs):
            self.store.add_fact("new evidence", "test")
            return "intents", "evidence only"

        with mock.patch.object(sapper, "_run_reason_raw", side_effect=add_evidence):
            rc, outcome = self.run_auto(self.args(max_steps=1))
        self.assertEqual(rc, 0)
        self.assertEqual(outcome["reason"], "max_steps")

        def complete_with_countdown(*args, **kwargs):
            self.assertIn("remaining", kwargs["time_budget"])
            self.store.complete_project([self.seed["id"]], "solved", "test")
            return "complete", "solved"

        with mock.patch.object(sapper, "_run_reason_raw", side_effect=complete_with_countdown):
            rc, outcome = self.run_auto(self.args(minutes=6))
        self.assertEqual(rc, 0)
        self.assertEqual(outcome["reason"], "completed")
    def test_dispatch_timeout_and_prompt_respect_finalization_reserve(self):
        self.add_intent()
        with mock.patch.object(sapper, "deadline_from_args", return_value=1600), \
                mock.patch.object(sapper.time, "time", return_value=1000), \
                mock.patch.object(sapper, "_run_reason_raw", return_value=("intents", "ready")), \
                mock.patch.object(sapper, "_run_explore_raw", side_effect=self.conclude) as explore:
            rc, outcome = self.run_auto(self.args(timeout=600, max_steps=1))
        self.assertEqual(rc, 0)
        self.assertEqual(outcome["reason"], "max_steps")
        self.assertEqual(explore.call_args.args[4], 300)
        self.assertEqual(explore.call_args.kwargs["dispatch_budget"], "300 seconds")


class ExploreLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.store = sapper.Store(self.work / ".sapper")
        self.store.init("ctfy:challenge-1", "solve")
        seed = self.store.add_fact("seed", "test")
        self.intent = self.store.add_intent([seed["id"]], "inspect target", "test")
        self.templates = ROOT / "templates" / "ctfy"

    def tearDown(self):
        self.tmp.cleanup()

    def test_explore_timeout_releases_claim_and_bounds_failure_history(self):
        before = sapper._graph_signature(self.store)
        with mock.patch.object(
            sapper, "run_claude", side_effect=sapper.EngineTimeout("x" * 1500)
        ):
            for _ in range(5):
                with self.assertRaises(sapper.EngineTimeout):
                    sapper._run_explore_raw(
                        self.store, self.templates, self.store.get_intent(self.intent["id"]),
                        "model", 10, True, self.work, claim=True,
                    )
        refreshed = self.store.get_intent(self.intent["id"])
        self.assertEqual(refreshed["status"], "open")
        self.assertIsNone(refreshed["worker"])
        self.assertEqual(len(refreshed["failures"]), 3)
        failure = refreshed["failures"][-1]
        self.assertEqual(failure["category"], "timeout")
        self.assertEqual(failure["worker"], sapper._worker_id("model"))
        self.assertLessEqual(len(failure["message"]), 1000)
        self.assertTrue(failure["created_at"])
        self.assertEqual(sapper._graph_signature(self.store), before)

    def test_dispatch_explore_failure_also_releases_claim(self):
        args = types.SimpleNamespace(dry_run=False, claim=True)
        with mock.patch.object(sapper, "run_claude", side_effect=sapper.FGError("boom")):
            with self.assertRaisesRegex(sapper.FGError, "boom"):
                sapper._dispatch_explore(
                    self.store, self.templates, self.intent,
                    "model", 10, True, self.work, args,
                )
        refreshed = self.store.get_intent(self.intent["id"])
        self.assertEqual(refreshed["status"], "open")
        self.assertIsNone(refreshed["worker"])
        self.assertEqual(refreshed["failures"][-1]["category"], "dispatch")

    def test_failure_history_is_visible_to_planner_and_legacy_intents_are_safe(self):
        self.intent["failures"] = [{
            "created_at": "2026-11-08T00:00:00", "category": "timeout",
            "message": "read timed out", "worker": "model-1",
        }]
        self.store.save_intent(self.intent)
        prompt = sapper.render_prompt(self.store, "{graph_yaml}\n{open_intents}")
        self.assertIn("read timed out", prompt)
        self.assertIn("timeout", prompt)
        brief = json.loads(sapper.render_prompt(self.store, "{open_intents}"))[0]
        self.assertEqual(brief["status"], "open")
        self.assertIsNone(brief["worker"])
        self.assertEqual(brief["failures"][0]["message"], "read timed out")
        legacy = dict(self.intent)
        legacy.pop("failures")
        self.assertEqual(sapper._intent_brief(legacy)["failures"], [])
        yaml = sapper._to_yaml_like(sapper.build_graph_view(self.store))
        self.assertIn("status=open", yaml)
        self.assertIn("worker=null", yaml)

    def test_malformed_response_recovers_executor_completed_fact(self):
        worker = sapper._worker_id("model")
        completed = []

        def done_then_malformed(*args, **kwargs):
            fact, _ = self.store.conclude_with_new_fact(
                self.intent["id"], "executor completed", worker,
            )
            completed.append(fact)
            return {"result": "not-json"}

        with mock.patch.object(sapper, "run_claude", side_effect=done_then_malformed):
            fact = sapper._run_explore_raw(
                self.store, self.templates, self.intent,
                "model", 10, True, self.work, claim=True,
            )
        self.assertEqual(fact["id"], completed[0]["id"])
        refreshed = self.store.get_intent(self.intent["id"])
        self.assertEqual(refreshed["status"], "done")
        self.assertEqual(refreshed["worker"], worker)
        self.assertNotIn("failures", refreshed)

    def test_failure_recording_does_not_relax_worker_ownership(self):
        self.store.claim_intent(self.intent["id"], "owner")
        with self.assertRaisesRegex(sapper.FGError, "claimed by owner"):
            self.store.record_intent_failure(
                self.intent["id"], "other", "dispatch", "boom",
            )
        self.assertEqual(self.store.get_intent(self.intent["id"])["worker"], "owner")


class EngineExecutionTests(unittest.TestCase):
    def call_engine(self, *, operation="reason", retry=True, deadline=None):
        return sapper.run_claude(
            "prompt", model="model", timeout=30, skip_permissions=True,
            append_system=None, cwd=Path.cwd(), operation=operation,
            retry=retry, deadline=deadline,
        )

    def test_reason_transient_error_has_bounded_retry(self):
        with mock.patch.dict("os.environ", {"SAPPER_RETRY_MODEL": "3"}), \
                mock.patch.object(
                    sapper, "_run_engine_once",
                    side_effect=[sapper.FGError("temporary"), {"result": "ok"}],
                ) as once, mock.patch.object(sapper.time, "sleep") as sleep:
            result = self.call_engine(operation="reason")
        self.assertEqual(result, {"result": "ok"})
        self.assertEqual(once.call_count, 2)
        sleep.assert_called_once_with(10)

    def test_explore_transient_error_is_not_retried(self):
        with mock.patch.dict("os.environ", {"SAPPER_RETRY_MODEL": "3"}), \
                mock.patch.object(
                    sapper, "_run_engine_once", side_effect=sapper.FGError("temporary"),
                ) as once, mock.patch.object(sapper.time, "sleep") as sleep:
            with self.assertRaisesRegex(sapper.FGError, "temporary"):
                self.call_engine(operation="explore", retry=False)
        self.assertEqual(once.call_count, 1)
        sleep.assert_not_called()

    def test_engine_timeout_is_never_retried_for_any_operation(self):
        for operation in ("reason", "explore", "verify"):
            with self.subTest(operation=operation), \
                    mock.patch.dict("os.environ", {"SAPPER_RETRY_MODEL": "3"}), \
                    mock.patch.object(
                        sapper, "_run_engine_once",
                        side_effect=sapper.EngineTimeout("timed out"),
                    ) as once, mock.patch.object(sapper.time, "sleep") as sleep:
                with self.assertRaises(sapper.EngineTimeout):
                    self.call_engine(operation=operation)
                self.assertEqual(once.call_count, 1)
                sleep.assert_not_called()

    def test_engine_timeout_kills_process_group_and_drains(self):
        proc = mock.Mock()
        proc.pid = 4242
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["engine"], 1), ("", ""),
        ]
        with mock.patch.object(sapper.subprocess, "Popen", return_value=proc) as popen, \
                mock.patch.object(sapper.os, "killpg") as killpg:
            with self.assertRaisesRegex(sapper.EngineTimeout, "explore"):
                sapper._run_engine_once(
                    ["engine"], "prompt", 1, Path.cwd(), "engine", operation="explore",
                )
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(4242, signal.SIGKILL)
        self.assertEqual(proc.communicate.call_count, 2)

    def test_engine_timeout_preserves_redacted_partial_output(self):
        proc = mock.Mock()
        proc.pid = 4242
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(
                ["engine"], 1, output=b"partial stdout",
                stderr=b"temporary-secret\nconnection refused",
            ),
            ("", ""),
        ]
        with mock.patch.dict("os.environ", {"LLM_API_KEY": "temporary-secret"}), \
                mock.patch.object(sapper.subprocess, "Popen", return_value=proc), \
                mock.patch.object(sapper.os, "killpg"):
            with self.assertRaises(sapper.EngineTimeout) as caught:
                sapper._run_engine_once(
                    ["engine"], "prompt", 1, Path.cwd(), "engine", operation="explore",
                )
        message = str(caught.exception)
        self.assertIn("partial stdout", message)
        self.assertIn("connection refused", message)
        self.assertIn("[redacted]", message)
        self.assertNotIn("temporary-secret", message)

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_engine_timeout_kills_grandchild_in_process_group(self):
        if not Path("/proc").exists():
            self.skipTest("grandchild liveness assertion requires procfs")
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "grandchild.pid"
            script = (
                "import pathlib, subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
                "time.sleep(60)"
            )
            child_pid = None
            try:
                with self.assertRaises(sapper.EngineTimeout):
                    sapper._run_engine_once(
                        [sys.executable, "-c", script, str(pid_path)],
                        "prompt", 2, Path.cwd(), "python", operation="explore",
                    )
                self.assertTrue(pid_path.exists(), "engine did not start its grandchild")
                child_pid = int(pid_path.read_text(encoding="utf-8"))

                def alive(pid):
                    try:
                        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
                    except FileNotFoundError:
                        return False
                    return stat.rsplit(")", 1)[1].split()[0] != "Z"

                for _ in range(40):
                    if not alive(child_pid):
                        break
                    time.sleep(0.05)
                self.assertFalse(alive(child_pid), "grandchild survived engine timeout")
            finally:
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_deadline_clamps_engine_timeout(self):
        with mock.patch.object(sapper.time, "time", return_value=100.0), \
                mock.patch.object(
                    sapper, "_run_engine_once", return_value={"result": "ok"},
                ) as once:
            self.call_engine(deadline=105.0)
        self.assertEqual(once.call_args.args[2], 5.0)

    def test_retry_does_not_sleep_or_start_past_deadline(self):
        with mock.patch.dict("os.environ", {"SAPPER_RETRY_MODEL": "3"}), \
                mock.patch.object(sapper.time, "time", return_value=100.0), \
                mock.patch.object(
                    sapper, "_run_engine_once", side_effect=sapper.FGError("temporary"),
                ) as once, mock.patch.object(sapper.time, "sleep") as sleep:
            with self.assertRaises(sapper.EngineTimeout):
                self.call_engine(deadline=105.0)
        self.assertEqual(once.call_count, 1)
        sleep.assert_not_called()


class HookExecutionTests(unittest.TestCase):
    """Regression for #2: the hook script itself must run and emit valid JSON.

    SettingsPatchTests only asserted the command string was written; the hook
    binary was never executed (its fg->sapper rename import bug shipped silently
    because Claude Code swallows hook stderr).
    """

    def _run_hook(self, event, cwd, stdin=None):
        import subprocess
        proc = subprocess.run(
            [sys.executable, str(ROOT / "lib" / "sapper-hook.py"), event],
            capture_output=True, text=True, timeout=30,
            cwd=cwd, input=stdin,
        )
        return proc

    def test_sessionstart_and_userpromptsubmit_execute_and_emit_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = sapper.Store(Path(tmp) / ".sapper")
            store.init("hooktest", "verify hook executes")
            for event, stdin in (("sessionstart", None),
                                 ("userpromptsubmit", '{"prompt": "hi"}')):
                proc = self._run_hook(event, tmp, stdin)
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                payload = json.loads(proc.stdout)
                outer = payload["hookSpecificOutput"]
                self.assertEqual(outer["hookEventName"].lower(), event)
                ctx = outer.get("additionalContext") or outer.get("contextForClaude")
                self.assertIn("fact-graph", ctx)
