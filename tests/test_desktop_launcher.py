"""Desktop startup receipts use synthetic children and an isolated install."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


loader = importlib.machinery.SourceFileLoader(
    "desktop_receipt_launcher", str(Path(__file__).resolve().parents[1] / "launcher.pyw"))
spec = importlib.util.spec_from_loader(loader.name, loader)
launcher = importlib.util.module_from_spec(spec)
loader.exec_module(launcher)


class DesktopStartupReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aihub-startup-receipt-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.request_id = "a" * 32
        self.path = self.data / "desktop" / f"startup-{self.request_id}.json"
        for name, value in (("ROOT", self.root), ("DATA", self.data)):
            patch = mock.patch.object(launcher, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_no_child_failure_is_proven_absent(self):
        launcher.write_desktop_result(self.request_id, "failed", {})
        record = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertFalse(record["child_alive"])
        self.assertEqual(record["install_root"], str(self.root.resolve()))
        self.assertEqual(record["request_id"], self.request_id)
        self.assertNotIn("token", record)

    def test_live_late_child_is_not_reported_absent(self):
        child = mock.Mock()
        child.poll.return_value = None
        launcher.write_desktop_result(self.request_id, "failed", {"process": child})
        self.assertTrue(json.loads(self.path.read_text())["child_alive"])

    def test_exited_child_is_reported_absent(self):
        child = mock.Mock()
        child.poll.return_value = 1
        launcher.write_desktop_result(self.request_id, "failed", {"process": child})
        self.assertFalse(json.loads(self.path.read_text())["child_alive"])

    @unittest.skipUnless(os.name == "nt", "Windows process creation-time receipt")
    def test_real_child_receipt_contains_windows_process_identity(self):
        with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              creationflags=subprocess.CREATE_NO_WINDOW) as child:
            launcher.write_desktop_result(self.request_id, "failed", {"process": child})
            record = json.loads(self.path.read_text())
            self.assertTrue(record["child_alive"])
            self.assertEqual(record["child_pid"], child.pid)
            self.assertGreater(int(record["child_start_filetime"]), 0)
            self.assertEqual(child.wait(timeout=10), 0)

    def test_existing_file_is_never_overwritten(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("preserve-user-file")
        with self.assertRaises(FileExistsError):
            launcher.write_desktop_result(self.request_id, "failed", {})
        self.assertEqual(self.path.read_text(), "preserve-user-file")

    def test_arbitrary_paths_are_rejected(self):
        for value in ("../outside", "/tmp/result", "A" * 32, "a" * 31, "a" * 33):
            with self.subTest(value=value), self.assertRaises(ValueError):
                launcher.write_desktop_result(value, "failed", {})

    def test_failure_without_child_writes_receipt(self):
        with mock.patch.object(launcher, "ensure_running", side_effect=RuntimeError("fixture failure")):
            result = launcher.main(["--no-dialog", "--no-browser", "--desktop-result-id", self.request_id])
        self.assertEqual(result, 1)
        self.assertFalse(json.loads(self.path.read_text())["child_alive"])

    def test_timeout_receipt_preserves_live_child(self):
        child = mock.Mock()
        child.poll.return_value = None

        def late_start(port, startup_state):
            startup_state["process"] = child
            raise RuntimeError("fixture startup timeout")

        with mock.patch.object(launcher, "ensure_running", side_effect=late_start):
            self.assertEqual(launcher.main(["--no-dialog", "--no-browser", "--desktop-result-id", self.request_id]), 1)
        self.assertTrue(json.loads(self.path.read_text())["child_alive"])
        child.kill.assert_not_called()
        child.terminate.assert_not_called()

    def test_receipt_failure_does_not_change_launch_result(self):
        with mock.patch.object(launcher, "ensure_running", return_value={"status": "reused", "port": 8765}), \
                mock.patch.object(launcher, "write_desktop_result", side_effect=OSError("fixture read-only")), \
                mock.patch("builtins.print"):
            self.assertEqual(launcher.main(["--no-dialog", "--no-browser", "--desktop-result-id", self.request_id]), 0)

    def test_browser_entry_does_not_write_desktop_receipt(self):
        with mock.patch.object(launcher, "ensure_running", return_value={"status": "reused", "port": 8765}), \
                mock.patch.object(launcher, "write_desktop_result") as receipt, \
                mock.patch.object(launcher.webbrowser, "open") as browser, mock.patch("builtins.print"):
            self.assertEqual(launcher.main(["--no-dialog"]), 0)
        receipt.assert_not_called()
        browser.assert_called_once()


if __name__ == "__main__":
    unittest.main()
