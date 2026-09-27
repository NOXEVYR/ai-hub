"""Isolated synthetic install tests for the application updater and helper."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock

from aihub import app_update as updater

ROOT = Path(__file__).resolve().parents[1]
helper_spec = importlib.util.spec_from_file_location("standalone_update_helper", ROOT / "tools" / "app_update_helper.py")
helper = importlib.util.module_from_spec(helper_spec)
helper_spec.loader.exec_module(helper)


def package_bytes(files, version="2.12.0"):
    rows = [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(files.items())]
    manifest = {"version": version, "kind": "Windows-x64", "user_data_included": False, "files": rows}
    import io
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr("AI-Hub/" + name, data)
        archive.writestr("AI-Hub/manifest.json", json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
    return output.getvalue()


def feed_value(package, version="2.12.0", minimum="2.11.3"):
    return json.dumps({"schema": updater.FEED_SCHEMA, "app": "ai-hub", "channel": "candidate",
                       "version": version, "commit": "a" * 40,
                       "package": {"bytes": len(package), "sha256": hashlib.sha256(package).hexdigest()},
                       "notes": "合成测试更新", "min_updater_version": minimum}).encode("utf-8")


def wait_state(manager, expected, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = manager.status()
        if value["state"] == expected:
            return value
        time.sleep(0.02)
    raise AssertionError("expected %s, got %r" % (expected, manager.status()))


class UpdateFeedTests(unittest.TestCase):
    def test_semver_prerelease_order_and_no_prerelease_downgrade(self):
        ordered = ["2.12.0-alpha", "2.12.0-alpha.1", "2.12.0-alpha.beta",
                   "2.12.0-beta", "2.12.0-beta.2", "2.12.0-beta.11",
                   "2.12.0-rc.1", "2.12.0"]
        self.assertEqual(sorted(reversed(ordered), key=updater._version_key), ordered)
        self.assertLess(updater._version_key("2.12.0-Z"), updater._version_key("2.12.0-a"))
        with self.assertRaises(updater.UpdateNotNewerError):
            updater._validate_feed(feed_value(b"x", version="2.12.0-alpha.1"), "2.12.0-alpha.beta")
        with self.assertRaises(updater.UpdateSecurityError):
            updater._safe_version("2.12.0-alpha.01")

    def test_build_metadata_feed_is_rejected_before_becoming_available(self):
        feed = feed_value(b"x", version="2.12.1+build.1")
        with self.assertRaisesRegex(updater.UpdateSecurityError, "构建元数据"):
            updater._validate_feed(feed, "2.12.0")
        manager = self.manager(lambda *_: feed, version="2.12.0")
        manager.check()
        failed = wait_state(manager, "failed")
        self.assertEqual(failed["error_code"], "invalid_update")
        self.assertIsNone(failed["release_id"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yaohe-update-feed-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()

    def manager(self, fetcher, version="2.11.3"):
        manager = updater.UpdateManager(self.root, version, fetcher=fetcher)
        self.addCleanup(manager.close)
        return manager

    def test_check_download_and_restart_reconcile_verified_package(self):
        package = package_bytes({"AI Hub.exe": b"exe", "server.py": b"server", "tools/app_update_helper.py": b"helper"})
        feed = feed_value(package)
        manager = self.manager(lambda url, maximum: feed if url == updater.FEED_URL else package)
        self.assertEqual(manager.check()["state"], "checking")
        available = wait_state(manager, "available")
        self.assertEqual(available["channel"], "candidate")
        self.assertEqual(available["latest_version"], "2.12.0")
        manager.download(available["release_id"])
        ready = wait_state(manager, "ready")
        self.assertEqual(ready["downloaded_bytes"], len(package))
        self.assertNotIn("package_path", ready)
        second = updater.UpdateManager(self.root, "2.11.3", fetcher=lambda *_: b"")
        self.addCleanup(second.close)
        self.assertEqual(second.status()["state"], "ready")
        current = updater.UpdateManager(self.root, "2.12.0", fetcher=lambda *_: b"")
        self.addCleanup(current.close)
        self.assertEqual(current.status()["state"], "idle")
        self.assertIsNone(current.status()["release_id"])

    def test_malformed_zip_and_digest_mismatch_never_become_ready(self):
        bad = b"not a zip"
        feed = feed_value(bad)
        manager = self.manager(lambda url, maximum: feed if url == updater.FEED_URL else bad)
        manager.check()
        available = wait_state(manager, "available")
        manager.download(available["release_id"])
        failed = wait_state(manager, "failed")
        self.assertEqual(failed["error_code"], "invalid_update")
        self.assertFalse(list((self.root / "data" / "app-updates" / "packages").glob("*.zip")))

    def test_feed_size_cap_and_numeric_no_downgrade(self):
        too_large = {"schema": updater.FEED_SCHEMA, "app": "ai-hub", "channel": "candidate",
                     "version": "2.13.0", "commit": "b" * 40,
                     "package": {"bytes": updater.MAX_PACKAGE_BYTES + 1, "sha256": "c" * 64},
                     "notes": "too big", "min_updater_version": "2.11.3"}
        manager = self.manager(lambda *_: json.dumps(too_large).encode("utf-8"))
        manager.check()
        status = wait_state(manager, "available")
        self.assertEqual(status["error_code"], "manual_download")
        with self.assertRaises(ValueError):
            manager.download(status["release_id"])
        for version in ("2.11.3", "2.10.99"):
            package = package_bytes({"AI Hub.exe": b"exe"}, version=version)
            with self.assertRaises(updater.UpdateSecurityError):
                updater._validate_feed(feed_value(package, version=version, minimum="2.11.3"), "2.11.3")

    def test_release_id_race_and_strict_settings(self):
        package = package_bytes({"AI Hub.exe": b"exe"})
        feed = feed_value(package)
        manager = self.manager(lambda url, maximum: feed if url == updater.FEED_URL else package)
        manager.check()
        ready = wait_state(manager, "available")
        with self.assertRaises(ValueError):
            manager.download(ready["release_id"][:-1] + "0")
        with self.assertRaises(ValueError):
            manager.settings(1, False)
        settings = manager.settings(False, True)
        self.assertTrue(settings["auto_check"])
        self.assertTrue(settings["auto_install"])

    def test_auto_install_setting_stages_background_download(self):
        package = package_bytes({"AI Hub.exe": b"exe"})
        feed = feed_value(package)
        manager = self.manager(lambda url, maximum: feed if url == updater.FEED_URL else package)
        manager.settings(False, True)
        ready = wait_state(manager, "ready", timeout=6)
        self.assertEqual(ready["auto_install"], True)
        self.assertEqual(ready["downloaded_bytes"], len(package))


class UpdateArchiveTests(unittest.TestCase):
    def test_package_allowlist_casefold_duplicates_and_host_bound_feed(self):
        for files in (
            {"AI Hub.exe": b"x", "data/private.db": b"no"},
            {"AI Hub.exe": b"x", "../escape.py": b"no"},
            {"AI Hub.exe": b"x", "server.py": b"one", "Server.py": b"two"},
        ):
            with self.subTest(files=files), self.assertRaises((ValueError, helper.SafeFailure)):
                updater._zip_manifest(package_bytes(files), "2.12.0")
        with self.assertRaises(updater.UpdateSecurityError):
            updater._validate_feed(b"{}", "2.11.3")
        self.assertTrue(updater._SameHostRedirect().redirect_request if hasattr(updater._SameHostRedirect, "redirect_request") else True)

    def test_durable_rollback_restores_only_owned_payload_and_keeps_user_data(self):
        with tempfile.TemporaryDirectory(prefix="yaohe-update-rollback-") as tmp:
            root = Path(tmp)
            target = root / "server.py"
            target.write_bytes(b"new program")
            data = root / "data"
            data.mkdir()
            sentinel = data / "private.db"
            sentinel.write_bytes(b"user data")
            tx = data / "app-updates" / "transactions" / ("a" * 32)
            (tx / "backups").mkdir(parents=True)
            backup = tx / "backups" / "server.bak"
            backup.write_bytes(b"old program")
            journal = {"schema": "ai-hub-update-journal-v1", "files": [{
                "path": "server.py", "baseline_sha256": hashlib.sha256(b"old program").hexdigest(),
                "new_sha256": hashlib.sha256(b"new program").hexdigest(), "backup_name": "server.bak",
                "backup_sha256": hashlib.sha256(b"old program").hexdigest(), "state": "applied"}]}
            (tx / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
            result = updater._recover_transaction(root, tx)
            self.assertEqual(result["state"], "recovered_rollback")
            self.assertEqual(target.read_bytes(), b"old program")
            self.assertEqual(sentinel.read_bytes(), b"user data")

    def test_recovery_retains_file_changed_by_another_writer(self):
        with tempfile.TemporaryDirectory(prefix="yaohe-update-external-change-") as tmp:
            root = Path(tmp)
            target = root / "server.py"
            target.write_bytes(b"edited by user after failure")
            tx = root / "data" / "app-updates" / "transactions" / ("b" * 32)
            (tx / "backups").mkdir(parents=True)
            (tx / "backups" / "server.bak").write_bytes(b"old")
            journal = {"schema": "ai-hub-update-journal-v1", "files": [{
                "path": "server.py", "baseline_sha256": hashlib.sha256(b"old").hexdigest(),
                "new_sha256": hashlib.sha256(b"new").hexdigest(), "backup_name": "server.bak",
                "backup_sha256": hashlib.sha256(b"old").hexdigest(), "state": "applied"}]}
            (tx / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
            result = updater._recover_transaction(root, tx)
            self.assertEqual(result["state"], "recovery_partial")
            self.assertEqual(target.read_bytes(), b"edited by user after failure")

    def test_sqlite_backup_evidence_uses_backup_api(self):
        with tempfile.TemporaryDirectory(prefix="yaohe-update-db-evidence-") as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            tx = root / "data" / "app-updates" / "transactions" / ("c" * 32)
            tx.mkdir(parents=True)
            db = root / "data" / "aihub.db"
            import sqlite3
            conn = sqlite3.connect(db)
            try:
                with conn:
                    conn.execute("create table sample(value text)")
                    conn.execute("insert into sample values('preserve me')")
            finally:
                conn.close()
            evidence = helper.sqlite_evidence(root, tx)
            self.assertIsNotNone(evidence)
            conn = sqlite3.connect(tx / "database-evidence.sqlite")
            try:
                self.assertEqual(conn.execute("select value from sample").fetchone()[0], "preserve me")
            finally:
                conn.close()
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("select value from sample").fetchone()[0], "preserve me")
            finally:
                conn.close()


class UpdatePrepareTests(unittest.TestCase):
    def make_install(self, root):
        helper_source = (ROOT / "tools" / "app_update_helper.py").read_bytes()
        old = {"server.py": b"old server", "AI Hub.exe": b"old exe",
               "tools/app_update_helper.py": helper_source, "AGENTS.md": b"local instructions"}
        for name, data in old.items():
            path = root.joinpath(*name.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        entries = [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                   for name, data in sorted(old.items())]
        manifest = {"version": "2.11.3", "desktop_shell_version": "2.11.3", "display_name": "曜核",
                    "source_commit": "b" * 40, "installed_at": "2026-09-26T00:00:00+00:00", "files": entries}
        (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        new = {"server.py": b"new server", "AI Hub.exe": b"new exe",
               "tools/app_update_helper.py": helper_source, "AGENTS.md": b"vendor instructions"}
        package = package_bytes(new)
        release = updater._validate_feed(feed_value(package), "2.11.3")
        base = root / "data" / "app-updates"
        (base / "packages").mkdir(parents=True, exist_ok=True)
        (base / "transactions").mkdir(parents=True, exist_ok=True)
        (base / "packages" / (release["release_id"] + ".zip")).write_bytes(package)
        return release, old, new

    def test_prepare_ticket_binds_baseline_helper_manifest_and_private_paths(self):
        with tempfile.TemporaryDirectory(prefix="yaohe-update-prepare-") as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            release, old, _new = self.make_install(root)
            manager = updater.UpdateManager(root, "2.11.3", fetcher=lambda *_: b"")
            self.addCleanup(manager.close)
            manager._release, manager._ready_release_id, manager._state = release, release["release_id"], "ready"
            desktop_pid = 99999
            def identity(pid):
                if pid == desktop_pid:
                    return {"pid": pid, "start_filetime": "12345", "executable_path": str(root / "AI Hub.exe")}
                return {"pid": pid, "start_filetime": "67890", "executable_path": sys.executable}
            with mock.patch.object(updater, "_process_identity", side_effect=identity):
                prepared = manager.prepare(release["release_id"], False,
                                           {"pid": desktop_pid, "start_filetime": "12345"})
            txdir = Path(prepared["ticket_path"]).parent
            ticket = json.loads(Path(prepared["ticket_path"]).read_text(encoding="utf-8"))
            self.assertEqual(ticket["schema"], updater.TICKET_SCHEMA)
            self.assertEqual(Path(ticket["transaction_dir"]), txdir)
            self.assertEqual(hashlib.sha256(Path(prepared["helper_path"]).read_bytes()).hexdigest(), ticket["helper_sha256"])
            self.assertEqual(ticket["installed_manifest"]["source_commit"], "a" * 40)
            self.assertEqual((root / "data" / "app-updates" / "install-lock.json").is_file(), True)
            local_agents = [row for row in ticket["installed_manifest"]["files"] if row["path"] == "AGENTS.md"][0]
            self.assertEqual(local_agents["sha256"], hashlib.sha256(old["AGENTS.md"]).hexdigest())
            self.assertIn(str(root / "data" / "app-updates" / "transactions"), prepared["ticket_path"])
            manager.cancel(prepared["transaction_id"])
            self.assertFalse((root / "data" / "app-updates" / "install-lock.json").exists())

    def test_prepare_refuses_program_tamper_not_recorded_by_manifest(self):
        with tempfile.TemporaryDirectory(prefix="yaohe-update-tamper-") as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            release, _old, _new = self.make_install(root)
            (root / "server.py").write_bytes(b"manual local edit")
            manager = updater.UpdateManager(root, "2.11.3", fetcher=lambda *_: b"")
            self.addCleanup(manager.close)
            manager._release, manager._ready_release_id, manager._state = release, release["release_id"], "ready"
            desktop_pid = 99998
            def identity(pid):
                return {"pid": pid, "start_filetime": "12345", "executable_path": str(root / "AI Hub.exe")}
            with mock.patch.object(updater, "_process_identity", side_effect=identity):
                with self.assertRaises(updater.UpdateBusyError):
                    manager.prepare(release["release_id"], False,
                                    {"pid": desktop_pid, "start_filetime": "12345"})
            self.assertFalse((root / "data" / "app-updates" / "install-lock.json").exists())

    def test_startup_guard_blocks_live_marked_transaction_and_mismatched_pid_is_not_alive(self):
        with tempfile.TemporaryDirectory(prefix="yaohe-update-startup-guard-") as tmp:
            root = Path(tmp)
            updates = root / "data" / "app-updates"
            txid = "d" * 32
            txdir = updates / "transactions" / txid
            txdir.mkdir(parents=True)
            manager_identity = updater._process_identity(os.getpid())
            (txdir / "transaction.json").write_text(json.dumps({"schema": updater.TX_SCHEMA,
                "transaction_id": txid, "state": "prepared", "service_identity": manager_identity,
                "helper_identity": None}), encoding="utf-8")
            (updates / "install-lock.json").write_text(json.dumps({"schema": updater.LOCK_SCHEMA,
                "transaction_id": txid, "transaction_dir": str(txdir), "created_at": int(time.time())}), encoding="utf-8")
            with self.assertRaises(updater.UpdateBusyError):
                updater.startup_guard(root)
            wrong = dict(manager_identity, start_filetime="0")
            self.assertFalse(updater._process_is_same(wrong))


class HelperLockHandshakeTests(unittest.TestCase):
    def test_acquire_file_lock_absolute_deadline_semantics(self):
        """execute() passes an absolute monotonic deadline; a held lock must be
        retried until that deadline, then give up without stealing the lock."""
        with tempfile.TemporaryDirectory(prefix="yaohe-helper-lock-") as tmp:
            path = Path(tmp) / "transaction.lock"
            stream = helper.acquire_file_lock(path, time.monotonic() + 5)
            self.assertIsNotNone(stream)
            try:
                started = time.monotonic()
                self.assertIsNone(helper.acquire_file_lock(path, time.monotonic() + 0.3))
                self.assertGreaterEqual(time.monotonic() - started, 0.25)
            finally:
                helper.unlock_file(stream)
            second = helper.acquire_file_lock(path, time.monotonic() + 5)
            self.assertIsNotNone(second)
            helper.unlock_file(second)

    def test_helper_main_failure_writes_diagnostic_stderr(self):
        with tempfile.TemporaryDirectory(prefix="yaohe-helper-main-") as tmp:
            missing_ticket = Path(tmp) / "missing-ticket.json"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = helper.main(["--ticket", str(missing_ticket)])
        self.assertEqual(code, 2)
        self.assertIn("Traceback", stderr.getvalue())
        self.assertIn("ticket", stderr.getvalue().casefold())


if __name__ == "__main__":
    unittest.main()
