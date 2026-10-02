"""Original package ledgers stay byte-identical; all installations are fixtures."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

from aihub import app_update as updater
from tools import package_release

ROOT = Path(__file__).resolve().parents[1]


def load_helper(path):
    specification = importlib.util.spec_from_file_location('raw_ledger_helper', path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


helper = load_helper(ROOT / 'tools' / 'app_update_helper.py')


class RawLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='aihub-raw-ledger-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'install'
        self.root.mkdir()
        self.files = {'AI Hub.exe': b'fixture desktop', 'server.py': b'fixture server',
                      'AGENTS.md': b'original package rules',
                      'tools/app_update_helper.py': (ROOT / 'tools' / 'app_update_helper.py').read_bytes()}
        archive_path = self.base / 'original.zip'
        package_release.write_archive(archive_path, self.files, '2.13.4', 'Windows-x64')
        with zipfile.ZipFile(archive_path) as archive:
            self.raw = archive.read('AI-Hub/manifest.json')
            for name in archive.namelist():
                destination = self.root / name.removeprefix('AI-Hub/')
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(name))
        self.manifest = json.loads(self.raw)

    def write_manifest(self, manifest):
        (self.root / 'manifest.json').write_bytes(json.dumps(manifest, ensure_ascii=False).encode('utf-8'))

    def verify(self):
        return helper.verify_installed_ledger(self.root, helper.program_inventory(self.root))

    def prepare(self):
        target_path = self.base / 'target.zip'
        target_files = {**self.files, 'server.py': b'updated fixture server'}
        package_release.write_archive(target_path, target_files, '2.13.5', 'Windows-x64')
        blob = target_path.read_bytes()
        feed = {'schema': updater.FEED_SCHEMA, 'app': 'ai-hub', 'channel': 'candidate',
                'version': '2.13.5', 'commit': 'a' * 40, 'min_updater_version': '2.13.4',
                'notes': 'fixture only', 'package': {'bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}}
        release = updater._validate_feed(json.dumps(feed).encode('utf-8'), '2.13.4')
        manager = updater.UpdateManager(self.root, '2.13.4', fetcher=lambda *_: b'')
        self.addCleanup(manager.close)
        (manager.packages / (release['release_id'] + '.zip')).write_bytes(blob)
        manager._release, manager._ready_release_id, manager._state = release, release['release_id'], 'ready'
        desktop_pid = 99998
        def identity(pid):
            return {'pid': pid, 'start_filetime': '12345',
                    'executable_path': str(self.root / 'AI Hub.exe') if pid == desktop_pid else sys.executable}
        with mock.patch.object(updater, '_process_identity', side_effect=identity):
            prepared = manager.prepare(release['release_id'], False, {'pid': desktop_pid, 'start_filetime': '12345'})
        return manager, prepared, load_helper(Path(prepared['helper_path']))

    def test_actual_release_writer_raw_manifest_is_accepted_without_rewriting(self):
        self.assertEqual(set(self.manifest), {'version', 'kind', 'user_data_included', 'files'})
        self.assertEqual(self.verify(), self.manifest)
        self.assertEqual((self.root / 'manifest.json').read_bytes(), self.raw)

    def test_source_archive_and_undeclared_raw_metadata_are_rejected(self):
        for patch in [{'kind': 'Source'}, {'kind': None}, {'user_data_included': True},
                      {'user_data_included': 0}, {'user_data_included': 'false'},
                      {'display_name': 'extra non-raw field'}]:
            with self.subTest(patch=patch):
                self.write_manifest({**self.manifest, **patch})
                with self.assertRaises(helper.SafeFailure):
                    self.verify()

    def test_invalid_version_values_are_rejected(self):
        for version in [None, False, 2.134, [], {}, '', '2.13', '02.13.4',
                        '2.13.4-alpha..1', '2.13.4-alpha.01', '2.13.4+build..1']:
            with self.subTest(version=version):
                self.write_manifest({**self.manifest, 'version': version})
                with self.assertRaises(helper.SafeFailure):
                    self.verify()

    def test_installed_ledger_keeps_valid_shell_identity_and_rejects_mismatch(self):
        ledger = {'version': '2.13.4', 'desktop_shell_version': '2.13.4',
                  'source_commit': 'b' * 40, 'files': self.manifest['files']}
        self.write_manifest(ledger)
        self.assertEqual(self.verify(), ledger)
        for shell in [None, True, 2.134, 'bad', '2.13.3', '2.13.4-alpha..1']:
            with self.subTest(shell=shell):
                self.write_manifest({**ledger, 'desktop_shell_version': shell})
                with self.assertRaises(helper.SafeFailure):
                    self.verify()
        self.write_manifest({key: value for key, value in ledger.items() if key != 'desktop_shell_version'})
        with self.assertRaises(helper.SafeFailure):
            self.verify()
        for metadata in [{'kind': 'Source'}, {'user_data_included': True}]:
            self.write_manifest({**ledger, **metadata})
            with self.assertRaises(helper.SafeFailure):
                self.verify()

    def test_raw_windows_ledger_requires_actual_desktop(self):
        (self.root / 'AI Hub.exe').unlink()
        self.write_manifest({**self.manifest, 'files': [row for row in self.manifest['files'] if row['path'] != 'AI Hub.exe']})
        with self.assertRaises(helper.SafeFailure):
            self.verify()

    def test_local_agents_remains_customizable_and_unchanged(self):
        custom = b'private workstation rules'
        (self.root / 'AGENTS.md').write_bytes(custom)
        self.verify()
        self.assertEqual((self.root / 'AGENTS.md').read_bytes(), custom)
        self.assertEqual((self.root / 'manifest.json').read_bytes(), self.raw)

    def test_changed_program_files_sizes_and_unlisted_files_are_rejected(self):
        (self.root / 'server.py').write_bytes(b'x' * len(self.files['server.py']))
        with self.assertRaises(helper.SafeFailure):
            self.verify()
        (self.root / 'server.py').write_bytes(self.files['server.py'])
        broken = copy.deepcopy(self.manifest)
        next(row for row in broken['files'] if row['path'] == 'server.py')['bytes'] += 1
        self.write_manifest(broken)
        with self.assertRaises(helper.SafeFailure):
            self.verify()
        self.write_manifest(self.manifest)
        (self.root / 'frontend').mkdir()
        (self.root / 'frontend' / 'unlisted.js').write_bytes(b'local code')
        with self.assertRaises(helper.SafeFailure):
            self.verify()

    def test_allowlist_row_types_and_case_duplicates_are_rejected(self):
        invalid_rows = [{'path': 'data/private.py', 'bytes': 0, 'sha256': 'a' * 64},
                        {'path': '../server.py', 'bytes': 0, 'sha256': 'a' * 64},
                        {'path': 'AGENTS.md', 'bytes': True, 'sha256': 'a' * 64},
                        {'path': 'AGENTS.md', 'bytes': -1, 'sha256': 'a' * 64},
                        {'path': 'AGENTS.md', 'bytes': 1, 'sha256': None},
                        {'path': 'Server.py', 'bytes': 0, 'sha256': 'a' * 64}]
        for row in invalid_rows:
            with self.subTest(row=row):
                self.write_manifest({**self.manifest, 'files': [*self.manifest['files'], row]})
                with self.assertRaises(helper.SafeFailure):
                    self.verify()
        for files in [None, {}, [], [True]]:
            self.write_manifest({**self.manifest, 'files': files})
            with self.assertRaises(helper.SafeFailure):
                self.verify()

    def test_raw_prepare_and_helper_ticket_keep_distinct_target_install_ledger(self):
        _manager, prepared, copied = self.prepare()
        ticket_path = Path(prepared['ticket_path'])
        ticket, root, _txdir, _package, _contents, rows, _serialized = copied.verify_ticket(ticket_path)
        copied.verify_baseline(ticket, root, rows)
        copied.verify_installed_ledger(root, copied.program_inventory(root))
        self.assertEqual(ticket['installed_manifest']['desktop_shell_version'], '2.13.5')
        self.assertEqual(ticket['installed_manifest']['version'], '2.13.5')
        self.assertEqual((self.root / 'manifest.json').read_bytes(), self.raw)
        # Raw-source compatibility never permits omitting the target shell identity.
        del ticket['installed_manifest']['desktop_shell_version']
        copied.atomic_json(ticket_path, ticket)
        with self.assertRaisesRegex(copied.SafeFailure, 'identity mismatch'):
            copied.verify_ticket(ticket_path)

    def test_helper_reaches_ready_from_raw_source_without_installing(self):
        _manager, prepared, copied = self.prepare()
        ticket_path = Path(prepared['ticket_path'])
        ticket = copied.read_json(ticket_path)
        txdir = ticket_path.parent
        copied.atomic_json(txdir / 'cancel.json', {'schema': 'ai-hub-update-cancel-v1',
                                                 'transaction_id': ticket['transaction_id']})
        with mock.patch.object(copied, 'process_identity', return_value=({'pid': os.getpid(), 'start_filetime': '999'}, None)), \
             mock.patch.object(copied, 'hold_identity', side_effect=lambda identity, *_: (identity, None)):
            self.assertEqual(copied.execute(ticket_path), 'cancelled')
        self.assertEqual(copied.read_json(txdir / 'ready.json')['schema'], 'ai-hub-update-ready-v1')
        self.assertEqual(copied.read_json(txdir / 'journal.json')['files'], [])
        self.assertEqual((self.root / 'manifest.json').read_bytes(), self.raw)
        for name, contents in self.files.items():
            self.assertEqual(self.root.joinpath(*name.split('/')).read_bytes(), contents)


if __name__ == '__main__':
    unittest.main()
