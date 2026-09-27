import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

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
