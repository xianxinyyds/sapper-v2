import argparse
import contextlib
import copy
import datetime
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

import ctfy_commander  # noqa: E402
import ctfy  # noqa: E402
import ctfy_campaign  # noqa: E402
import sapper  # noqa: E402


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


def board_row(challenge_id="challenge-1", **overrides):
    value = {
        "id": challenge_id,
        "name": challenge_id,
        "difficulty": "easy",
        "solves_count": 10,
        "closed": False,
        "my_team_fully_solved": False,
        "unsupported": None,
        "status": "idle",
        "worker_status": "idle",
        "stalled": False,
        "instance_id": None,
        "instance_expires_at": None,
    }
    value.update(overrides)
    return value


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


class DecisionTests(unittest.TestCase):
    def parse(self, payload, rows=None, **overrides):
        options = {"now": NOW, "deadline": NOW + 3600}
        options.update(overrides)
        return ctfy_commander.parse_decision(
            payload, [board_row()] if rows is None else rows, **options,
        )

    def test_decision_parses_all_supported_sapper_payloads(self):
        cases = [
            ({"accepted": True, "data": {
                "action": "work", "target": "challenge-1", "minutes": 12,
                "note": "fresh lead",
            }}, ("work", "challenge-1", 12, "fresh lead")),
            ({"action": "hire", "target": "challenge-1", "minutes": 15,
              "note": "parallel lead"},
             ("hire", "challenge-1", 15, "parallel lead")),
            ({"accepted": True, "data": {
                "action": "fire", "target": "challenge-1", "note": "park it",
            }}, ("fire", "challenge-1", 0, "park it")),
            ({"action": "stop", "reason": "nothing viable"},
             ("stop", "", 0, "nothing viable")),
        ]
        for payload, expected in cases:
            with self.subTest(action=expected[0]):
                decision = self.parse(payload, rows=[] if expected[0] == "stop" else None)
                self.assertIsInstance(decision, ctfy_commander.Decision)
                self.assertEqual(
                    (decision.action, decision.target, decision.minutes, decision.note),
                    expected,
                )

    def test_decision_slice_defaults_and_clamps_to_limits(self):
        for minutes, expected in ((None, 10), (1, 5), (90, 30)):
            with self.subTest(minutes=minutes):
                data = {"action": "work", "target": "challenge-1", "note": "lead"}
                if minutes is not None:
                    data["minutes"] = minutes
                self.assertEqual(self.parse({"data": data}).minutes, expected)

        decision = self.parse(
            {"action": "work", "target": "challenge-1", "minutes": 30},
            deadline=NOW + 7 * 60 + 30,
        )
        self.assertEqual(decision.minutes, 7)

    def test_decision_rejects_less_than_five_minutes_remaining(self):
        for action in ("work", "hire"):
            with self.subTest(action=action), self.assertRaises(ctfy.CtfyError):
                self.parse(
                    {"action": action, "target": "challenge-1", "minutes": 10},
                    deadline=NOW + 299,
                )
        decision = self.parse(
            {"action": "work", "target": "challenge-1", "minutes": 10},
            deadline=NOW + 300,
        )
        self.assertEqual(decision.minutes, 5)

    def test_decision_rejects_unavailable_work_and_hire_targets(self):
        rows = [
            board_row("solved", status="solved", my_team_fully_solved=True),
            board_row("closed", status="closed", closed=True),
            board_row("unsupported", status="unsupported", unsupported="AWD"),
            board_row("active", status="active", worker_status="running"),
        ]
        for action in ("work", "hire"):
            for target in ("solved", "closed", "unsupported", "active", "unknown"):
                with self.subTest(action=action, target=target), \
                        self.assertRaises(ctfy.CtfyError):
                    self.parse(
                        {"action": action, "target": target, "minutes": 10}, rows,
                    )

    def test_decision_fire_accepts_active_or_idle_unfinished_target(self):
        for status, worker_status in (("active", "running"), ("idle", "idle")):
            with self.subTest(status=status):
                row = board_row(status=status, worker_status=worker_status)
                decision = self.parse(
                    {"action": "fire", "target": "challenge-1", "note": "release"},
                    [row],
                )
                self.assertEqual((decision.target, decision.minutes), ("challenge-1", 0))

    def test_decision_fire_rejects_non_active_non_idle_target(self):
        for status in ("failed", "pending"):
            with self.subTest(status=status), self.assertRaises(ctfy.CtfyError):
                row = board_row(status=status, worker_status="idle")
                self.parse(
                    {"action": "fire", "target": "challenge-1", "note": "release"},
                    [row],
                )

    def test_decision_instance_ttl_clamps_and_requires_viable_slice(self):
        row = board_row(
            instance_id="instance-1", instance_expires_at=NOW + 7 * 60 + 30,
        )
        decision = self.parse(
            {"action": "work", "target": "challenge-1", "minutes": 30}, [row],
        )
        self.assertEqual(decision.minutes, 7)

        row["instance_expires_at"] = NOW + 299
        with self.assertRaises(ctfy.CtfyError):
            self.parse({"action": "hire", "target": "challenge-1"}, [row])

    def test_decision_stalled_target_requires_a_concrete_note(self):
        row = board_row(stalled=True)
        with self.assertRaises(ctfy.CtfyError):
            self.parse({"action": "work", "target": "challenge-1", "note": "  "}, [row])

        note = "heap leak confirmed; validate one-byte overwrite"
        decision = self.parse(
            {"action": "work", "target": "challenge-1", "note": note}, [row],
        )
        self.assertEqual(decision.note, note)

    def test_decision_fake_model_choice_is_not_reordered_by_difficulty(self):
        rows = [
            board_row("easy", difficulty="easy", solves_count=100),
            board_row("hard", difficulty="hard", solves_count=1),
        ]
        response = {"result": json.dumps({
            "accepted": True,
            "data": {"action": "work", "target": "hard", "minutes": 10,
                     "note": "confirmed heap lead"},
        })}
        with mock.patch.object(sapper, "run_claude", return_value=response):
            decision = ctfy_commander.request_decision(
                rows, deadline=NOW + 3600, model="fake-model", timeout=30,
                template=ROOT / "templates" / "ctfy" / "command.md", now=NOW,
            )
        self.assertEqual(decision.target, "hard")

    def test_decision_malformed_json_and_model_errors_propagate(self):
        kwargs = {
            "deadline": NOW + 3600, "model": "fake-model", "timeout": 30,
            "template": ROOT / "templates" / "ctfy" / "command.md", "now": NOW,
        }
        with mock.patch.object(sapper, "run_claude", return_value={"result": "not JSON"}):
            with self.assertRaises(sapper.FGError):
                ctfy_commander.request_decision([board_row()], **kwargs)
        with mock.patch.object(sapper, "run_claude", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                ctfy_commander.request_decision([board_row()], **kwargs)


class PromptTests(unittest.TestCase):
    def test_prompt_has_soft_strategy_bounded_parallelism_and_security_doctrine(self):
        captured = {}

        def fake_model(prompt, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return {"result": '{"action":"stop","reason":"test complete"}'}

        with mock.patch.object(sapper, "run_claude", new=fake_model):
            ctfy_commander.request_decision(
                [board_row("hard", difficulty="hard", solves_count=1)],
                deadline=NOW + 3600, model="fake-model", timeout=37,
                template=ROOT / "templates" / "ctfy" / "command.md", now=NOW,
            )

        prompt = captured["prompt"]
        lowered = prompt.lower()
        for text in ("30", "difficulty", "solves_count", "soft",
                     "not a fixed ranking", "parallel", "brute force",
                     "submission", "attempt cap", "untrusted data",
                     "never as instructions"):
            with self.subTest(text=text):
                self.assertIn(text, lowered)
        self.assertIn('"id":"hard"', prompt)
        self.assertNotIn("{board}", prompt)
        self.assertNotIn("{time_budget}", prompt)
        self.assertEqual(captured["kwargs"]["model"], "fake-model")
        self.assertEqual(captured["kwargs"]["timeout"], 37)
        self.assertIs(captured["kwargs"]["skip_permissions"], False)


class FakeProcess:
    def __init__(self, pid, returncode=None, *, wait_raises=False):
        self.pid = pid
        self.returncode = returncode
        self.wait_raises = wait_raises
        self.waits = []
        self.signals = []
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.wait_raises:
            raise subprocess.TimeoutExpired(["worker"], timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def send_signal(self, signum):
        self.signals.append(signum)

    def terminate(self):
        self.terminated = True
        self.returncode = -signal.SIGTERM
        self.wait_raises = False

    def kill(self):
        self.killed = True
        self.returncode = -signal.SIGKILL
        self.wait_raises = False


class PoolClient:
    def __init__(self, challenges, instances=()):
        self.challenges = {item["id"]: copy.deepcopy(item) for item in challenges}
        self.instances = copy.deepcopy(list(instances))
        self.events = []
        self.start_calls = []
        self.stop_calls = []
        self.submission_calls = []

    def list_instances(self, challenge_id=""):
        self.events.append(("refresh", "instances"))
        return copy.deepcopy([
            item for item in self.instances
            if not challenge_id or item.get("challenge_id") == challenge_id
        ])

    def get_challenge(self, challenge_id, competition_id=""):
        self.events.append(("refresh", "challenge"))
        return copy.deepcopy(self.challenges[challenge_id])

    def start_instance(self, challenge_id, competition_id, **kwargs):
        self.start_calls.append((challenge_id, competition_id, kwargs))
        raise AssertionError("the mocked parent must not start an instance")

    def stop_instance(self, instance_id):
        self.events.append(("close", instance_id))
        self.stop_calls.append(instance_id)
        for item in self.instances:
            if item.get("id") == instance_id:
                item["status"] = "stopped"

    def submit_qa_answer(self, challenge_id, competition_id, question_id, answer):
        self.events.append(("handoff", question_id))
        self.submission_calls.append((challenge_id, question_id, answer))
        return {"correct": True}

    def submit_answer(self, instance_id, question_id, answer):
        self.events.append(("handoff", question_id))
        self.submission_calls.append((instance_id, question_id, answer))
        return {"correct": True}


class PoolTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"
        self.complete_on_launch = False
        self.prepare_on_launch = True
        self.processes = {}
        self.popen_calls = []
        self.challenge_map = {}
        self.make_pool()

    def make_pool(self, *, capacity=2, max_workers=3, instances=(), challenges=None):
        if challenges is None:
            challenges = [
                challenge(f"challenge-{number}", is_qa_only=False)
                for number in range(1, 5)
            ]
        self.challenge_map = {item["id"]: item for item in challenges}
        self.client = PoolClient(challenges, instances)
        self.store = ctfy_campaign.CampaignStore(self.root)
        self.store.save({
            "schema_version": 1,
            "competition_id": "competition-1",
            "deadline": NOW + 3600,
            "max_workers": max_workers,
            "stall_minutes": 30,
            "challenges": {},
            "workers": {},
            "instances": {},
        })
        competition = {"id": "competition-1"}
        if capacity is not None:
            competition["max_instances_per_team_effective"] = capacity
        self.run_args = argparse.Namespace(
            platform_url="https://ctfy.example", image="sapper-ctfy:test",
            engine=None, add_host=[], max_turns=12, dispatch_timeout=300,
            startup_timeout=300, ttl=None, prepare_only=False,
        )
        self.pool = ctfy_commander.WorkerPool(
            self.store, self.client, competition, self.run_args,
        )
        return self.pool

    def decision(self, target, action="hire", minutes=10):
        return ctfy_commander.Decision(action, target, minutes, "test lead")

    def _popen(self, argv, **kwargs):
        target = argv[argv.index("--challenge-id") + 1]
        attempt = Path(argv[argv.index("--attempt") + 1])
        process = FakeProcess(1000 + len(self.popen_calls),
                              0 if self.complete_on_launch else None)
        self.popen_calls.append((argv, kwargs))
        self.processes[target] = process
        current = self.challenge_map[target]
        matching = [
            item for item in self.client.instances
            if item.get("challenge_id") == target
            and item.get("status", "ready") in {"ready", "running"}
        ]
        live = matching[0] if matching else None
        if self.prepare_on_launch:
            if current.get("is_qa_only") is True:
                instance_id = None
                created = False
            else:
                if live is None:
                    live = instance(target, id=f"instance-{target}-{len(self.popen_calls)}")
                    self.client.instances.append(live)
                    created = True
                else:
                    created = False
                instance_id = live["id"]
            write_json(attempt / "prepared.json", {
                "work_dir": str(attempt), "instance_id": instance_id,
                "created_instance": created,
            })
        if self.complete_on_launch:
            result = {
                "schema_version": 3, "answers": {}, "solved": [],
                "fully_solved": False, "stop_reason": "incomplete",
            }
            write_json(attempt / "result.json", result)
            write_json(attempt / "handoff.json", {"submissions": []})
            write_json(attempt / "outcome.json", {
                "status": "completed", "work_dir": str(attempt),
                "instance_id": None if current.get("is_qa_only") is True else live["id"],
                "created_instance": False, "prepare_only": False,
                "container_exit_code": 0, "result": result, "handoff": [],
            })
        return process

    def start(self, target, *, background=True, action=None):
        with mock.patch.object(
                ctfy_commander.subprocess, "Popen", side_effect=self._popen):
            return self.pool.start(
                self.decision(target, action or ("hire" if background else "work")),
                self.challenge_map[target], background=background,
            )

    def finish(self, target, *, result=None, handoff=None, status="completed"):
        worker = self.store.load()["workers"][target]
        attempt = Path(worker["attempt_dir"])
        prepared = json.loads((attempt / "prepared.json").read_text(encoding="utf-8"))
        write_json(attempt / "task.json", {
            "schema_version": 3,
            "run_mode": "qa" if self.challenge_map[target].get("is_qa_only") is True else "instance",
            "target_instance": prepared.get("instance_id"),
            "challenge": {"id": target}, "competition_id": "competition-1",
            "questions": [{"id": "q1", "prompt": "question"}],
        })
        if result is None:
            result = {
                "schema_version": 3, "answers": {}, "solved": [],
                "fully_solved": False, "stop_reason": "incomplete",
            }
        if handoff is None:
            handoff = []
        write_json(attempt / "result.json", result)
        write_json(attempt / "handoff.json", {"submissions": handoff})
        write_json(attempt / "outcome.json", {
            "status": status, "work_dir": str(attempt),
            "instance_id": prepared.get("instance_id"),
            "created_instance": prepared.get("created_instance", False),
            "prepare_only": False, "container_exit_code": 0,
            "result": result, "handoff": handoff,
        })
        self.processes[target].returncode = 0 if status == "completed" else 1


class PoolTests(PoolTestCase):
    def test_pool_same_challenge_is_locked_and_distinct_hire_is_allowed(self):
        first = self.start("challenge-1")
        self.assertEqual(first["status"], "starting")
        self.assertIsNone(self.processes["challenge-1"].poll())
        with self.assertRaises(ctfy.CtfyError):
            self.start("challenge-1")
        second = self.start("challenge-2")
        self.assertEqual(second["status"], "starting")
        self.assertEqual(len(self.popen_calls), 2)

    def test_pool_duplicate_ready_instances_fail_before_launch(self):
        self.make_pool(instances=[instance("challenge-1", id="one"),
                                  instance("challenge-1", id="two")])
        with self.assertRaises(ctfy.CtfyError):
            self.start("challenge-1")
        self.assertEqual(self.popen_calls, [])
        self.assertEqual(self.client.start_calls, [])

    def test_pool_work_waits_and_hire_fire_preserve_active_slice(self):
        self.complete_on_launch = True
        completed = self.start("challenge-1", background=False)
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(self.processes["challenge-1"].waits)

        self.complete_on_launch = False
        self.start("challenge-2")
        fired = self.pool.fire("challenge-2")
        self.assertTrue(fired["cancel_after_slice"])
        self.assertFalse(self.processes["challenge-2"].terminated)
        self.assertFalse(self.processes["challenge-2"].killed)
        worker = self.store.load()["workers"]["challenge-2"]
        self.assertTrue(worker["cancel_after_slice"])

        idle = self.pool.fire("challenge-3")
        self.assertEqual(idle["status"], "parked")
        self.assertTrue(self.store.load()["challenges"]["challenge-3"]["parked"])

    def test_pool_reap_orders_handoff_refresh_manifest_and_owned_close(self):
        self.start("challenge-1")
        self.pool.reap()
        self.pool.fire("challenge-1")
        attempt = Path(self.store.load()["workers"]["challenge-1"]["attempt_dir"])
        result = {
            "schema_version": 3, "answers": {"q1": "flag{attempt-only-secret}"},
            "solved": [], "fully_solved": False, "stop_reason": "incomplete",
        }
        self.client.events.append(("result", "saved"))
        self.finish("challenge-1", result=result)
        original_save = self.store.save

        def recording_save(state):
            if state.get("workers", {}).get("challenge-1", {}).get("status") == "completed":
                self.client.events.append(("manifest", "completed"))
            original_save(state)

        self.store.save = recording_save
        reaped = self.pool.reap()

        self.assertEqual(reaped[0]["status"], "completed")
        names = [event[0] for event in self.client.events]
        self.assertLess(names.index("result"), names.index("handoff"))
        self.assertLess(names.index("handoff"), names.index("refresh", names.index("handoff")))
        self.assertLess(names.index("refresh", names.index("handoff")), names.index("manifest"))
        self.assertLess(names.index("manifest"), names.index("close"))
        state = self.store.load()
        record = state["challenges"]["challenge-1"]
        self.assertEqual(record["work_dir"], str(attempt))
        self.assertEqual(record["handoff_summary"]["accepted"], 1)
        self.assertGreaterEqual(record["total_minutes"], 0)
        self.assertNotIn("flag{attempt-only-secret}", json.dumps(state))
        self.assertEqual(len(self.client.stop_calls), 1)

    def test_pool_normal_reap_keeps_idle_instance(self):
        self.start("challenge-1")
        self.finish("challenge-1")
        self.pool.reap()
        self.assertEqual(self.client.stop_calls, [])

    def test_pool_start_rejects_an_expired_campaign_budget(self):
        state = self.store.load()
        state["deadline"] = NOW - 1
        self.store.save(state)
        with mock.patch.object(ctfy_commander.time, "time", return_value=NOW):
            with self.assertRaises(ctfy.CtfyError):
                self.start("challenge-1")
        self.assertEqual(self.popen_calls, [])


class CapacityTests(PoolTestCase):
    def test_capacity_two_permits_two_and_rejects_third_without_start_post(self):
        self.make_pool(capacity=2, max_workers=3)
        self.start("challenge-1")
        self.start("challenge-2")
        refreshes = len(self.client.events)
        with self.assertRaises(ctfy.CtfyError):
            self.start("challenge-3")
        self.assertEqual(len(self.popen_calls), 2)
        self.assertEqual(self.client.start_calls, [])
        self.assertEqual(len(self.client.events), refreshes + 1)

    def test_capacity_missing_falls_back_to_serial_instance_work(self):
        self.make_pool(capacity=None, max_workers=3)
        self.start("challenge-1")
        refreshes = len(self.client.events)
        with self.assertRaises(ctfy.CtfyError):
            self.start("challenge-2")
        self.assertEqual(len(self.popen_calls), 1)
        self.assertEqual(len(self.client.events), refreshes + 1)

    def test_capacity_worker_limit_is_enforced_before_launch(self):
        self.make_pool(capacity=4, max_workers=1)
        self.start("challenge-1")
        with self.assertRaises(ctfy.CtfyError):
            self.start("challenge-2")
        self.assertEqual(len(self.popen_calls), 1)

    def test_capacity_platform_rejection_refreshes_without_retry(self):
        self.start("challenge-1")
        self.finish("challenge-1", status="failed")
        attempt = Path(self.store.load()["workers"]["challenge-1"]["attempt_dir"])
        outcome = json.loads((attempt / "outcome.json").read_text(encoding="utf-8"))
        outcome["error"] = {
            "code": "max_instances_per_team_exceeded", "message": "instance capacity",
        }
        write_json(attempt / "outcome.json", outcome)
        self.pool.reap()
        refreshes = len(self.client.events)
        with self.assertRaises(ctfy.CtfyError):
            self.start("challenge-2")
        self.assertEqual(len(self.popen_calls), 1)
        self.assertEqual(len(self.client.events), refreshes + 1)


class RecoveryTests(PoolTestCase):
    def seed_recovery(self, *, with_instance=True):
        attempt = self.root / "challenge-1" / "attempt-old"
        write_json(attempt / "task.json", {
            "run_mode": "instance", "target_instance": "instance-challenge-1",
            "challenge": {"id": "challenge-1"}, "competition_id": "competition-1",
        })
        write_json(attempt / "result.json", {
            "schema_version": 3, "answers": {}, "solved": [],
            "fully_solved": False, "stop_reason": "incomplete",
        })
        write_json(attempt / "handoff.json", {"submissions": []})
        state = self.store.load()
        state["challenges"]["challenge-1"] = {"work_dir": str(attempt)}
        state["workers"]["challenge-1"] = {
            "status": "running", "pid": 4242, "attempt_dir": str(attempt),
            "started_at": NOW - 60, "minutes": 10, "cancel_after_slice": False,
        }
        self.store.save(state)
        if with_instance:
            self.client.instances = [instance("challenge-1")]
        return attempt

    def test_recovery_adopts_live_verified_worker_without_start_post(self):
        attempt = self.seed_recovery()
        write_json(attempt / "prepared.json", {
            "work_dir": str(attempt), "instance_id": "instance-challenge-1",
            "created_instance": True,
        })
        with mock.patch.object(ctfy_commander, "_pid_matches", return_value=True):
            recovered = self.pool.recover()
        self.assertIn("challenge-1", recovered["adopted"])
        self.assertEqual(self.store.load()["workers"]["challenge-1"]["status"], "running")
        self.assertFalse(self.store.load()["instances"]["instance-challenge-1"]["created_by_campaign"])
        self.assertEqual(self.client.start_calls, [])

    def test_recovery_stale_pid_uses_attempt_and_platform_without_start_post(self):
        attempt = self.seed_recovery()
        with mock.patch.object(ctfy_commander, "_pid_matches", return_value=False):
            recovered = self.pool.recover()
        self.assertIn("challenge-1", recovered["reconciled"])
        state = self.store.load()
        self.assertEqual(state["workers"]["challenge-1"]["status"], "interrupted")
        self.assertEqual(state["challenges"]["challenge-1"]["work_dir"], str(attempt))
        self.assertFalse(state["instances"]["instance-challenge-1"]["created_by_campaign"])
        self.assertEqual(self.client.start_calls, [])

    def test_pool_replacement_instance_gets_fresh_attempt_without_old_flags(self):
        old = self.root / "challenge-1" / "instance-old"
        write_json(old / "task.json", {
            "run_mode": "instance", "target_instance": "instance-old",
            "challenge": {"id": "challenge-1"}, "competition_id": "competition-1",
        })
        write_json(old / "result.json", {
            "schema_version": 3, "answers": {"q1": "flag{old-dynamic-instance}"},
        })
        write_json(old / "handoff.json", {"submissions": [{
            "question_id": "q1", "answer": "flag{old-dynamic-instance}",
            "correct": None, "status": "unresolved", "transport_uncertain": True,
        }]})
        state = self.store.load()
        state["challenges"]["challenge-1"] = {"work_dir": str(old)}
        self.store.save(state)

        self.start("challenge-1")
        new = Path(self.store.load()["challenges"]["challenge-1"]["work_dir"])
        self.assertNotEqual(new, old)
        self.assertFalse((new / "result.json").exists())
        self.assertFalse((new / "handoff.json").exists())
        self.assertNotIn("flag{old-dynamic-instance}", json.dumps(self.store.load()))
        self.assertEqual(self.client.submission_calls, [])


class CleanupTests(PoolTestCase):
    def seed_idle_instance(self, *, owned, unresolved=False):
        attempt = self.root / "challenge-1" / "attempt-idle"
        answer = "flag{unresolved-attempt-only}"
        write_json(attempt / "task.json", {
            "schema_version": 3, "run_mode": "instance",
            "target_instance": "instance-challenge-1",
            "challenge": {"id": "challenge-1"}, "competition_id": "competition-1",
            "questions": [{"id": "q1", "prompt": "question"}],
        })
        write_json(attempt / "result.json", {
            "schema_version": 3, "answers": {"q1": answer} if unresolved else {},
            "solved": [], "fully_solved": False, "stop_reason": "incomplete",
        })
        write_json(attempt / "handoff.json", {"submissions": [{
            "question_id": "q1", "answer": answer, "correct": None,
            "status": "unresolved", "transport_uncertain": True,
        }] if unresolved else []})
        state = self.store.load()
        state["challenges"]["challenge-1"] = {"work_dir": str(attempt)}
        state["instances"]["instance-challenge-1"] = {
            "challenge_id": "challenge-1", "created_by_campaign": owned,
        }
        self.store.save(state)
        self.client.instances = [instance("challenge-1")]
        return attempt

    def test_cleanup_exit_policies_close_only_owned_and_report_preserved(self):
        for owned in (True, False):
            for reason in ("stop", "deadline", "interrupt", "model_error", "infra_error"):
                with self.subTest(owned=owned, reason=reason), tempfile.TemporaryDirectory() as tmp:
                    self.root = Path(tmp) / "campaign"
                    self.make_pool()
                    self.seed_idle_instance(owned=owned)
                    result = self.pool.cleanup(reason)
                    should_close = owned and reason in {"stop", "deadline"}
                    self.assertEqual(bool(self.client.stop_calls), should_close)
                    if should_close:
                        self.assertEqual(result["closed"], ["instance-challenge-1"])
                    else:
                        self.assertEqual(self.client.stop_calls, [])
                        self.assertEqual(result["preserved"], [{
                            "id": "instance-challenge-1", "expires_at": NOW + 3600,
                        }])

    def test_cleanup_repeated_unresolved_handoff_is_not_submitted_twice(self):
        self.seed_idle_instance(owned=True, unresolved=True)
        first = self.pool.cleanup("stop")
        second = self.pool.cleanup("stop")
        self.assertEqual(self.client.submission_calls, [])
        self.assertEqual(self.client.stop_calls, ["instance-challenge-1"])
        self.assertEqual(second["closed"], [])

    def test_cleanup_stale_ready_response_does_not_repeat_close(self):
        self.seed_idle_instance(owned=True)

        def stale_stop(instance_id):
            self.client.events.append(("close", instance_id))
            self.client.stop_calls.append(instance_id)

        self.client.stop_instance = stale_stop
        self.pool.cleanup("stop")
        self.pool.cleanup("stop")
        self.assertEqual(self.client.stop_calls, ["instance-challenge-1"])

    def test_cleanup_second_interrupt_forces_bounded_worker_exit(self):
        self.start("challenge-1")
        process = self.processes["challenge-1"]
        process.wait_raises = True
        self.pool.cleanup("interrupt")
        self.assertFalse(process.terminated)
        self.pool.cleanup("interrupt")
        self.assertTrue(process.terminated)
        self.assertLessEqual(len(process.waits), 4)

    def test_cleanup_interrupt_forced_exit_preserves_owned_instance(self):
        self.start("challenge-1")
        process = self.processes["challenge-1"]
        process.wait_raises = True
        self.pool.cleanup("interrupt")
        self.assertFalse(process.terminated)
        result = self.pool.cleanup("interrupt")
        self.assertTrue(process.terminated)
        self.assertEqual(self.client.stop_calls, [])
        self.assertIn({"id": "instance-challenge-1-1", "expires_at": NOW + 3600},
                      result["preserved"])

    def test_cleanup_stop_instance_failure_records_error_and_preserves(self):
        self.seed_idle_instance(owned=True)

        def failing_stop(instance_id):
            raise ctfy.CtfyError("stop refused", transport=True)

        self.client.stop_instance = failing_stop
        result = self.pool.cleanup("stop")
        self.assertEqual(result["closed"], [])
        self.assertIn({"id": "instance-challenge-1", "expires_at": NOW + 3600},
                      result["preserved"])
        record = self.store.load()["instances"]["instance-challenge-1"]
        self.assertTrue(record["close_requested"])
        self.assertIn("error", record)

    def test_cleanup_active_interrupt_reports_instance_expiry(self):
        self.start("challenge-1")
        self.processes["challenge-1"].wait_raises = True
        result = self.pool.cleanup("interrupt")
        self.assertEqual(result["preserved"], [{
            "id": "instance-challenge-1-1", "expires_at": NOW + 3600,
        }])
        self.assertEqual(self.client.stop_calls, [])

    def test_cleanup_second_interrupt_preempts_graceful_wait_promptly(self):
        self.start("challenge-1")
        process = self.processes["challenge-1"]
        process.wait_raises = True
        pending = {"value": False}
        sleeps = []

        def interrupt_pending():
            return pending["value"]

        def fake_sleep(delay):
            sleeps.append(delay)
            pending["value"] = True

        with mock.patch.object(ctfy_commander.time, "sleep", side_effect=fake_sleep):
            result = self.pool.cleanup(
                "interrupt", interrupt_pending=interrupt_pending,
            )

        self.assertTrue(process.terminated)
        self.assertLessEqual(len(sleeps), 2)
        self.assertLessEqual(sum(sleeps), 2.0)
        self.assertEqual(self.client.stop_calls, [])
        self.assertIn({"id": "instance-challenge-1-1", "expires_at": NOW + 3600},
                      result["preserved"])

    def test_cleanup_pre_recorded_second_interrupt_forces_immediately(self):
        self.start("challenge-1")
        process = self.processes["challenge-1"]
        process.wait_raises = True
        sleeps = []

        with mock.patch.object(ctfy_commander.time, "sleep", side_effect=sleeps.append):
            result = self.pool.cleanup(
                "interrupt", interrupt_pending=lambda: True,
            )

        self.assertTrue(process.terminated)
        self.assertEqual(sleeps, [])
        self.assertEqual(self.client.stop_calls, [])
        self.assertIn({"id": "instance-challenge-1-1", "expires_at": NOW + 3600},
                      result["preserved"])


class WorkerSignalTests(PoolTestCase):
    def test_worker_signal_interrupt_cleans_docker_and_redacts_logs(self):
        attempt = self.root / "challenge-1" / "attempt-signal"
        write_json(attempt / "worker.json", {
            "platform_url": "https://ctfy.example", "competition": "competition-1",
            "challenge_id": "challenge-1", "work_dir": str(attempt),
            "image": "sapper-ctfy:test", "engine": None, "add_host": [],
            "minutes": 10, "max_turns": 12, "dispatch_timeout": 300,
            "startup_timeout": 300, "ttl": None, "prepare_only": False,
        })

        def interrupted_run(args, *, client, on_event):
            on_event("prepared", {
                "work_dir": str(attempt), "instance_id": "instance-challenge-1",
                "created_instance": True,
            })
            on_event("container", {"name": "sapper-local-test"})
            raise KeyboardInterrupt()

        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {
                "CTFY_TOKEN": "token-secret", "LLM_API_KEY": "sk-proj-fakeSecret123"}), \
                mock.patch.object(ctfy_commander.ctfy, "CtfyClient", return_value=self.client), \
                mock.patch.object(ctfy_commander.ctfy_local, "run_challenge",
                                  side_effect=interrupted_run), \
                mock.patch.object(ctfy_commander.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0)) as docker, \
                contextlib.redirect_stderr(stderr):
            returncode = ctfy_commander.worker_main([
                "--store", str(self.root), "--challenge-id", "challenge-1",
                "--attempt", str(attempt),
            ])

        self.assertEqual(returncode, 130)
        outcome = json.loads((attempt / "outcome.json").read_text(encoding="utf-8"))
        self.assertEqual(outcome["status"], "interrupted")
        docker.assert_called_once_with(
            ["docker", "rm", "-f", "sapper-local-test"],
            capture_output=True, timeout=10, check=False,
        )
        self.assertNotIn("token-secret", stderr.getvalue())
        self.assertNotIn("sk-proj-fakeSecret123", stderr.getvalue())


class SelectCompetitionTests(unittest.TestCase):
    def competition(self, competition_id="competition-1", **overrides):
        value = {
            "id": competition_id, "title": competition_id, "phase": "running",
            "is_registered": True, "challenge_count": 3,
        }
        value.update(overrides)
        return value

    def test_select_competition_auto_selects_single_candidate(self):
        selected = ctfy_commander.select_competition([self.competition()])
        self.assertEqual(selected["id"], "competition-1")

    def test_select_competition_filters_non_running_and_empty_challenges(self):
        competitions = [
            self.competition("upcoming", phase="upcoming"),
            self.competition("empty", challenge_count=0),
            self.competition("missing-count", challenge_count=None),
            self.competition("nonnumeric", challenge_count="three"),
        ]
        with self.assertRaises(ctfy.CtfyError):
            ctfy_commander.select_competition(competitions)
        with self.assertRaises(ctfy.CtfyError):
            ctfy_commander.select_competition([])

    def test_select_competition_multiple_registered_need_explicit_id(self):
        competitions = [self.competition("a"), self.competition("b")]
        with self.assertRaises(ctfy.CtfyError):
            ctfy_commander.select_competition(competitions)
        selected = ctfy_commander.select_competition(competitions, "b")
        self.assertEqual(selected["id"], "b")

    def test_select_competition_explicit_id_must_be_a_visible_candidate(self):
        competitions = [
            self.competition("a"),
            self.competition("upcoming", phase="upcoming"),
        ]
        with self.assertRaises(ctfy.CtfyError):
            ctfy_commander.select_competition(competitions, "upcoming")
        with self.assertRaises(ctfy.CtfyError):
            ctfy_commander.select_competition(competitions, "missing")

    def test_select_competition_explicit_accepts_unregistered_running(self):
        selected = ctfy_commander.select_competition(
            [self.competition("qualifier", is_registered=False)], "qualifier")
        self.assertEqual(selected["id"], "qualifier")

    def test_select_competition_explicit_id_among_unregistered_eligible(self):
        competitions = [
            self.competition("a", is_registered=False),
            self.competition("b", is_registered=False),
        ]
        selected = ctfy_commander.select_competition(competitions, "b")
        self.assertEqual(selected["id"], "b")

    def test_select_competition_auto_prefers_registered(self):
        competitions = [
            self.competition("a", is_registered=False),
            self.competition("b", is_registered=True),
        ]
        selected = ctfy_commander.select_competition(competitions)
        self.assertEqual(selected["id"], "b")

    def test_select_competition_auto_accepts_single_unregistered_eligible(self):
        selected = ctfy_commander.select_competition(
            [self.competition("qualifier", is_registered=False)])
        self.assertEqual(selected["id"], "qualifier")

    def test_select_competition_auto_multiple_unregistered_errors(self):
        competitions = [
            self.competition("a", is_registered=False),
            self.competition("b", is_registered=False),
        ]
        with self.assertRaisesRegex(ctfy.CtfyError, "a"):
            ctfy_commander.select_competition(competitions)


class FakePool:
    def __init__(self):
        self.cleanups = []
        self.starts = []
        self.fires = []
        self.reaps = 0
        self.outcome = {
            "challenge_id": "challenge-1", "status": "completed",
            "work_dir": "/tmp/attempt", "instance_id": "instance-1",
            "attempt_minutes": 10, "closed": [],
        }

    def reap(self):
        self.reaps += 1
        return []

    def start(self, decision, challenge, background=False):
        self.starts.append((decision.action, decision.target, background))
        return dict(self.outcome)

    def fire(self, target):
        self.fires.append(target)
        return {"target": target, "status": "parked", "closed": [], "preserved": []}

    def cleanup(self, reason, interrupt_pending=None):
        self.cleanups.append(reason)
        return {"reason": reason, "closed": [], "preserved": [], "workers": {}}


class LoopClient:
    def __init__(self, challenges=(), instances=()):
        self.challenges = copy.deepcopy(list(challenges))
        self.instances = copy.deepcopy(list(instances))
        self.list_challenges_calls = 0
        self.list_instances_calls = 0
        self.error = None

    def list_challenges(self, competition_id):
        self.list_challenges_calls += 1
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.challenges)

    def list_instances(self, challenge_id=""):
        self.list_instances_calls += 1
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.instances)


class CampaignLoopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"
        self.store = ctfy_campaign.CampaignStore(self.root)

    def commander(self, *, client=None, pool=None, deadline=None):
        self.store.save({
            "schema_version": 1, "competition_id": "competition-1",
            "competition_title": "Test", "created_at": NOW,
            "deadline": NOW + 3600 if deadline is None else deadline,
            "stall_minutes": 30, "max_workers": 2,
            "challenges": {}, "workers": {}, "instances": {},
        })
        return ctfy_commander.Commander(
            self.store, client, {"id": "competition-1"}, {}, pool=pool,
            model="fake-model", template=Path("unused"), poll_seconds=0,
        )

    def test_campaign_loop_refreshes_decides_records_and_cleans_up(self):
        pool = FakePool()
        client = LoopClient(challenges=[challenge()])
        commander = self.commander(client=client, pool=pool)
        decisions = iter([
            ctfy_commander.Decision("work", "challenge-1", 10, "lead; flag{test-secret}"),
            ctfy_commander.Decision("stop", "", 0, "all done"),
        ])
        with mock.patch.object(ctfy_commander, "request_decision",
                               side_effect=lambda *a, **k: next(decisions)):
            self.assertEqual(commander.run(), 0)
        self.assertEqual(pool.starts, [("work", "challenge-1", False)])
        self.assertEqual(pool.cleanups, ["stop"])
        self.assertGreaterEqual(pool.reaps, 1)
        self.assertEqual(client.list_challenges_calls, 2)
        self.assertEqual(client.list_instances_calls, 2)
        lines = (self.store.root / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertNotIn("flag{test-secret}", "\n".join(lines))
        self.assertIn("challenge-1", self.store.load()["challenges"])

    def test_campaign_loop_deadline_stops_without_deciding(self):
        pool = FakePool()
        client = LoopClient(challenges=[challenge()])
        commander = self.commander(client=client, pool=pool, deadline=0.0)
        with mock.patch.object(ctfy_commander, "request_decision") as decision_mock:
            self.assertEqual(commander.run(), 0)
        decision_mock.assert_not_called()
        self.assertEqual(pool.starts, [])
        self.assertEqual(pool.cleanups, ["deadline"])

    def test_campaign_loop_finished_board_stops_without_work(self):
        pool = FakePool()
        client = LoopClient(challenges=[
            challenge("solved", my_team_fully_solved=True),
            challenge("closed", closed=True),
            challenge("awd", is_qa_only=False, network_topology="awd"),
        ])
        commander = self.commander(client=client, pool=pool)
        with mock.patch.object(ctfy_commander, "request_decision") as decision_mock:
            self.assertEqual(commander.run(), 0)
        decision_mock.assert_not_called()
        self.assertEqual(pool.starts, [])
        self.assertEqual(pool.cleanups, ["stop"])

    def test_campaign_loop_model_failure_backs_off_without_work(self):
        pool = FakePool()
        client = LoopClient(challenges=[challenge()])
        commander = self.commander(client=client, pool=pool)
        sleeps = []
        with mock.patch.object(ctfy_commander, "request_decision",
                               side_effect=RuntimeError("offline")), \
                mock.patch.object(ctfy_commander.time, "sleep",
                                  side_effect=sleeps.append):
            self.assertEqual(commander.run(), 2)
        self.assertEqual(pool.starts, [])
        self.assertEqual(pool.cleanups, ["model_error"])
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(delay <= 60 for delay in sleeps))

    def test_campaign_loop_infra_failure_backs_off_then_infra_error(self):
        pool = FakePool()
        client = LoopClient(challenges=[challenge()])
        client.error = ctfy.CtfyError("platform down", transport=True)
        commander = self.commander(client=client, pool=pool)
        sleeps = []
        with mock.patch.object(ctfy_commander.time, "sleep",
                               side_effect=sleeps.append):
            self.assertEqual(commander.run(), 2)
        self.assertEqual(pool.starts, [])
        self.assertEqual(pool.cleanups, ["infra_error"])
        self.assertEqual(client.list_challenges_calls, 3)
        self.assertEqual(client.list_instances_calls, 0)
        self.assertEqual(len(sleeps), 2)

    def test_interrupt_records_without_cleanup_or_io(self):
        pool = FakePool()
        client = LoopClient(challenges=[challenge()])
        commander = self.commander(client=client, pool=pool)
        before = (self.store.root / "campaign.json").read_text(encoding="utf-8")
        commander.interrupt()
        commander.interrupt()
        self.assertEqual(pool.cleanups, [])
        self.assertEqual(client.list_challenges_calls, 0)
        self.assertEqual(client.list_instances_calls, 0)
        self.assertEqual(
            (self.store.root / "campaign.json").read_text(encoding="utf-8"),
            before,
        )
        self.assertEqual(commander._interrupts, 2)

    def test_campaign_loop_interrupt_during_refresh_skips_model(self):
        pool = FakePool()
        client = LoopClient(challenges=[challenge()])
        commander = self.commander(client=client, pool=pool)
        original = client.list_challenges

        def interrupting(competition_id):
            result = original(competition_id)
            commander.interrupt()
            return result

        client.list_challenges = interrupting
        with mock.patch.object(ctfy_commander, "request_decision") as decision_mock:
            self.assertEqual(commander.run(), 130)
        decision_mock.assert_not_called()
        self.assertEqual(pool.starts, [])
        self.assertEqual(pool.cleanups, ["interrupt"])

    def test_campaign_loop_interrupt_during_decision_does_not_execute(self):
        pool = FakePool()
        client = LoopClient(challenges=[challenge()])
        commander = self.commander(client=client, pool=pool)

        def deciding(*a, **k):
            commander.interrupt()
            return ctfy_commander.Decision("work", "challenge-1", 10, "lead")

        with mock.patch.object(ctfy_commander, "request_decision",
                               side_effect=deciding):
            self.assertEqual(commander.run(), 130)
        self.assertEqual(pool.starts, [])
        self.assertEqual(pool.cleanups, ["interrupt"])

    def test_campaign_loop_second_interrupt_forces_bounded_exit(self):
        pool = FakePool()
        client = LoopClient(challenges=[challenge()])
        commander = self.commander(client=client, pool=pool)
        commander.interrupt()
        commander.interrupt()
        self.assertEqual(pool.cleanups, [])
        self.assertEqual(commander.run(), 130)
        self.assertEqual(pool.cleanups, ["interrupt", "interrupt"])


if __name__ == "__main__":
    unittest.main()
