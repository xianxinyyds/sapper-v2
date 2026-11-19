import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import ctfy  # noqa: E402
import ctfy_campaign  # noqa: E402


NOW = 1_767_225_600.0  # 2026-01-01T00:00:00Z


class DeadlineTests(unittest.TestCase):
    def test_resolve_deadline_uses_the_earlier_limit(self):
        self.assertEqual(
            ctfy_campaign.resolve_deadline(
                "2026-01-01T00:30:00Z", 60, now=NOW,
            ),
            1_767_227_400.0,
        )
        self.assertEqual(
            ctfy_campaign.resolve_deadline(
                "2026-01-01T02:00:00Z", 30, now=NOW,
            ),
            1_767_227_400.0,
        )

    def test_resolve_deadline_accepts_either_limit_alone(self):
        self.assertEqual(
            ctfy_campaign.resolve_deadline(
                "2026-01-01T01:00:00Z", None, now=NOW,
            ),
            1_767_229_200.0,
        )
        self.assertEqual(
            ctfy_campaign.resolve_deadline(None, 15, now=NOW),
            1_767_226_500.0,
        )

    def test_resolve_deadline_accepts_naive_ends_at_as_utc(self):
        self.assertEqual(
            ctfy_campaign.resolve_deadline(
                "2026-01-01T00:30:00", None, now=NOW,
            ),
            1_767_227_400.0,
        )
        self.assertEqual(
            ctfy_campaign.resolve_deadline(
                "2026-01-01T00:30:00", 60, now=NOW,
            ),
            1_767_227_400.0,
        )

    def test_resolve_deadline_rejects_missing_invalid_and_past_limits(self):
        cases = (
            (None, None),
            ("not-an-iso-date", None),
            ("2026-01-01T00:00:00", None),
            ("2025-12-31T23:59:59Z", None),
            ("2026-01-01T00:00:00Z", None),
            (None, 0),
            (None, -1),
        )
        for ends_at, minutes in cases:
            with self.subTest(ends_at=ends_at, minutes=minutes), \
                    self.assertRaises(ctfy.CtfyError):
                ctfy_campaign.resolve_deadline(ends_at, minutes, now=NOW)


class CampaignTests(unittest.TestCase):
    def test_make_campaign_emits_persistent_defaults_and_empty_mappings(self):
        competition = {
            "id": "competition-1",
            "title": "Example Cup",
            "ends_at": "2026-01-01T01:00:00Z",
            "credentials": {"password": "must-not-be-copied"},
        }

        state = ctfy_campaign.make_campaign(
            competition,
            deadline=1_767_229_200.0,
            stall_minutes=30,
            max_workers=2,
            now=NOW,
        )

        self.assertEqual(state, {
            "schema_version": 1,
            "competition_id": "competition-1",
            "competition_title": "Example Cup",
            "created_at": NOW,
            "deadline": 1_767_229_200.0,
            "stall_minutes": 30,
            "max_workers": 2,
            "challenges": {},
            "workers": {},
            "instances": {},
        })


class CampaignStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "competition-1"
        self.store = ctfy_campaign.CampaignStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _assert_rejected_without_persisting(
            self, method_name, record, root, filename):
        store = ctfy_campaign.CampaignStore(root)
        root.mkdir(parents=True)
        destination = root / filename
        destination.write_text("original\n", encoding="utf-8")

        with self.assertRaises(ctfy.CtfyError):
            getattr(store, method_name)(record)
        self.assertEqual(destination.read_text(encoding="utf-8"), "original\n")
        self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_save_and_reopen_preserve_the_original_deadline_atomically(self):
        self.assertEqual(self.store.load(), {})
        state = ctfy_campaign.make_campaign(
            {"id": "competition-1", "title": "Example Cup"},
            deadline=1_767_229_200.0,
            stall_minutes=30,
            max_workers=2,
            now=NOW,
        )
        self.store.save(state)
        campaign_path = self.root / "campaign.json"
        first_inode = campaign_path.stat().st_ino

        reopened = ctfy_campaign.CampaignStore(self.root)
        loaded = reopened.load()
        self.assertEqual(loaded["deadline"], 1_767_229_200.0)
        loaded["challenges"]["challenge-1"] = {"first_seen_at": NOW}
        reopened.save(loaded)

        self.assertNotEqual(campaign_path.stat().st_ino, first_inode)
        self.assertEqual(
            ctfy_campaign.CampaignStore(self.root).load()["deadline"],
            1_767_229_200.0,
        )
        self.assertEqual(list(self.root.glob(".campaign.json.*.tmp")), [])

    def test_load_rejects_non_utf8_state(self):
        self.root.mkdir(parents=True)
        (self.root / "campaign.json").write_bytes(b"\xff")

        with self.assertRaises(ctfy.CtfyError):
            self.store.load()

    def test_nonblocking_second_campaign_lock_fails(self):
        with self.store.lock():
            with self.assertRaises(ctfy.CtfyError):
                with ctfy_campaign.CampaignStore(self.root).lock(blocking=False):
                    pass

    def test_separate_challenge_ids_have_independent_locks(self):
        with self.store.challenge_lock("challenge-1"):
            with self.store.challenge_lock("challenge-2"):
                pass

    def test_challenge_lock_rejects_path_traversal(self):
        with self.assertRaises(ctfy.CtfyError):
            with self.store.challenge_lock("../x"):
                pass

    def test_decisions_append_one_json_object_per_line(self):
        records = (
            {"action": "work", "target": "challenge-1", "minutes": 10},
            {"action": "stop", "result": {"status": "done"}},
        )
        for record in records:
            self.store.append_decision(record)

        lines = (self.root / "decisions.jsonl").read_text(
            encoding="utf-8",
        ).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual([json.loads(line) for line in lines], list(records))

    def test_state_and_decisions_do_not_capture_environment_secrets(self):
        values = {
            "CTFY_TOKEN": "token-secret-value",
            "LLM_API_KEY": "llm-secret-value",
        }
        with mock.patch.dict(os.environ, values):
            state = ctfy_campaign.make_campaign(
                {"id": "competition-1", "title": "Example Cup"},
                deadline=1_767_229_200.0,
                stall_minutes=30,
                max_workers=2,
                now=NOW,
            )
            self.store.save(state)
            self.store.append_decision({"action": "stop", "note": "finished"})

        contents = [
            (self.root / "campaign.json").read_text(encoding="utf-8"),
            (self.root / "decisions.jsonl").read_text(encoding="utf-8"),
        ]
        for content in contents:
            for secret in values.values():
                self.assertNotIn(secret, content)

    def test_write_boundaries_reject_nested_sensitive_keys(self):
        payloads = {
            "token": {"token": "token-secret-value"},
            "api-key": {"LLM_API_KEY": "llm-secret-value"},
            "password": {"password": "password-secret-value"},
            "private-key": {"private_key": "private-key-secret-value"},
            "credentials": {
                "credentials": {"username": "user", "password": "secret"},
            },
            "answer": {"answer": "flag{answer-secret-value}"},
            "answers": {
                "answers": {"question-1": "flag{answers-secret-value}"},
            },
        }
        writers = (
            ("save", "campaign.json"),
            ("append_decision", "decisions.jsonl"),
        )
        for method_name, filename in writers:
            for label, payload in payloads.items():
                record = {"outer": [{"inner": payload}]}
                with self.subTest(method=method_name, kind=label):
                    self._assert_rejected_without_persisting(
                        method_name, record,
                        self.root / method_name / label, filename,
                    )

    def test_write_boundaries_reject_secret_formats_in_free_text(self):
        values = {
            "flag": "found flag{free-text-secret} in a note",
            "openai": "key sk-proj-fakeSecret123 must not persist",
            "github": "key ghp_fakeSecret123 must not persist",
            "slack": "key xoxb-fakeSecret123 must not persist",
            "aws": "key AKIAFAKESECRET123 must not persist",
            "private-key": "-----BEGIN PRIVATE KEY-----\\nsecret\\n-----END PRIVATE KEY-----",
        }
        writers = (
            ("save", "campaign.json"),
            ("append_decision", "decisions.jsonl"),
        )
        for method_name, filename in writers:
            for label, value in values.items():
                record = {"action": "work", "result": {"note": value}}
                with self.subTest(method=method_name, kind=label):
                    self._assert_rejected_without_persisting(
                        method_name, record,
                        self.root / method_name / label, filename,
                    )

    def test_write_boundaries_reject_current_environment_secret_values(self):
        values = {
            "CTFY_TOKEN": "current-ctfy-environment-secret",
            "LLM_API_KEY": "current-llm-environment-secret",
        }
        writers = (
            ("save", "campaign.json"),
            ("append_decision", "decisions.jsonl"),
        )
        with mock.patch.dict(os.environ, values):
            for method_name, filename in writers:
                for label, secret in values.items():
                    record = {"note": f"prefix {secret} suffix"}
                    with self.subTest(method=method_name, kind=label):
                        self._assert_rejected_without_persisting(
                            method_name, record,
                            self.root / method_name / label, filename,
                        )


if __name__ == "__main__":
    unittest.main()
