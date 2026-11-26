"""Temporary file fixtures; preview must never delete, move or rewrite assets."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from aihub import config, file_lifecycle as fl


class FileLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yaohe-lifecycle-')
        self.base = Path(self.temp.name)
        self.root = self.base / 'Workspace'
        self.root.mkdir()
        self.data = self.base / 'app' / 'data'
        self.patches = [patch.object(config, 'DATA_DIR', str(self.data)), patch.object(config, 'APP_DIR', str(self.base / 'app'))]
        for p in self.patches:
            p.start()
        self.cfg = {'ai_root': str(self.root)}
        fl._PREVIEWS.clear()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def file(self, relative, content=b'fixture'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def register(self, ident, kind, path, **extra):
        record = {'id': ident, 'kind': kind, 'name': ident, 'path': str(path), 'purpose': 'unknown', 'state': 'completed', **extra}
        p = fl.registration_preview(self.cfg, record)
        fl.registration_save(self.cfg, p['token'])
        return record

    def candidate(self, path):
        self.register('project', 'project', self.root)
        self.register('task', 'task', path.parent)
        self.register('tool', 'tool', self.root)
        return self.register('artifact', 'artifact', path, purpose='temporary', state='expired', project_id='project', task_id='task', tool_id='tool', expires_at='2000-01-01T00:00:00Z')

    def item(self, path):
        return next(r for r in fl.inventory(self.cfg)['items'] if r['path'] == str(path))

    def test_nested_files_unknown_skip_and_no_asset_mutation(self):
        path = self.file('project/work/deep/data.bin')
        before = path.stat()
        r = self.item(path)
        self.assertEqual(r['relative_path'], str(path.relative_to(self.root)))
        self.assertEqual(r['decision'], 'skip')
        self.assertEqual(path.read_bytes(), b'fixture')
        self.assertEqual(before.st_mtime_ns, path.stat().st_mtime_ns)
        self.assertFalse(self.data.exists())

    def test_owned_expired_file_has_separate_logical_allocation_and_release(self):
        path = self.file('project/work/a.tmp', b'1234567')
        self.candidate(path)
        with patch.object(fl, '_allocated', return_value=4096):
            result = fl.inventory(self.cfg)
        item = result['items'][0]
        self.assertEqual(item['decision'], 'candidate')
        self.assertEqual(result['summary']['candidate_logical_bytes'], 7)
        self.assertEqual(result['summary']['estimated_releasable_bytes'], 4096)
        self.assertEqual(result['summary']['actual_released_bytes'], 0)
        self.assertIsNone(result['summary']['volume_free_space_change_bytes'])
        self.assertFalse(result['automatic_cleanup'])
        self.assertTrue(path.exists())

    def test_unknown_allocation_is_not_reported_as_logical_release(self):
        path = self.file('cache/tmp.dat')
        self.candidate(path)
        with patch.object(fl, '_allocated', return_value=None):
            result = fl.inventory(self.cfg)
        self.assertIsNone(result['summary']['estimated_releasable_bytes'])

    @unittest.skipUnless(os.name == 'nt', 'Windows allocation API')
    def test_windows_allocation_is_not_uncompressed_file_length(self):
        path=self.file('cache/allocated.tmp',b'x'*65537)
        actual=fl._allocated(path,path.lstat())
        self.assertIsNotNone(actual)
        self.assertNotEqual(actual,65537)
        self.assertGreaterEqual(actual,65537)

    def test_active_task_blocks_expired_artifact(self):
        path = self.file('cache/a.tmp'); self.candidate(path)
        self.register('task', 'task', path.parent, state='active')
        item = self.item(path)
        self.assertEqual(item['decision'], 'protected')
        self.assertIn('active_task_or_owner', item['reasons'])

    def test_source_work_model_configuration_never_become_candidates(self):
        for suffix in ('.py', '.png', '.md', '.safetensors', '.sqlite3', '.zip', '.xmind', '.canvas', '.sav', '.rpp', '.drp'):
            path = self.file('cache/a' + suffix); self.candidate(path)
            self.assertEqual(self.item(path)['decision'], 'protected')

    def test_existing_collaboration_activity_blocks_manual_completed_state(self):
        path = self.file('task/work/cache.tmp'); self.candidate(path)
        con = sqlite3.connect(self.data / 'collaboration.sqlite3')
        con.execute('CREATE TABLE tasks(root TEXT,status TEXT,paths TEXT)')
        con.execute('INSERT INTO tasks VALUES (?,?,?)', (config._key(self.root), 'active', json.dumps({'temp':str(path.parent)})))
        con.commit();con.close()
        self.assertIn('active_collaboration_task', self.item(path)['reasons'])

    def test_damaged_activity_ledger_fails_closed(self):
        path = self.file('cache/a.tmp'); self.candidate(path)
        (self.data / 'collaboration.sqlite3').write_bytes(b'corrupt')
        self.assertIn('activity_ledger_unavailable', self.item(path)['reasons'])

    def test_unknown_git_state_and_unpushed_commits_protected(self):
        path = self.file('repo/cache/a.tmp'); self.candidate(path)
        (self.root / 'repo' / '.git').mkdir()
        with patch.object(fl, '_git_state', return_value={'path':str(self.root/'repo'),'dirty':None,'ahead':None}):
            item = fl.inventory(self.cfg, str(path.parent))['items'][0]
        self.assertEqual(item['decision'], 'protected')
        self.assertIn('uncommitted_or_unverified_repository', item['reasons'])
        self.assertIn('unpushed_or_unverified_repository', item['reasons'])

    def test_git_probe_is_readonly_and_does_not_fetch(self):
        with patch.object(fl.subprocess, 'run', side_effect=[subprocess.CompletedProcess([],0,' M main.py\n',''),subprocess.CompletedProcess([],0,'2\n','')]) as run:
            result=fl._git_state(self.root)
        self.assertTrue(result['dirty']);self.assertEqual(result['ahead'],2)
        self.assertFalse(result['remote_fetched'])
        self.assertTrue(all('--no-optional-locks' in call.args[0] for call in run.call_args_list))
        self.assertTrue(all('fetch' not in call.args[0] for call in run.call_args_list))

    def test_hardlinks_are_protected_and_allocation_deduplicated(self):
        path = self.file('cache/a.tmp'); self.candidate(path)
        alias = path.with_name('alias.tmp')
        os.link(path, alias)
        with patch.object(fl, '_allocated', return_value=4096):
            result = fl.inventory(self.cfg)
        self.assertEqual(result['summary']['logical_bytes'],14)
        self.assertEqual(result['summary']['allocated_bytes_known'],4096)
        self.assertTrue(all('shared_hardlink' in item['reasons'] for item in result['items']))

    def test_modified_registered_file_is_protected(self):
        path = self.file('cache/a.tmp'); self.candidate(path)
        path.write_bytes(b'changed file content')
        self.assertIn('registration_identity_changed', self.item(path)['reasons'])

    def test_reparse_entry_skipped_without_following(self):
        path = self.file('junction/secret.tmp')
        original = config._is_reparse
        inode = path.parent.stat().st_ino
        with patch.object(config, '_is_reparse', side_effect=lambda info: info.st_ino==inode or original(info)):
            result = fl.inventory(self.cfg)
        self.assertEqual(result['items'], [])
        self.assertEqual(result['issues'][0]['reason'], 'reparse_point_skipped')

    def test_protected_skill_tree_skipped(self):
        self.file('skills/new-in-progress/unknown.tmp')
        result = fl.inventory(self.cfg)
        self.assertEqual(result['items'], [])
        self.assertEqual(result['issues'][0]['reason'], 'protected_directory_skipped')

    def test_scope_escape_and_parent_reparse_rejected(self):
        outside=self.base/'outside';outside.mkdir()
        with self.assertRaises(ValueError):fl.inventory(self.cfg,str(outside))
        with self.assertRaises(ValueError):fl.inventory(self.cfg,str(self.root/'..'/'outside'))
        with patch.object(config,'_check_ancestors',side_effect=ValueError('junction')):
            with self.assertRaises(ValueError):fl.inventory(self.cfg)

    def test_bounded_recursive_preview_marks_incomplete(self):
        for n in range(10):self.file('deep/folder/%s.tmp'%n)
        result=fl.inventory(self.cfg,max_entries=4)
        self.assertTrue(result['truncated']);self.assertLessEqual(result['examined_entries'],4)

    def test_registration_preview_cas_backup_and_stale_file(self):
        path=self.file('cache/a.tmp')
        record={'id':'p','kind':'project','name':'Project','path':str(self.root),'purpose':'unknown','state':'completed'}
        a=fl.registration_preview(self.cfg,record);b=fl.registration_preview(self.cfg,record)
        self.assertFalse(self.data.exists())
        fl.registration_save(self.cfg,a['token'])
        with self.assertRaises(ValueError):fl.registration_save(self.cfg,b['token'])
        self.register('p','project',self.root,name='Revised')
        self.assertEqual(len(list((self.data/'file-lifecycle-backups').glob('*.json'))),1)
        record.update(id='f',kind='artifact',path=str(path))
        a=fl.registration_preview(self.cfg,record);path.write_bytes(b'changed')
        with self.assertRaises(ValueError):fl.registration_save(self.cfg,a['token'])

    def test_registration_rejects_wrong_link_type_and_workspace(self):
        path=self.file('cache/a.tmp');self.register('task','task',path.parent)
        with self.assertRaises(ValueError):self.register('a','artifact',path,project_id='task')
        record={'id':'p','kind':'project','name':'Project','path':str(self.root),'purpose':'unknown','state':'completed'}
        preview=fl.registration_preview(self.cfg,record)
        other=self.base/'Other';other.mkdir()
        with self.assertRaises(ValueError):fl.registration_save({'ai_root':str(other)},preview['token'])

    def test_corrupt_metadata_is_preserved(self):
        self.data.mkdir(parents=True);path=self.data/'file-lifecycle.json';path.write_bytes(b'broken')
        with self.assertRaises(ValueError):fl.registrations(self.cfg)
        self.assertEqual(path.read_bytes(),b'broken')

    def test_exclusive_writer_lock_blocks_and_preserves_pending_registration(self):
        record={'id':'p','kind':'project','name':'Project','path':str(self.root),'purpose':'unknown','state':'completed'}
        preview=fl.registration_preview(self.cfg,record)
        with fl._write_lock():
            with self.assertRaisesRegex(ValueError,'另一个进程'):
                fl.registration_save(self.cfg,preview['token'])
        self.assertFalse((self.data/'file-lifecycle.json').exists())
        fl.registration_save(self.cfg,preview['token'])
        self.assertEqual(fl.registrations(self.cfg)['records'][0]['id'],'p')

    def test_oversized_write_preserves_readable_ledger_before_backup(self):
        self.register('p','project',self.root)
        ledger=self.data/'file-lifecycle.json';before=ledger.read_bytes()
        record={'id':'p','kind':'project','name':'A'*200,'path':str(self.root),'purpose':'unknown','state':'completed'}
        preview=fl.registration_preview(self.cfg,record)
        with patch.object(fl,'MAX_LEDGER_BYTES',len(before)+1):
            with self.assertRaisesRegex(ValueError,'容量超过上限'):
                fl.registration_save(self.cfg,preview['token'])
        self.assertEqual(ledger.read_bytes(),before)
        self.assertFalse((self.data/'file-lifecycle-backups').exists())


if __name__ == '__main__':
    unittest.main()
