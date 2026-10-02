"""Manager-level policy checks using a synthetic release and isolated storage."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import zipfile

from aihub import app_update as updater
from test_app_update import package_bytes, feed_value, wait_state


def bound_feed(package):
    value = json.loads(feed_value(package, version='2.13.6', minimum='2.13.5'))
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        manifest = archive.read('AI-Hub/manifest.json')
    value.update(schema=updater.BOUND_FEED_SCHEMA, platform='windows', architecture='x64',
                 build_id='2.13.6-' + value['package']['sha256'][:16])
    value['package']['manifest_sha256'] = hashlib.sha256(manifest).hexdigest()
    return value


class UpdatePolicyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.package = package_bytes({'AI Hub.exe': b'exe'}, version='2.13.6')
        self.feed = bound_feed(self.package)
        self.calls = []

    def fetch(self, url, maximum):
        self.calls.append(url)
        return json.dumps(self.feed).encode() if url == updater.FEED_URL else self.package

    def manager(self, fetch=None):
        manager = updater.UpdateManager(self.root, '2.13.5', fetcher=fetch or self.fetch)
        self.addCleanup(manager.close)
        return manager

    def finish(self, manager):
        limit = time.monotonic() + 4
        while manager.status()['operation_active'] and time.monotonic() < limit:
            time.sleep(.01)
        self.assertFalse(manager.status()['operation_active'])

    def test_manual_check_bypasses_ui_delay_but_restart_keeps_deadline(self):
        manager = self.manager()
        manager.check(automatic=True)
        self.assertFalse(self.calls)
        manager.check()
        wait_state(manager, 'available')
        self.finish(manager)
        deadline = manager.status()['next_check_at']
        manager.close()
        second = self.manager()
        second._ui_ready_at = time.time() - 61
        second.check(automatic=True)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(second.status()['next_check_at'], deadline)
        second.check()
        self.finish(second)
        self.assertEqual(len(self.calls), 2)

    def test_target_channel_and_build_identity_are_strict(self):
        for field, value in [('platform', 'linux'), ('architecture', 'arm64'),
                             ('channel', 'stable'), ('build_id', '2.13.6-' + '0' * 16)]:
            with self.subTest(field=field):
                changed = dict(self.feed, **{field: value})
                with self.assertRaises(updater.UpdateSecurityError):
                    updater._validate_feed(json.dumps(changed).encode(), '2.13.5')

    def test_manifest_binding_failure_never_stages_package(self):
        self.feed['package']['manifest_sha256'] = '0' * 64
        manager = self.manager()
        manager.check()
        available = wait_state(manager, 'available')
        self.finish(manager)
        manager.download(available['release_id'])
        status = wait_state(manager, 'failed')
        self.assertEqual(status['error_code'], 'invalid_update')
        self.assertFalse(list(manager.packages.glob('*.zip')))

    def test_restored_cache_revalidates_feed_and_recomputes_url(self):
        manager = self.manager()
        manager.check()
        available = wait_state(manager, 'available')
        self.finish(manager)
        manager.download(available['release_id'])
        wait_state(manager, 'ready')
        self.finish(manager)
        manager.close()
        path = manager.base / 'status.json'
        saved = json.loads(path.read_text())
        saved['release']['package_url'] = 'https://untrusted.example/payload'
        path.write_text(json.dumps(saved))
        second = self.manager()
        self.assertEqual(second.status()['state'], 'ready')
        self.assertTrue(second._release['package_url'].startswith(updater.PACKAGE_BASE))
        second.close()
        saved['release']['architecture'] = 'arm64'
        path.write_text(json.dumps(saved))
        third = self.manager()
        self.assertNotEqual(third.status()['state'], 'ready')

    def test_failed_check_is_throttled_across_restart(self):
        def broken(*args):
            raise OSError('offline')
        manager = self.manager(broken)
        manager.check()
        wait_state(manager, 'failed')
        self.finish(manager)
        self.assertEqual(manager.status()['check_failures'], 1)
        manager.close()
        second = self.manager()
        second._ui_ready_at = time.time() - 61
        second.check(automatic=True)
        self.assertFalse(self.calls)

    def test_failed_schedule_write_stops_network(self):
        manager = self.manager()
        def denied(*args):
            raise PermissionError('read-only fixture')
        manager._schedule_state._writer = denied
        with self.assertRaises(updater.UpdateBusyError):
            manager.check()
        self.assertFalse(self.calls)

    def test_installed_shell_version_mismatch_is_rejected_before_helper(self):
        content = b'fixture executable'
        (self.root / 'AI Hub.exe').write_bytes(content)
        ledger = {'version': '2.13.5', 'desktop_shell_version': '2.13.4',
                  'files': [{'path': 'AI Hub.exe', 'bytes': len(content),
                             'sha256': hashlib.sha256(content).hexdigest()}]}
        (self.root / 'manifest.json').write_text(json.dumps(ledger))
        with self.assertRaises(updater.UpdateBusyError):
            updater._verify_installed_manifest(self.root, updater._program_files(self.root), '2.13.5')
