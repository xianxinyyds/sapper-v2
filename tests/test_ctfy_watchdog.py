import fcntl
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ctfy_watchdog", ROOT / "scripts" / "ctfy_watchdog.py")
watchdog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watchdog)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_window(self):
        self.assertTrue(watchdog.in_window("1000", "1000", "2005"))
        self.assertTrue(watchdog.in_window("1200", "1000", "2005"))
        self.assertFalse(watchdog.in_window("0959", "1000", "2005"))
        self.assertFalse(watchdog.in_window("2006", "1000", "2005"))

    def test_lock_free_detects_a_held_lock(self):
        lock = self.root / "qualifier.lock"
        self.assertTrue(watchdog.lock_is_free(lock))
        fd = os.open(lock, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(watchdog.lock_is_free(lock))
        finally:
            os.close(fd)
        self.assertTrue(watchdog.lock_is_free(lock))

    def test_heartbeat_states(self):
        campaign = self.runs / "c1"
        campaign.mkdir()
        (campaign / "campaign.json").write_text("{}", encoding="utf-8")
        now = time.time()
        self.assertEqual(
            watchdog.heartbeat_state(self.runs, now=now, stale=900), "missing")
        heartbeat = campaign / "heartbeat"
        heartbeat.write_text("1", encoding="utf-8")
        os.utime(heartbeat, (now - 10, now - 10))
        self.assertEqual(
            watchdog.heartbeat_state(self.runs, now=now, stale=900), "fresh")
        os.utime(heartbeat, (now - 1000, now - 1000))
        self.assertEqual(
            watchdog.heartbeat_state(self.runs, now=now, stale=900), "stale")

    def test_old_launch_marker_without_manifest_is_stale(self):
        now = time.time()
        marker = self.runs / ".launch-start"
        marker.write_text("1", encoding="utf-8")
        os.utime(marker, (now - 1000, now - 1000))
        self.assertEqual(
            watchdog.heartbeat_state(self.runs, now=now, stale=900), "stale")

    def test_missing_heartbeat_with_old_campaign_is_stale(self):
        campaign = self.runs / "c1"
        campaign.mkdir()
        manifest = campaign / "campaign.json"
        manifest.write_text("{}", encoding="utf-8")
        now = time.time()
        os.utime(manifest, (now - 1000, now - 1000))
        self.assertEqual(
            watchdog.heartbeat_state(self.runs, now=now, stale=900), "stale")

    def test_find_play_pids_checks_cmdline_and_cwd(self):
        proc = self.root / "proc"
        for pid, cmd, cwd in (
            ("101", "/usr/bin/python3 lib/ctfy_local.py play --competition x",
             self.project),
            ("102", "/usr/bin/python3 lib/ctfy_local.py play --competition x",
             self.root / "elsewhere"),
            ("103", "/usr/bin/python3 lib/ctfy_local.py worker", self.project),
        ):
            entry = proc / pid
            entry.mkdir(parents=True)
            (entry / "cmdline").write_bytes(cmd.replace(" ", "\0").encode())
            (entry / "cwd").symlink_to(cwd)
        self.assertEqual(
            watchdog.find_play_pids(self.project, proc_root=proc), [101])

    def test_main_starts_the_service_when_no_campaign_is_live(self):
        calls = []
        env = {
            "CTFY_PROJECT": str(self.project),
            "CTFY_RUNS_ROOT": str(self.runs),
            "CTFY_LOCK_FILE": str(self.root / "qualifier.lock"),
            "CTFY_WINDOW_START": "0000",
            "CTFY_WINDOW_END": "2359",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(watchdog.subprocess, "run",
                                  side_effect=lambda argv, **kw: calls.append(argv)):
            self.assertEqual(watchdog.main(), 0)
        self.assertIn(["systemctl", "start", "--no-block",
                       "ctfy-qualifier.service"], calls)

    def test_a_just_started_campaign_without_heartbeat_is_not_killed(self):
        campaign = self.runs / "c1"
        campaign.mkdir()
        (campaign / "campaign.json").write_text("{}", encoding="utf-8")
        lock = self.root / "qualifier.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        killed = []
        env = {
            "CTFY_PROJECT": str(self.project),
            "CTFY_RUNS_ROOT": str(self.runs),
            "CTFY_LOCK_FILE": str(lock),
            "CTFY_WINDOW_START": "0000",
            "CTFY_WINDOW_END": "2359",
        }
        try:
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(watchdog, "find_play_pids",
                                      return_value=[4242]), \
                    mock.patch.object(watchdog.os, "kill",
                                      side_effect=lambda pid, sig: killed.append(pid)):
                self.assertEqual(watchdog.main(), 0)
        finally:
            os.close(fd)
        self.assertEqual(killed, [])

    def test_stale_campaign_kills_only_verified_play_pids(self):
        campaign = self.runs / "c1"
        campaign.mkdir()
        manifest = campaign / "campaign.json"
        manifest.write_text("{}", encoding="utf-8")
        now = time.time()
        os.utime(manifest, (now - 2000, now - 2000))
        lock = self.root / "qualifier.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        killed = []
        env = {
            "CTFY_PROJECT": str(self.project),
            "CTFY_RUNS_ROOT": str(self.runs),
            "CTFY_LOCK_FILE": str(lock),
            "CTFY_WINDOW_START": "0000",
            "CTFY_WINDOW_END": "2359",
        }
        try:
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(watchdog, "find_play_pids",
                                      return_value=[4242]), \
                    mock.patch.object(watchdog, "lock_holders", return_value=[]), \
                    mock.patch.object(watchdog.time, "sleep"), \
                    mock.patch.object(watchdog.os, "kill",
                                      side_effect=lambda pid, sig: killed.append(
                                          (pid, sig))), \
                    mock.patch.object(watchdog.subprocess, "run",
                                      side_effect=lambda argv, **kw: None):
                self.assertEqual(watchdog.main(), 0)
        finally:
            os.close(fd)
        self.assertIn((4242, 15), killed)
        self.assertIn((4242, 9), killed)

    def test_stale_campaign_restarts_the_unit_once_the_lock_frees(self):
        campaign = self.runs / "c1"
        campaign.mkdir()
        manifest = campaign / "campaign.json"
        manifest.write_text("{}", encoding="utf-8")
        now = time.time()
        os.utime(manifest, (now - 2000, now - 2000))
        calls = []
        env = {
            "CTFY_PROJECT": str(self.project),
            "CTFY_RUNS_ROOT": str(self.runs),
            "CTFY_LOCK_FILE": str(self.root / "qualifier.lock"),
            "CTFY_WINDOW_START": "0000",
            "CTFY_WINDOW_END": "2359",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(watchdog, "find_play_pids", return_value=[]), \
                mock.patch.object(watchdog, "lock_is_free", return_value=False):
            self.assertEqual(watchdog.main(), 0)
        self.assertEqual(calls, [])

        calls = []
        free_values = iter([False, True])
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(watchdog, "find_play_pids",
                                  return_value=[4242]), \
                mock.patch.object(watchdog, "lock_is_free",
                                  side_effect=lambda lock: next(free_values)), \
                mock.patch.object(watchdog, "lock_holders", return_value=[]), \
                mock.patch.object(watchdog.time, "sleep"), \
                mock.patch.object(watchdog.os, "kill"), \
                mock.patch.object(watchdog.subprocess, "run",
                                  side_effect=lambda argv, **kw: calls.append(argv)):
            self.assertEqual(watchdog.main(), 0)
        self.assertIn(["systemctl", "restart", "--no-block",
                       "ctfy-qualifier.service"], calls)


if __name__ == "__main__":
    unittest.main()
