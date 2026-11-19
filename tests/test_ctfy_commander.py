import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import ctfy_commander  # noqa: E402


NOW = 1_800_000_000.0


def timestamp(value):
    return datetime.datetime.fromtimestamp(
        value, datetime.timezone.utc,
    ).isoformat().replace("+00:00", "Z")


def challenge(challenge_id="challenge-1", **overrides):
    value = {
        "id": challenge_id,
        "name": challenge_id,
        "category": "web",
        "difficulty": "easy",
        "description": "Solve the challenge",
        "solves_count": 10,
        "scoring": "scored",
        "questions": [{
            "id": "q1", "prompt": "Question prompt", "unlocked": True,
            "answered_correctly": False,
        }],
        "is_qa_only": True,
        "network_topology": "simple",
        "defence": None,
        "closed": False,
        "my_solved_question_ids": [],
        "my_fully_solved": False,
        "my_team_solved_question_ids": [],
        "my_team_fully_solved": False,
        "my_team_authored": False,
    }
    value.update(overrides)
    return value


def instance(challenge_id="challenge-1", **overrides):
    value = {
        "id": f"instance-{challenge_id}",
        "challenge_id": challenge_id,
        "competition_id": "competition-1",
        "status": "ready",
        "expires_at": NOW + 3600,
        "network_topology": "simple",
        "questions": [{
            "id": "q1", "prompt": "Instance prompt", "unlocked": True,
            "answered_correctly": False,
        }],
        "services": [],
    }
    value.update(overrides)
    return value


def campaign_state():
    return {
        "stall_minutes": 30,
        "challenges": {},
        "workers": {},
        "instances": {},
    }


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_fact(work_dir, fact_id, kind, created_at, *, creator="solver",
               source_intent=None, value="not-an-answer", status=None):
    data = {"value": value}
    if status is not None:
        data["status"] = status
    write_json(work_dir / ".sapper" / "facts" / f"{fact_id}.json", {
        "id": fact_id,
        "description": f"confirmed {kind}: {value}",
        "created_at": timestamp(created_at),
        "creator": creator,
        "source_intent": source_intent,
        "kind": kind,
        "data": data,
    })


