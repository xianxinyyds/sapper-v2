import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import ctfy  # noqa: E402
import ctfy_vpn  # noqa: E402


class FakeClient:
    def __init__(self, body=b"[Interface]\n"):
        self.body = body
        self.downloads = []

    def download_vpn_config(self, instance_id, destination):
        self.downloads.append((instance_id, Path(destination)))
        Path(destination).write_bytes(self.body)


def engagement_instance(**overrides):
    value = {
        "id": "d631cd0d-5202-4e13-b2a7-6c36bcf07e2e",
        "network_topology": "engagement",
        "vpn_endpoint": {"config_url": "/api/v1/instances/x/vpn-config"},
    }
    value.update(overrides)
    return value


class VpnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patch = mock.patch.object(ctfy_vpn, "_CONFIG_DIR", self.dir)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_interface_name_is_safe_and_deterministic(self):
        name = ctfy_vpn.interface_name("d631cd0d-5202-4e13-b2a7-6c36bcf07e2e")
        self.assertRegex(name, r"^ctfy-[0-9a-f]{8}$")
        self.assertEqual(
            name, ctfy_vpn.interface_name("d631cd0d-5202-4e13-b2a7-6c36bcf07e2e"))
        self.assertLessEqual(len(ctfy_vpn.interface_name("a" * 64)), 15)
        self.assertNotEqual(
            ctfy_vpn.interface_name("../../etc"),
            name)

    def test_simple_instances_need_no_vpn(self):
        self.assertIsNone(ctfy_vpn.ensure_vpn(FakeClient(), {"id": "i1"}))
        self.assertIsNone(ctfy_vpn.ensure_vpn(
            FakeClient(), {"id": "i1", "network_topology": "simple"}))

    def test_missing_vpn_endpoint_fails_closed(self):
        with self.assertRaisesRegex(ctfy.CtfyError, "vpn_endpoint"):
            ctfy_vpn.ensure_vpn(FakeClient(), engagement_instance(vpn_endpoint=None))

    def test_engagement_downloads_config_and_brings_interface_up(self):
        client = FakeClient()
        results = [
            subprocess.CompletedProcess(["wg", "show", "x"], 1, "", ""),
            subprocess.CompletedProcess(["wg-quick", "up"], 0, "", ""),
        ]
        with mock.patch.object(ctfy_vpn, "_run", side_effect=results) as run:
            name = ctfy_vpn.ensure_vpn(client, engagement_instance())
        expected = ctfy_vpn.interface_name("d631cd0d-5202-4e13-b2a7-6c36bcf07e2e")
        self.assertEqual(name, expected)
        self.assertEqual(len(client.downloads), 1)
        config = self.dir / f"{expected}.conf"
        self.assertTrue(config.is_file())
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        down = run.call_args_list[0].args[0]
        up = run.call_args_list[1].args[0]
        self.assertEqual(down[:2], ["wg", "show"])
        self.assertEqual(up, ["wg-quick", "up", expected])

    def test_existing_interface_is_reused_without_download(self):
        client = FakeClient()
        ok = subprocess.CompletedProcess(["wg"], 0, "", "")
        with mock.patch.object(ctfy_vpn, "_run", return_value=ok):
            name = ctfy_vpn.ensure_vpn(client, engagement_instance())
        self.assertEqual(
            name, ctfy_vpn.interface_name("d631cd0d-5202-4e13-b2a7-6c36bcf07e2e"))
        self.assertEqual(client.downloads, [])

    def test_failed_bringup_removes_the_config(self):
        client = FakeClient()
        results = [
            subprocess.CompletedProcess(["wg"], 1, "", ""),
            subprocess.CompletedProcess(["wg-quick"], 1, "", "boom"),
            subprocess.CompletedProcess(["wg"], 1, "", ""),
        ]
        with mock.patch.object(ctfy_vpn, "_run", side_effect=results):
            with self.assertRaisesRegex(ctfy.CtfyError, "boom"):
                ctfy_vpn.ensure_vpn(client, engagement_instance())
        self.assertEqual(list(self.dir.glob("*.conf")), [])

    def test_teardown_down_and_removes_config(self):
        config = self.dir / "ctfy-x.conf"
        config.write_text("[Interface]\n", encoding="utf-8")
        results = [
            subprocess.CompletedProcess(["wg"], 0, "", ""),
            subprocess.CompletedProcess(["wg-quick"], 0, "", ""),
        ]
        with mock.patch.object(ctfy_vpn, "_run", side_effect=results) as run:
            ctfy_vpn.teardown_vpn("ctfy-x")
        self.assertFalse(config.exists())
        self.assertEqual(run.call_args_list[1].args[0], ["wg-quick", "down", "ctfy-x"])


if __name__ == "__main__":
    unittest.main()
