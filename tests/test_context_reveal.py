import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from aihub import api, capabilities


class ContextReveal(unittest.TestCase):
    def test_requires_current_list_and_existing_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'workflow.json'
            path.write_text('{}')
            cfg = {'ai_root':directory}
            with mock.patch.object(api.management, 'workflows', return_value={'items':[{'path':str(path)}]}), mock.patch('subprocess.Popen') as launch:
                result = api.context_reveal(None, cfg, {}, {'kind':'workflow','path':str(path)})
                self.assertEqual(result[0], 200 if os.name == 'nt' else 409)
                if os.name == 'nt':
                    self.assertEqual(launch.call_args.args[0][-2:], ['/select,',str(path)])
                launch.reset_mock()
                self.assertEqual(api.context_reveal(None, {}, {}, {'kind':'workflow','path':str(path.parent/'other.json')})[0],403)
                self.assertFalse(launch.called)
                path.unlink()
                self.assertEqual(api.context_reveal(None, cfg, {}, {'kind':'workflow','path':str(path)})[0],409)
                self.assertFalse(launch.called)

    def test_skill_is_authorized_by_discovery_not_arbitrary_absolute_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'SKILL.md'
            path.write_text('---\nname: test\n---\n')
            with mock.patch.object(capabilities,'discover',return_value={'suggestions':[]}),mock.patch('subprocess.Popen') as launch:
                self.assertEqual(api.context_reveal(None,{}, {}, {'kind':'skill','path':str(path)})[0],403)
                launch.assert_not_called()

    def test_bad_types_and_unknown_kinds_never_launch(self):
        with mock.patch('subprocess.Popen') as launch:
            for body in (None,{}, {'kind':'file','path':os.path.abspath(__file__)},{'kind':'skill','path':['bad']},{'kind':'skill','path':'relative'}):
                self.assertEqual(api.context_reveal(None,{}, {},body)[0],400)
            launch.assert_not_called()

    def test_indexed_empty_and_subdirectory_only_dirs_require_dirs_identity(self):
        class IndexedDB:
            def __init__(self, directory_paths):
                self.directory_paths = set(directory_paths)
                self.directory_lookups = []

            def one(self, sql, params=()):
                if 'FROM dirs' in sql:
                    self.directory_lookups.append(params[0])
                    if params[0] in self.directory_paths:
                        return {'path': params[0]}
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root/'empty'
            parent = root/'only-subdirectories'
            child = parent/'child'
            empty.mkdir()
            child.mkdir(parents=True)
            unindexed = root/'unindexed'
            unindexed.mkdir()
            cfg = {'ai_root': str(root)}
            db = IndexedDB({str(empty), str(parent), str(child)})
            with mock.patch('subprocess.Popen') as launch:
                for path in (empty, parent):
                    result = api.context_reveal(db, cfg, {}, {'kind': 'indexed', 'path': str(path)})
                    self.assertEqual(result[0], 200 if os.name == 'nt' else 409)
                self.assertEqual(api.context_reveal(db, cfg, {}, {'kind': 'indexed', 'path': str(unindexed)})[0], 403)
                if os.name == 'nt':
                    self.assertEqual(launch.call_count, 2)
                else:
                    launch.assert_not_called()
            self.assertIn(str(empty), db.directory_lookups)
            self.assertIn(str(parent), db.directory_lookups)
            self.assertIn(str(unindexed), db.directory_lookups)