class BoardTests(unittest.TestCase):
    def test_metadata_order_instance_worker_and_accumulated_time(self):
        hard = challenge(
            "hard", difficulty="hard", solves_count=1, scoring="dynamic",
            my_team_authored=True, my_team_solved_question_ids=["team-q"],
            is_qa_only=False,
            questions=[
                {"id": "q1", "prompt": "one", "unlocked": True},
                {"id": "q2", "prompt": "two", "unlocked": False},
            ],
        )
        easy = challenge("easy", difficulty="easy", solves_count=99)
        current = instance(
            "hard",
            questions=[
                {"id": "q1", "prompt": "one", "unlocked": True,
                 "answered_correctly": True},
                {"id": "q2", "prompt": "two", "unlocked": False,
                 "answered_correctly": False},
            ],
        )
        state = campaign_state()
        state["challenges"]["hard"] = {
            "first_seen_at": NOW - 3600,
            "last_progress_at": NOW - 1800,
            "platform_solved_question_ids": ["team-q", "q1"],
            "total_minutes": 40,
            "attempt_minutes": 2,
        }
        state["workers"]["hard"] = {
            "status": "running", "started_at": NOW - 300,
            "cancel_after_slice": False, "api_key": "worker-secret",
        }
        state["instances"][current["id"]] = {
            "challenge_id": "hard", "created_by_campaign": True,
        }

        rows = ctfy_commander.build_board(
            [hard, easy], [current], state, now=NOW,
        )

        self.assertEqual([row["id"] for row in rows], ["hard", "easy"])
        row = rows[0]
        for key in ("id", "name", "category", "difficulty", "solves_count",
                    "scoring", "my_team_authored"):
            self.assertEqual(row[key], hard[key])
        self.assertEqual(row["my_team_solved_question_ids"], ["team-q"])
        self.assertEqual(row["platform_solved_question_ids"], ["team-q", "q1"])
        self.assertEqual(row["questions"], [
            {"id": "q1", "unlocked": True, "answered_correctly": True},
            {"id": "q2", "unlocked": False, "answered_correctly": False},
        ])
        self.assertEqual(row["gated_questions"], 1)
        self.assertEqual(row["instance_id"], current["id"])
        self.assertEqual(row["instance_status"], "ready")
        self.assertEqual(row["instance_expires_at"], NOW + 3600)
        self.assertTrue(row["created_by_campaign"])
        self.assertEqual(row["worker_status"], "running")
        self.assertEqual(row["status"], "active")
        self.assertAlmostEqual(row["attempt_minutes"], 5)
        self.assertAlmostEqual(row["total_minutes"], 45)
        self.assertEqual(row["last_progress_at"], NOW - 1800)
        self.assertNotIn("priority", row)
        self.assertNotIn("api_key", row["worker"])

    def test_instance_omission_of_locked_questions_preserves_gating_count(self):
        current = challenge(
            is_qa_only=False,
            questions=[
                {"id": "q1", "prompt": "one", "unlocked": True},
                {"id": "q2", "prompt": "two", "unlocked": False},
            ],
        )
        live = instance(questions=[{
            "id": "q1", "prompt": "one", "unlocked": True,
            "answered_correctly": False,
        }])

        row = ctfy_commander.build_board(
            [current], [live], campaign_state(), now=NOW,
        )[0]

        self.assertEqual([question["id"] for question in row["questions"]], ["q1"])
        self.assertEqual(row["gated_questions"], 1)
        self.assertEqual(row["status"], "idle")

    def test_task_gates_prevent_visible_only_completion_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            write_json(work_dir / "task.json", {
                "run_mode": "instance", "gated_questions": 1,
            })
            current = challenge(
                is_qa_only=False,
                questions=[{
                    "id": "q1", "prompt": "one", "unlocked": True,
                    "answered_correctly": True,
                }],
            )
            live = instance(questions=[{
                "id": "q1", "prompt": "one", "unlocked": True,
                "answered_correctly": True,
            }])
            state = campaign_state()
            state["challenges"]["challenge-1"] = {"work_dir": str(work_dir)}

            row = ctfy_commander.build_board(
                [current], [live], state, now=NOW,
            )[0]

            self.assertEqual(row["gated_questions"], 1)
            self.assertFalse(row["my_team_fully_solved"])
            self.assertEqual(row["status"], "idle")

    def test_task_gate_clears_when_the_question_becomes_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            write_json(work_dir / "task.json", {
                "run_mode": "instance",
                "questions": [{"id": "q1", "prompt": "one"}],
                "gated_questions": 1,
            })
            answered = [
                {"id": "q1", "prompt": "one", "unlocked": True,
                 "answered_correctly": True},
                {"id": "q2", "prompt": "two", "unlocked": True,
                 "answered_correctly": True},
            ]
            current = challenge(is_qa_only=False, questions=answered)
            live = instance(questions=answered)
            state = campaign_state()
            state["challenges"]["challenge-1"] = {"work_dir": str(work_dir)}

            row = ctfy_commander.build_board(
                [current], [live], state, now=NOW,
            )[0]

            self.assertEqual(row["gated_questions"], 0)
            self.assertTrue(row["my_team_fully_solved"])
            self.assertEqual(row["status"], "solved")

    def test_solved_closed_and_unsupported_statuses(self):
        challenges = [
            challenge("solved", my_team_fully_solved=True),
            challenge("closed", closed=True),
            challenge("engagement", is_qa_only=False,
                      network_topology="engagement"),
            challenge("defence", defence={"enabled": True}),
            challenge("awd", is_qa_only=False, network_topology="awd"),
            challenge("unknown", is_qa_only=False, network_topology="mystery"),
            challenge("supported"),
        ]
        rows = ctfy_commander.build_board(
            challenges, [], campaign_state(), now=NOW,
        )
        by_id = {row["id"]: row for row in rows}

        self.assertEqual(by_id["solved"]["status"], "solved")
        self.assertIsNone(by_id["solved"]["unsupported"])
        self.assertEqual(by_id["closed"]["status"], "closed")
        self.assertIsNone(by_id["closed"]["unsupported"])
        for challenge_id in ("engagement", "defence", "awd", "unknown"):
            with self.subTest(challenge_id=challenge_id):
                self.assertEqual(by_id[challenge_id]["status"], "unsupported")
                self.assertTrue(by_id[challenge_id]["unsupported"])
        self.assertIn("engagement", by_id["engagement"]["unsupported"])
        self.assertIn("defence", by_id["defence"]["unsupported"])
        self.assertIn("AWD", by_id["awd"]["unsupported"])
        self.assertIn("unknown", by_id["unknown"]["unsupported"])
        self.assertEqual(by_id["supported"]["status"], "idle")
        self.assertIsNone(by_id["supported"]["unsupported"])

    def test_each_solver_fact_kind_is_meaningful_progress(self):
        for kind in ("vuln", "cred", "loot", "flag", "note"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                work_dir = Path(tmp)
                write_fact(work_dir, "f001", kind, NOW - 1000)
                state = campaign_state()
                state["challenges"]["challenge-1"] = {
                    "first_seen_at": NOW - 5000,
                    "last_progress_at": None,
                    "work_dir": str(work_dir),
                }
                ctfy_commander.build_board(
                    [challenge()], [], state, now=NOW,
                )
                self.assertEqual(
                    state["challenges"]["challenge-1"]["last_progress_at"],
                    NOW - 1000,
                )

    def test_sync_unfinished_duplicate_and_completed_intent_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            write_fact(work_dir, "f001", "vuln", NOW - 3000)
            for fact_id, record_type, created_at in (
                    ("f002", "challenge", NOW - 200),
                    ("f003", "question", NOW - 150),
                    ("f004", "attachment", NOW - 100)):
                write_fact(
                    work_dir, fact_id, "loot", created_at, creator="ctfy",
                )
            write_json(work_dir / ".sapper" / "intents" / "i001.json", {
                "id": "i001", "from": ["goal"], "to": None,
                "description": "unfinished", "creator": "solver",
                "status": "claimed", "worker": "solver",
                "requires_confirmation": False, "confirmed_at": None,
                "created_at": timestamp(NOW - 50), "concluded_at": None,
            })
            state = campaign_state()
            state["challenges"]["challenge-1"] = {
                "first_seen_at": NOW - 5000,
                "last_progress_at": None,
                "work_dir": str(work_dir),
            }

            ctfy_commander.build_board([challenge()], [], state, now=NOW)
            record = state["challenges"]["challenge-1"]
            self.assertEqual(record["last_progress_at"], NOW - 3000)
            ctfy_commander.build_board([challenge()], [], state, now=NOW + 60)
            self.assertEqual(record["last_progress_at"], NOW - 3000)

            write_fact(
                work_dir, "f005", "note", NOW - 1500,
                source_intent="i002",
            )
            write_json(work_dir / ".sapper" / "intents" / "i002.json", {
                "id": "i002", "from": ["f001"], "to": "f005",
                "description": "completed with a new fact", "creator": "solver",
                "status": "done", "worker": "solver",
                "requires_confirmation": False, "confirmed_at": None,
                "created_at": timestamp(NOW - 1600),
                "concluded_at": timestamp(NOW - 1500),
            })
            ctfy_commander.build_board([challenge()], [], state, now=NOW + 120)
            self.assertEqual(record["last_progress_at"], NOW - 1500)

    def test_marked_and_equivalent_duplicate_facts_are_not_new_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            write_fact(work_dir, "f001", "vuln", NOW - 3000, value="same")
            state = campaign_state()
            state["challenges"]["challenge-1"] = {
                "first_seen_at": NOW - 5000,
                "last_progress_at": NOW - 3000,
                "stalled_decisions": 3,
                "work_dir": str(work_dir),
            }
            record = state["challenges"]["challenge-1"]

            write_fact(
                work_dir, "f002", "vuln", NOW - 2000,
                value="same", status="duplicate",
            )
            row = ctfy_commander.build_board(
                [challenge()], [], state, now=NOW,
            )[0]
            self.assertEqual(row["last_progress_at"], NOW - 3000)
            self.assertEqual(row["stalled_decisions"], 4)

            write_fact(work_dir, "f003", "vuln", NOW - 1000, value="same")
            row = ctfy_commander.build_board(
                [challenge()], [], state, now=NOW + 60,
            )[0]
            self.assertEqual(row["last_progress_at"], NOW - 3000)
            self.assertEqual(row["stalled_decisions"], 5)

    def test_existing_latest_progress_never_decreases(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            write_fact(work_dir, "f001", "vuln", NOW - 3000)
            state = campaign_state()
            state["challenges"]["challenge-1"] = {
                "first_seen_at": NOW - 5000,
                "last_progress_at": NOW - 100,
                "work_dir": str(work_dir),
            }
            ctfy_commander.build_board([challenge()], [], state, now=NOW)
            self.assertEqual(
                state["challenges"]["challenge-1"]["last_progress_at"], NOW - 100,
            )

    def test_new_platform_question_and_team_transitions_use_now(self):
        current = challenge(
            my_team_solved_question_ids=["q1"],
            questions=[
                {"id": "q1", "answered_correctly": True, "unlocked": True},
                {"id": "q2", "answered_correctly": True, "unlocked": True},
            ],
        )
        state = campaign_state()
        state["challenges"]["challenge-1"] = {
            "first_seen_at": NOW - 5000,
            "last_progress_at": NOW - 4000,
            "platform_solved_question_ids": ["q1"],
        }
        ctfy_commander.build_board([current], [], state, now=NOW)
        record = state["challenges"]["challenge-1"]
        self.assertEqual(record["platform_solved_question_ids"], ["q1", "q2"])
        self.assertEqual(record["last_progress_at"], NOW)

        ctfy_commander.build_board([current], [], state, now=NOW + 60)
        self.assertEqual(record["last_progress_at"], NOW)
        current["my_team_solved_question_ids"] = ["q1", "q2", "team-q3"]
        ctfy_commander.build_board([current], [], state, now=NOW + 120)
        self.assertEqual(record["last_progress_at"], NOW + 120)
        self.assertEqual(
            record["platform_solved_question_ids"], ["q1", "q2", "team-q3"],
        )

    def test_hidden_solved_id_reappearance_is_not_a_second_progress_event(self):
        current = challenge(questions=[
            {"id": "q1", "answered_correctly": True, "unlocked": True},
            {"id": "q2", "answered_correctly": True, "unlocked": True},
        ])
        state = campaign_state()
        state["challenges"]["challenge-1"] = {
            "first_seen_at": NOW - 5000,
            "last_progress_at": NOW - 4000,
            "platform_solved_question_ids": [],
        }
        record = state["challenges"]["challenge-1"]

        ctfy_commander.build_board([current], [], state, now=NOW)
        self.assertEqual(record["last_progress_at"], NOW)
        self.assertEqual(record["platform_solved_question_ids"], ["q1", "q2"])

        current["questions"] = [
            {"id": "q1", "answered_correctly": True, "unlocked": True},
        ]
        row = ctfy_commander.build_board(
            [current], [], state, now=NOW + 60,
        )[0]
        self.assertEqual(row["platform_solved_question_ids"], ["q1"])
        self.assertEqual(record["platform_solved_question_ids"], ["q1", "q2"])
        self.assertEqual(row["last_progress_at"], NOW)

        current["questions"] = [
            {"id": "q1", "answered_correctly": True, "unlocked": True},
            {"id": "q2", "answered_correctly": True, "unlocked": True},
        ]
        row = ctfy_commander.build_board(
            [current], [], state, now=NOW + 120,
        )[0]
        self.assertEqual(row["platform_solved_question_ids"], ["q1", "q2"])
        self.assertEqual(record["platform_solved_question_ids"], ["q1", "q2"])
        self.assertEqual(row["last_progress_at"], NOW)

    def test_stall_boundaries_decisions_and_progress_reset(self):
        current = challenge()
        state = campaign_state()
        state["challenges"]["challenge-1"] = {"first_seen_at": NOW}

        row = ctfy_commander.build_board(
            [current], [], state, now=NOW + 29 * 60,
        )[0]
        self.assertFalse(row["stalled"])
        self.assertEqual(row["stalled_decisions"], 0)
        row = ctfy_commander.build_board(
            [current], [], state, now=NOW + 30 * 60,
        )[0]
        self.assertTrue(row["stalled"])
        self.assertEqual(row["stalled_decisions"], 1)
        row = ctfy_commander.build_board(
            [current], [], state, now=NOW + 31 * 60,
        )[0]
        self.assertTrue(row["stalled"])
        self.assertEqual(row["stalled_decisions"], 2)

        current["questions"][0]["answered_correctly"] = True
        row = ctfy_commander.build_board(
            [current], [], state, now=NOW + 32 * 60,
        )[0]
        self.assertFalse(row["stalled"])
        self.assertEqual(row["stalled_decisions"], 0)
        self.assertEqual(row["last_progress_at"], NOW + 32 * 60)

    def test_transport_uncertainty_is_not_accepted_until_platform_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            handoff = {"submissions": [{
                "question_id": "q1", "answer": "flag{uncertain-secret}",
                "correct": None, "status": "unresolved",
                "transport_uncertain": True, "error": "connection dropped",
            }]}
            write_json(work_dir / "handoff.json", handoff)
            write_json(work_dir / "result.json", {
                "schema_version": 3,
                "answers": {"q1": "flag{uncertain-secret}"},
                "solved": ["q1"], "fully_solved": True,
                "stop_reason": "answers_handed_off",
            })
            current = challenge(is_qa_only=False)
            live = instance()
            state = campaign_state()
            state["challenges"]["challenge-1"] = {
                "first_seen_at": NOW - 3600, "work_dir": str(work_dir),
            }

            row = ctfy_commander.build_board(
                [current], [live], state, now=NOW,
            )[0]
            self.assertEqual(row["status"], "idle")
            self.assertEqual(row["platform_solved_question_ids"], [])
            self.assertIsNone(row["last_progress_at"])
            self.assertEqual(row["result_summary"]["accepted"], 0)
            self.assertEqual(row["result_summary"]["unresolved"], 1)

            live["questions"][0]["answered_correctly"] = True
            live["grace_kind"] = "solved"
            row = ctfy_commander.build_board(
                [current], [live], state, now=NOW + 60,
            )[0]
            self.assertEqual(row["status"], "solved")
            self.assertEqual(row["platform_solved_question_ids"], ["q1"])
            self.assertEqual(row["last_progress_at"], NOW + 60)
            self.assertEqual(
                json.loads((work_dir / "handoff.json").read_text(encoding="utf-8")),
                handoff,
            )

    def test_handoff_verdict_summary_excludes_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            write_json(work_dir / "handoff.json", {"submissions": [
                {"question_id": "q1", "answer": "flag{accepted}", "correct": True},
                {"question_id": "q2", "answer": "flag{rejected}", "correct": False},
                {"question_id": "q3", "answer": "flag{failed}",
                 "correct": None, "status": "submission_failed"},
            ]})
            write_json(work_dir / "result.json", {
                "schema_version": 3, "answers": {"q1": "flag{accepted}"},
                "solved": ["q1"], "fully_solved": True,
                "stop_reason": "incomplete",
            })
            state = campaign_state()
            state["challenges"]["challenge-1"] = {"work_dir": str(work_dir)}
            row = ctfy_commander.build_board(
                [challenge()], [], state, now=NOW,
            )[0]
            self.assertEqual(row["result_summary"], {
                "stop_reason": "incomplete", "accepted": 1,
                "rejected": 1, "submission_failed": 1, "unresolved": 0,
            })
            self.assertEqual(row["status"], "idle")

    def test_non_string_answer_metadata_does_not_break_the_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            write_json(work_dir / "result.json", {
                "schema_version": 3,
                "answers": {"q1": {"unexpected": "shape"}},
                "stop_reason": "incomplete",
            })
            state = campaign_state()
            state["challenges"]["challenge-1"] = {"work_dir": str(work_dir)}

            row = ctfy_commander.build_board(
                [challenge()], [], state, now=NOW,
            )[0]

            self.assertEqual(row["result_summary"]["stop_reason"], "incomplete")
            self.assertEqual(row["status"], "idle")


class RenderTests(unittest.TestCase):
    def test_compact_json_never_contains_answers_facts_or_credentials(self):
        secrets = (
            "flag{answer-secret}", "flag{fact-secret}", "token-secret",
            "service-password", "sk-proj-fakeSecret123",
        )
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            write_fact(
                work_dir, "f001", "flag", NOW - 60,
                value="flag{fact-secret}",
            )
            write_json(work_dir / "result.json", {
                "schema_version": 3,
                "answers": {"q1": "flag{answer-secret}"},
                "solved": ["q1"], "fully_solved": True,
                "stop_reason": "answers_handed_off",
            })
            write_json(work_dir / "handoff.json", {"submissions": [{
                "question_id": "q1", "answer": "flag{answer-secret}",
                "correct": None, "status": "unresolved",
                "response": {"token": "token-secret"},
            }]})
            current = challenge(
                description="flag{answer-secret}", is_qa_only=False,
                questions=[{
                    "id": "q1", "prompt": "flag{answer-secret}",
                    "unlocked": True, "answered_correctly": False,
                }],
            )
            live = instance(services=[{
                "service_type": "ssh", "host": "target.example", "port": 22,
                "credentials": {
                    "username": "root", "password": "service-password",
                    "key_file": "sk-proj-fakeSecret123",
                },
            }])
            state = campaign_state()
            state["challenges"]["challenge-1"] = {"work_dir": str(work_dir)}
            rows = ctfy_commander.build_board(
                [current], [live], state, now=NOW,
            )
            rows[0].update({
                "answer": "flag{answer-secret}",
                "fact_value": "flag{fact-secret}",
                "token": "token-secret",
                "credentials": {"password": "service-password"},
            })

            rendered = ctfy_commander.render_ctfy_board(rows)
            self.assertIsInstance(json.loads(rendered), list)
            self.assertNotIn("\n", rendered)
            campaign_summary = json.dumps(state)
            for secret in secrets:
                with self.subTest(secret=secret):
                    self.assertNotIn(secret, rendered)
                    self.assertNotIn(secret, campaign_summary)


if __name__ == "__main__":
    unittest.main()
