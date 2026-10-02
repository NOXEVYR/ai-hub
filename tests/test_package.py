import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from aihub import app_update

MODULE = Path(__file__).resolve().parents[1] / "tools/package_release.py"
spec = importlib.util.spec_from_file_location("package_release", MODULE)
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def fixture(self, root):
        for name in package.ROOT_FILES:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("public source", encoding="utf-8")
        for name in ("docs/DISTRIBUTION_README.md", "docs/REPOSITORY_AGENTS.md", "frontend/app.js", "aihub/api.py"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("public source", encoding="utf-8")

    def test_allowlist_excludes_personal_state_backups_and_shortcuts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            for name in ("data/config.json", "data/desktop/profile.js", "backups/code.py", "desktop/vendor/secret.py",
                         "tests/_tmp/secret.py", "docs/coop-00/DRAFT.md", "docs/coop-00/fixtures/example.json",
                         "README.md", "VALIDATION.md", "AI Hub.lnk", ".env"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("PRIVATE-SENTINEL", encoding="utf-8")
            files = package.source_files(root)
            self.assertNotIn("PRIVATE-SENTINEL", b"".join(files.values()).decode())
            self.assertEqual(files["README.md"], b"public source")

    def test_manifest_matches_archive_contents_and_build_is_reproducible(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            files = {"frontend/app.js": b"example", "README.md": "说明".encode()}
            a = package.write_archive(folder / "a.zip", files, "2.2.0", "Source")
            b = package.write_archive(folder / "b.zip", files, "2.2.0", "Source")
            self.assertEqual(a["sha256"], b["sha256"])
            with zipfile.ZipFile(folder / "a.zip") as archive:
                manifest = json.loads(archive.read("AI-Hub/manifest.json"))
                self.assertFalse(manifest["user_data_included"])
                for item in manifest["files"]:
                    data = archive.read("AI-Hub/" + item["path"])
                    self.assertEqual(item["sha256"], package.hashlib.sha256(data).hexdigest())

    def test_rejects_console_or_non_windows_executable(self):
        with self.assertRaises(ValueError):
            package.check_exe(b"not executable")
        data = bytearray(300)
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 0x3c, 128)
        data[128:132] = b"PE\0\0"
        struct.pack_into("<H", data, 132, 0x8664)
        struct.pack_into("<H", data, 128 + 24 + 68, 3)
        with self.assertRaises(ValueError):
            package.check_exe(data)
        struct.pack_into("<H", data, 128 + 24 + 68, 2)
        package.check_exe(data)

    def test_fresh_windows_archive_is_a_valid_installed_update_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            files = {"AI Hub.exe": b"synthetic exe", "server.py": b"synthetic server"}
            package.write_archive(folder / "windows.zip", files, "2.13.0", "Windows-x64")
            with zipfile.ZipFile(folder / "windows.zip") as archive:
                archive.extractall(folder / "unpacked")
            root = folder / "unpacked" / "AI-Hub"
            manifest_before = (root / "manifest.json").read_bytes()
            ledger = app_update._verify_installed_manifest(root, app_update._program_files(root), "2.13.0")
            self.assertEqual(ledger["kind"], "Windows-x64")
            self.assertEqual((root / "manifest.json").read_bytes(), manifest_before)
            (root / "server.py").write_bytes(b"changed")
            with self.assertRaises(app_update.UpdateBusyError):
                app_update._verify_installed_manifest(root, app_update._program_files(root), "2.13.0")

    def test_source_archive_cannot_become_windows_update_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            package.write_archive(folder / "source.zip", {"server.py": b"source"}, "2.13.0", "Source")
            with zipfile.ZipFile(folder / "source.zip") as archive:
                archive.extractall(folder)
            root = folder / "AI-Hub"
            with self.assertRaises(app_update.UpdateBusyError):
                app_update._verify_installed_manifest(root, app_update._program_files(root), "2.13.0")
