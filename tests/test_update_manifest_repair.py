import json
from pathlib import Path
import tempfile
import unittest

from aihub import app_update
from tools.repair_update_manifest import repair


class LegacyManifestRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yaohe-legacy-ledger-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        files = {"AI Hub.exe": b"old exe", "server.py": b"old server"}
        for name, content in files.items():
            (self.root / name).write_bytes(content)
        self.ledger = {"version": "2.13.0", "kind": "Windows-x64", "user_data_included": False,
                       "files": [{"path": name, "bytes": len(content), "sha256": app_update._sha(content)}
                                 for name, content in files.items()]}
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps(self.ledger), encoding="utf-8")

    def test_preview_then_backup_and_minimal_idempotent_repair(self):
        before = self.manifest.read_bytes()
        program = app_update._program_files(self.root)
        self.assertFalse(repair(self.root)["written"])
        self.assertFalse((self.root / "data").exists())
        result = repair(self.root, True)
        self.assertEqual(Path(result["backup"]).read_bytes(), before)
        self.assertEqual(app_update._program_files(self.root), program)
        updated = json.loads(self.manifest.read_bytes())
        self.assertEqual(updated.pop("desktop_shell_version"), "2.13.0")
        self.assertEqual(updated, self.ledger)
        self.assertFalse(repair(self.root, True)["written"])

    def test_changed_program_is_not_blessed_by_repair(self):
        before = self.manifest.read_bytes()
        (self.root / "server.py").write_bytes(b"local changes")
        with self.assertRaises(app_update.UpdateBusyError):
            repair(self.root, True)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertFalse((self.root / "data").exists())

    def test_helper_affected_versions_preserve_every_program_byte(self):
        baseline = app_update._program_files(self.root)
        for version in ('2.13.2', '2.13.3', '2.13.4'):
            with self.subTest(version=version):
                self.ledger['version'] = version
                self.manifest.write_text(json.dumps(self.ledger), encoding='utf-8')
                original = self.manifest.read_bytes()
                result = repair(self.root, True)
                self.assertEqual(result['status'], 'repaired')
                self.assertEqual(Path(result['backup']).read_bytes(), original)
                self.assertEqual(app_update._program_files(self.root), baseline)
                self.assertEqual(json.loads(self.manifest.read_bytes())['desktop_shell_version'], version)

    def test_active_transaction_blocks_repair(self):
        base = self.root / "data" / "app-updates"
        base.mkdir(parents=True)
        (base / "install-lock.json").write_text("{}")
        with self.assertRaises(app_update.UpdateBusyError):
            repair(self.root, True)
        self.assertNotIn("desktop_shell_version", json.loads(self.manifest.read_bytes()))

    def test_source_package_is_not_repaired(self):
        self.ledger["kind"] = "Source"
        self.manifest.write_text(json.dumps(self.ledger))
        with self.assertRaises(app_update.UpdateBusyError):
            repair(self.root, True)

    def test_version_with_separate_runtime_mismatch_is_not_claimed_repaired(self):
        self.ledger["version"] = "2.13.1"
        self.manifest.write_text(json.dumps(self.ledger))
        with self.assertRaisesRegex(ValueError, "仅修复清单不足"):
            repair(self.root, True)
        self.assertFalse((self.root / "data").exists())
