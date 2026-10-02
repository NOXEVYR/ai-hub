import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

spec = importlib.util.spec_from_file_location('update_feed', Path(__file__).resolve().parents[1] / 'tools/update_feed.py')
feed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feed)


class FeedPublicationTests(unittest.TestCase):
    def package(self, root, **overrides):
        path = Path(root) / 'AI-Hub-v2.12.0-Windows-x64.zip'
        value = dict(version='2.12.0', kind='Windows-x64', user_data_included=False, **overrides)
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('AI-Hub/manifest.json', json.dumps(value))
        return path

    def test_published_feed_binds_exact_commit_and_package_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.package(root)
            value = feed.create(path, 'a' * 40, '程序更新')
            self.assertEqual(value['commit'], 'a' * 40)
            self.assertEqual(value['package'], {'bytes':path.stat().st_size, 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
            self.assertNotIn('url', value['package'])
            for invalid in ('main', 'https://evil.invalid/a', 'a' * 39):
                with self.assertRaises(ValueError): feed.create(path, invalid, 'notes')

    def test_source_package_or_mismatched_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.package(root)
            for field, invalid in (('version','9.0.0'), ('kind','Source'), ('user_data_included',True)):
                with zipfile.ZipFile(path, 'w') as archive:
                    data = {'version':'2.12.0','kind':'Windows-x64','user_data_included':False, field:invalid}
                    archive.writestr('AI-Hub/manifest.json', json.dumps(data))
                with self.assertRaises(ValueError): feed.create(path, 'a' * 40, 'notes')


class BoundFeedPublicationTests(unittest.TestCase):
    def package(self, root, *, files=None, manifest_change=None, extras=None):
        path = Path(root) / 'AI-Hub-v2.13.6-Windows-x64.zip'
        files = files if files is not None else {'AI Hub.exe': b'isolated exe fixture', 'server.py': b'print("server")\n'}
        manifest = {'version':'2.13.6', 'kind':'Windows-x64', 'user_data_included':False,
                    'files':[{'path':name, 'bytes':len(data), 'sha256':hashlib.sha256(data).hexdigest()}
                             for name, data in sorted(files.items())]}
        if manifest_change:
            manifest_change(manifest)
        # Whitespace and CRLF deliberately distinguish the original bytes from
        # a parsed-and-reserialized JSON document.
        raw_manifest = (json.dumps(manifest, indent=4).replace('\n', '\r\n') + '\r\n').encode('utf-8')
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr('AI-Hub/' + name, data)
            archive.writestr('AI-Hub/manifest.json', raw_manifest)
            for name, data in (extras or {}).items():
                archive.writestr('AI-Hub/' + name, data)
        return path, raw_manifest

    def test_bound_feed_binds_package_and_original_manifest_bytes(self):
        from aihub import app_update
        with tempfile.TemporaryDirectory() as root:
            path, raw_manifest = self.package(root)
            value = feed.create(path, 'b' * 40, '绑定更新包', bound=True)
            package_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(value['schema'], 'ai-hub-update-v2')
            self.assertEqual(value['platform'], 'windows')
            self.assertEqual(value['architecture'], 'x64')
            self.assertEqual(value['build_id'], '2.13.6-' + package_sha[:16])
            self.assertEqual(value['commit'], 'b' * 40)
            self.assertEqual(value['min_updater_version'], '2.13.5')
            self.assertEqual(value['package'], {'bytes':path.stat().st_size, 'sha256':package_sha,
                                              'manifest_sha256':hashlib.sha256(raw_manifest).hexdigest()})
            reserialized = json.dumps(json.loads(raw_manifest)).encode('utf-8')
            self.assertNotEqual(value['package']['manifest_sha256'], hashlib.sha256(reserialized).hexdigest())
            accepted = app_update._validate_feed(json.dumps(value).encode('utf-8'), '2.13.5')
            self.assertEqual(accepted['release_id'], value['build_id'])
            self.assertIn('/' + 'b' * 40 + '/', accepted['package_url'])

    def test_bound_feed_rejects_manifest_only_legacy_fixture(self):
        with tempfile.TemporaryDirectory() as root:
            path = FeedPublicationTests().package(root)
            legacy = feed.create(path, 'a' * 40, 'legacy fixture')
            self.assertEqual(legacy['schema'], 'ai-hub-update-v1')
            self.assertEqual(legacy['min_updater_version'], '2.12.0')
            self.assertNotIn('manifest_sha256', legacy['package'])
            self.assertNotIn('build_id', legacy)
            with self.assertRaises(ValueError):
                feed.create(path, 'a' * 40, 'incomplete package', bound=True)

    def test_bound_feed_rejects_tampered_hashes_sizes_and_identity(self):
        changes = {
            'hash': lambda manifest: manifest['files'][0].update(sha256='0' * 64),
            'size': lambda manifest: manifest['files'][0].update(bytes=999),
            'version': lambda manifest: manifest.update(version='2.13.7'),
            'platform': lambda manifest: manifest.update(kind='Source'),
            'private_data': lambda manifest: manifest.update(user_data_included=True),
        }
        with tempfile.TemporaryDirectory() as root:
            for name, change in changes.items():
                with self.subTest(name=name):
                    path, _ = self.package(root, manifest_change=change)
                    with self.assertRaises(ValueError):
                        feed.create(path, 'b' * 40, 'invalid package', bound=True)

    def test_bound_feed_rejects_unlisted_protected_and_missing_desktop_files(self):
        variants = ({'extras':{'frontend/extra.js':b'not listed'}},
                    {'files':{'AI Hub.exe':b'exe', 'data/private.db':b'private'}},
                    {'files':{'AI Hub.exe':b'exe', '../escape.py':b'escape'}},
                    {'files':{'server.py':b'no desktop executable'}})
        with tempfile.TemporaryDirectory() as root:
            for variant in variants:
                with self.subTest(variant=variant):
                    path, _ = self.package(root, **variant)
                    with self.assertRaises(ValueError):
                        feed.create(path, 'b' * 40, 'invalid package', bound=True)

    def test_cli_defaults_to_bound_v2_and_legacy_flag_keeps_v1(self):
        script = Path(__file__).resolve().parents[1] / 'tools/update_feed.py'
        with tempfile.TemporaryDirectory() as root:
            path, _ = self.package(root)
            output = Path(root) / 'candidate.json'
            command = [str(script), '--package', str(path),
                       '--commit', 'b' * 40, '--notes', 'CLI package binding', '--output', str(output)]
            with mock.patch.object(sys, 'argv', command):
                feed.main()
            self.assertEqual(json.loads(output.read_text(encoding='utf-8')), feed.create(path, 'b' * 40, 'CLI package binding', bound=True))
            with mock.patch.object(sys, 'argv', command + ['--legacy']):
                feed.main()
            self.assertEqual(json.loads(output.read_text(encoding='utf-8')), feed.create(path, 'b' * 40, 'CLI package binding'))

    def test_cli_validation_failure_preserves_existing_output(self):
        script = Path(__file__).resolve().parents[1] / 'tools/update_feed.py'
        with tempfile.TemporaryDirectory() as root:
            path, _ = self.package(root, extras={'frontend/extra.js':b'unlisted'})
            output = Path(root) / 'candidate.json'
            output.write_bytes(b'previous published feed fixture')
            command = [str(script), '--package', str(path), '--commit', 'b' * 40,
                       '--notes', 'invalid package', '--output', str(output)]
            with mock.patch.object(sys, 'argv', command), self.assertRaises(ValueError):
                feed.main()
            self.assertEqual(output.read_bytes(), b'previous published feed fixture')
