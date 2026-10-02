"""Synthetic sources only. Never recycle real workspaces or native memories."""
import json
import contextlib
import os
import sqlite3
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from aihub import harnesses, config, collaboration_api, collaboration_maintenance as maintenance


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihub-maintenance-')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(maintenance._clear_scan_enumerators)
        self.base = Path(self.temp.name)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        self.data = self.base / 'application/data'
        self.data.mkdir(parents=True)
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True}
        for name, value in [('DATA_DIR', str(self.data)), ('APP_DIR', str(self.data.parent))]:
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        # This fixture explicitly opts into the four compatibility recipes.
        for identifier in harnesses.BUILTIN_IDS:
            harnesses.save(self.cfg, {'id': identifier, 'revision': 0, 'connection_mode': 'mcp_stdio'})
        patch = mock.patch.object(maintenance, '_root', side_effect=lambda cfg: cfg['ai_root'])
        patch.start()
        self.addCleanup(patch.stop)

    def source(self, root=None):
        return maintenance.add_source(self.cfg, {'path': str(root or self.root), 'label': '合成来源', 'tool': 'codex'})

    def replaced_source(self):
        source_path = self.base / 'reports'
        source_path.mkdir()
        (source_path / 'old.md').write_text('old')
        source = self.source(source_path)
        maintenance.scan_source(self.cfg, {'source_id': source['id']})
        source_path.rename(self.base / 'reports-old')
        source_path.mkdir()
        (source_path / 'new.md').write_text('new')
        return source, source_path

    def test_reconfirmation_is_explicit_root_bound_and_retains_history_until_complete(self):
        source, path = self.replaced_source()
        readded = self.source(path)
        self.assertEqual(readded['id'], source['id'])
        with self.assertRaisesRegex(ValueError, '身份改变'):
            maintenance.scan_source(self.cfg, {'source_id': source['id']})
        before = maintenance.list_sources(self.cfg)
        preview = collaboration_api.execute(self.cfg, 'source_reconfirm_preview',
                    {'source_id': source['id'], '_workspace_root': str(self.root)})
        self.assertTrue(preview['can_apply'])
        self.assertNotEqual(preview['previous_identity'], preview['current_identity'])
        self.assertEqual(maintenance.list_sources(self.cfg), before)
        self.assertEqual(preview['retained_inventory_count'], 1)
        with self.assertRaises(ValueError):
            collaboration_api.execute(self.cfg, 'source_reconfirm_apply', {'token': preview['token']})
        result = collaboration_api.execute(self.cfg, 'source_reconfirm_apply',
                    {'token': preview['token'], '_workspace_root': str(self.root)})
        self.assertTrue(result['applied'])
        self.assertIsNone(maintenance.list_sources(self.cfg)['items'][0]['scanned_at'])
        self.assertEqual(maintenance.inventory(self.cfg)['items'][0]['historical'], 1)
        with mock.patch.object(maintenance, 'MAX_ITEMS', 0):
            maintenance.scan_source(self.cfg, {'source_id': source['id']})
        self.assertEqual(maintenance.inventory(self.cfg)['total'], 1)
        maintenance.scan_source(self.cfg, {'source_id': source['id']})
        rows = maintenance.inventory(self.cfg)['items']
        self.assertEqual([(r['title'], r['historical']) for r in rows], [('new.md', 0)])
        with self.assertRaises(ValueError):
            maintenance.source_reconfirm_apply(self.cfg, {'token': preview['token']})

    def test_reconfirmation_rejects_expiry_replacement_root_switch_revision_and_lease(self):
        source, path = self.replaced_source()
        for failure in ('expiry', 'replacement', 'root', 'revision', 'lease', 'scan'):
            with self.subTest(failure=failure):
                preview = maintenance.source_reconfirm_preview(self.cfg, {'source_id': source['id']})
                cfg = self.cfg
                if failure == 'expiry':
                    maintenance._SOURCE_PREVIEWS[preview['token']]['expires_at'] = 0
                elif failure == 'replacement':
                    path.rename(self.base / 'reports-replaced-again')
                    path.mkdir()
                elif failure == 'root':
                    other = self.base / 'other'
                    other.mkdir(exist_ok=True)
                    cfg = {**cfg, 'ai_root': str(other)}
                else:
                    with maintenance._db() as conn, conn:
                        if failure == 'revision':
                            conn.execute('UPDATE sources SET revision=revision+1 WHERE id=?', (source['id'],))
                        elif failure == 'scan':
                            conn.execute("UPDATE source_scan_progress SET state=state || ' ' WHERE source_id=?", (source['id'],))
                        else:
                            conn.execute('UPDATE source_scan_progress SET lease_until=? WHERE source_id=?', (time.time()+100, source['id']))
                with self.assertRaises(ValueError):
                    maintenance.source_reconfirm_apply(cfg, {'token': preview['token']})
                self.assertEqual(maintenance.inventory(self.cfg)['total'], 1)
                with maintenance._db() as conn, conn:
                    conn.execute('UPDATE source_scan_progress SET lease_until=0 WHERE source_id=?', (source['id'],))
        with maintenance._db() as conn, conn:
            conn.execute('UPDATE source_scan_progress SET lease_until=? WHERE source_id=?', (time.time()+100, source['id']))
        self.assertFalse(maintenance.source_reconfirm_preview(self.cfg, {'source_id': source['id']})['can_apply'])
        for action in ('source_reconfirm_preview', 'source_reconfirm_apply'):
            with self.assertRaises(PermissionError):
                collaboration_api.execute(self.cfg, action, {'_workspace_root': str(self.root)}, actor='mcp')

    def test_legacy_scan_identity_migration_uses_sqlite_backup_and_remains_scannable(self):
        path = self.base / 'legacy-reports'
        path.mkdir()
        report = path / 'report.md'
        report.write_text('legacy')
        identity = maintenance._source_identity(str(path))
        database = self.data / 'collaboration-maintenance.sqlite3'
        with contextlib.closing(sqlite3.connect(database)) as conn, conn:
            conn.executescript('''CREATE TABLE sources(id TEXT PRIMARY KEY,root TEXT,path TEXT,label TEXT,tool TEXT,
              created_at REAL,scanned_at REAL,file_count INTEGER,bytes INTEGER,truncated INTEGER);
              CREATE TABLE inventory(source_id TEXT,path TEXT,title TEXT,size INTEGER,mtime REAL,category TEXT,
              PRIMARY KEY(source_id,path));
              CREATE TABLE source_scan_progress(source_id TEXT PRIMARY KEY,root TEXT,state TEXT,lease_until REAL);''')
            conn.execute('INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?)', ('legacy', str(self.root), str(path), 'legacy', 'codex', 1, 1, 1, 6, 0))
            conn.execute('INSERT INTO inventory VALUES(?,?,?,?,?,?)', ('legacy', str(report), 'report.md', 6, 1, 'report'))
            conn.execute('INSERT INTO source_scan_progress VALUES(?,?,?,?)', ('legacy', str(self.root), json.dumps({'identity': identity, 'queue': []}), 0))
        self.assertEqual(maintenance.list_sources(self.cfg)['items'][0]['identity'], identity)
        backups = list((self.data / 'maintenance-schema-backups').glob('*.sqlite3'))
        self.assertEqual(len(backups), 1)
        with contextlib.closing(sqlite3.connect(backups[0])) as conn:
            self.assertNotIn('identity', {row[1] for row in conn.execute('PRAGMA table_info(sources)')})
            self.assertEqual(conn.execute('SELECT count(*) FROM inventory').fetchone()[0], 1)
        self.assertTrue(maintenance.scan_source(self.cfg, {'source_id': 'legacy'})['progress']['complete'])

    def test_read_only_inventory_excludes_native_state_secrets_links_and_weights(self):
        (self.root / '验收报告.md').write_text('keep original', encoding='utf-8')
        (self.root / 'secret-token.txt').write_text('never read')
        (self.root / 'model.safetensors').write_bytes(b'not a model')
        (self.root / '.codex').mkdir()
        (self.root / '.codex/native-memory.md').write_text('native memory')
        (self.root / 'Temp').mkdir()
        (self.root / 'Temp/scratch.txt').write_text('keep too')
        hard = self.root / 'linked-report.md'
        os.link(self.root / '验收报告.md', hard)
        source = self.source()
        result = maintenance.scan_source(self.cfg, {'source_id': source['id']})
        self.assertEqual([r['title'] for r in result['items']], ['scratch.txt'])
        self.assertEqual(result['items'][0]['category'], 'temp_candidate')
        self.assertFalse(result['retention_authority'])
        self.assertEqual((self.root / '.codex/native-memory.md').read_text(), 'native memory')
        self.assertTrue(hard.exists())

    def test_root_partition_partial_scan_preserves_prior_rows(self):
        for number in range(3):
            (self.root / ('report%d.md' % number)).write_text(str(number))
        source = self.source()
        first = maintenance.scan_source(self.cfg, {'source_id': source['id']})
        self.assertEqual(len(first['items']), 3)
        with mock.patch.object(maintenance, 'MAX_ITEMS', 1):
            second = maintenance.scan_source(self.cfg, {'source_id': source['id']})
        self.assertTrue(second['truncated'])
        self.assertEqual(maintenance.inventory(self.cfg)['total'], 3)
        other = {**self.cfg, 'ai_root': str(self.base / 'other')}
        self.assertEqual(maintenance.list_sources(other)['items'], [])
        self.assertEqual(maintenance.inventory(other)['total'], 0)
        with self.assertRaises(ValueError):
            maintenance.scan_source(other, {'source_id': source['id']})

    def test_report_classification_ignores_source_parent_names(self):
        expected = {'验收报告.md': 'report', 'knowledge/notes.md': 'knowledge',
                    'Temp/scratch.txt': 'temp_candidate', 'plain.txt': 'other_text'}
        for parent in ('Temp', '报告归档', 'Knowledge', 'ordinary'):
            source_root = self.base / parent / 'selected-source'
            before = {}
            for name in expected:
                path = source_root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(('original ' + name).encode('utf-8'))
                before[name] = (path.read_bytes(), path.stat().st_mtime_ns)
            source = self.source(source_root)
            with mock.patch.object(maintenance.recycle, 'recycle_file') as recycler:
                result = maintenance.scan_source(self.cfg, {'source_id': source['id']})
                recycler.assert_not_called()
            with self.subTest(parent=parent):
                self.assertEqual({Path(row['path']).relative_to(source_root).as_posix(): row['category']
                                  for row in result['items']}, expected)
                self.assertFalse(result['retention_authority'])
                self.assertTrue(result['read_only'])
                self.assertEqual({name: ((source_root / name).read_bytes(),
                                        (source_root / name).stat().st_mtime_ns)
                                  for name in expected}, before)

    def test_settings_validation_and_mcp_cannot_expand_authority(self):
        self.assertTrue(maintenance.policy(self.cfg)['enabled'])
        result = maintenance.save_policy(self.cfg, {'enabled': False, 'days': 30})
        self.assertFalse(result['enabled'])
        self.assertEqual(result['days'], 30)
        self.assertEqual(result['days_apply_to'], 'new_artifacts')
        for body in ({'enabled': 'yes', 'days': 7}, {'enabled': True, 'days': True}, {'enabled': True, 'days': 0}):
            with self.assertRaises(ValueError):
                maintenance.save_policy(self.cfg, body)
        for action in ('source_add', 'source_scan', 'retention_policy', 'retention_run'):
            with self.assertRaises(PermissionError):
                maintenance.execute(self.cfg, action, {}, actor='mcp')

    def test_source_internal_directories_rejected(self):
        state = self.root / '.workbuddy'
        state.mkdir()
        with self.assertRaises(ValueError):
            self.source(state)
        self.assertEqual(maintenance.list_sources(self.cfg)['items'], [])

    def test_scheduled_run_is_leased_rate_limited_and_failure_preserves(self):
        core = mock.Mock()
        core.retention_count.return_value = 1
        core.retention_candidates.return_value = {'items': [{'id': 'x'}]}
        core.recycle_candidate.return_value = {'id': 'x', 'status': 'error', 'error': 'recycle denied'}
        with mock.patch.object(maintenance, '_core', return_value=core):
            first = maintenance.run_retention(self.cfg, scheduled=True)
            second = maintenance.run_retention(self.cfg, scheduled=True)
            self.assertEqual(first['recycled'], 0)
            self.assertEqual(first['errors'], ['recycle denied'])
            self.assertTrue(second['skipped'])
            self.assertEqual(core.recycle_candidate.call_count, 1)
            self.assertIs(core.recycle_candidate.call_args.args[2], maintenance.recycle.recycle_file)
            self.assertEqual(maintenance.policy(self.cfg)['last_run']['errors'], ['recycle denied'])
            maintenance.save_policy(self.cfg, {'enabled': False, 'days': 7})
            self.assertTrue(maintenance.run_retention(self.cfg, scheduled=True)['skipped'])

    def test_protected_first_batch_cannot_starve_later_candidates(self):
        core = mock.Mock()
        core.retention_count.return_value = 101
        core.retention_candidates.side_effect = lambda cfg, **kw: [{'id': str(n)} for n in range(kw['offset'], min(101, kw['offset'] + kw['limit']))]
        core.recycle_candidate.side_effect = lambda cfg, ident, recycler, **kw: {'id': ident, 'status': 'recycled' if ident == '100' else 'protected'}
        with mock.patch.object(maintenance, '_core', return_value=core):
            first = maintenance.run_retention(self.cfg)
            second = maintenance.run_retention(self.cfg)
        self.assertEqual(first['checked'], 100)
        self.assertEqual(first['next_offset'], 100)
        self.assertEqual(second['checked'], 1)
        self.assertEqual(second['recycled'], 1)
        self.assertEqual(second['next_offset'], 0)

    def test_manual_retention_checks_only_the_explicit_preview_candidates(self):
        core = mock.Mock()
        core.recycle_candidate.return_value = {'id': 'visible-candidate', 'status': 'protected'}
        with mock.patch.object(maintenance, '_core', return_value=core):
            result = maintenance.execute(self.cfg, 'retention_run', {'artifact_ids': ['visible-candidate']})
        self.assertEqual(result['checked'], 1)
        core.retention_candidates.assert_not_called()
        self.assertEqual(core.recycle_candidate.call_args.args[1], 'visible-candidate')
        for values in ([], ['a', 'a'], [None], ['a'] * 1001):
            with self.subTest(values=str(values)[:30]), self.assertRaises(ValueError):
                maintenance.run_retention(self.cfg, artifact_ids=values)
        with self.assertRaises(ValueError):
            maintenance.run_retention(self.cfg, scheduled=True, artifact_ids=['a'])

    def test_scheduler_stops_without_processing_unmanaged_workspace(self):
        with mock.patch.object(maintenance, 'run_retention') as run:
            stop, thread = maintenance.start_scheduler({'workspace_managed': False})
            stop.set()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            run.assert_not_called()

    def test_resume_is_fair_across_projects_and_full_round_removes_only_stale_index(self):
        for project in ('a', 'b', 'c'):
            folder = self.root / '2026-09-25' / project / 'outputs'
            folder.mkdir(parents=True)
            for number in range(3):
                (folder / ('report%d.md' % number)).write_text(project)
        source = self.source()
        with mock.patch.object(maintenance, 'MAX_ITEMS', 1):
            first = maintenance.scan_source(self.cfg, {'source_id': source['id']})
            second = maintenance.scan_source(self.cfg, {'source_id': source['id']})
            self.assertTrue(second['progress']['resumed'])
            self.assertNotEqual(Path(first['items'][0]['path']).parent, Path(second['items'][0]['path']).parent)
            for _ in range(15):
                result = maintenance.scan_source(self.cfg, {'source_id': source['id']})
                if not result['truncated']:
                    break
        self.assertFalse(result['truncated'])
        self.assertEqual(maintenance.inventory(self.cfg)['total'], 9)
        deleted = self.root / '2026-09-25/a/outputs/report0.md'
        deleted.unlink()
        with mock.patch.object(maintenance, 'MAX_ITEMS', 1):
            maintenance.scan_source(self.cfg, {'source_id': source['id']})
        self.assertEqual(maintenance.inventory(self.cfg)['total'], 9)
        maintenance.scan_source(self.cfg, {'source_id': source['id']})
        self.assertEqual(maintenance.inventory(self.cfg)['total'], 8)

    def test_flat_directory_larger_than_entry_budget_eventually_completes(self):
        expected = set()
        for name in ('z.md', 'a.md', 'q.md', 'b.md', 'r.md'):
            path = self.root / name
            path.write_text('synthetic report')
            expected.add(str(path))
        source = self.source()
        with mock.patch.object(maintenance, 'MAX_FILES', 2):
            batches = []
            for _ in range(6):
                result = maintenance.scan_source(self.cfg, {'source_id': source['id']})
                batches.append(result)
                if not result['truncated']:
                    break
        self.assertTrue(batches[0]['truncated'])
        self.assertGreater(batches[0]['progress']['pending_directories'], 0)
        self.assertFalse(result['truncated'])
        self.assertEqual({row['path'] for row in maintenance.inventory(self.cfg)['items']}, expected)
        self.assertFalse(maintenance._SCAN_ENUMERATORS)

    def test_lost_in_memory_cursor_replays_safely_and_eventually_completes(self):
        for index in range(5):
            (self.root / ('report%d.md' % index)).write_text('synthetic report')
        source = self.source()
        with mock.patch.object(maintenance, 'MAX_FILES', 2):
            first = maintenance.scan_source(self.cfg, {'source_id': source['id']})
            self.assertTrue(first['truncated'])
            maintenance._clear_scan_enumerators()  # process restart or cache eviction
            for _ in range(6):
                result = maintenance.scan_source(self.cfg, {'source_id': source['id']})
                if not result['truncated']:
                    break
        self.assertFalse(result['truncated'])
        self.assertEqual(maintenance.inventory(self.cfg)['total'], 5)
        self.assertFalse(maintenance._SCAN_ENUMERATORS)

    def test_report_roles_html_and_explicit_work_source_override(self):
        paths = ('Datasets/caption.txt', 'captions/photo.txt', 'work/copied-source/README.md',
                 'work/outputs/result.html', 'outputs/final.htm', 'Reports/review.md', 'root.md',
                 'node_modules/dependency/README.md', 'releases/package/README.md')
        for relative in paths:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('never changed')
        source = self.source()
        result = maintenance.scan_source(self.cfg, {'source_id': source['id']})
        self.assertEqual({Path(r['path']).relative_to(self.root).as_posix() for r in result['items']},
                         {'work/outputs/result.html', 'outputs/final.htm', 'Reports/review.md', 'root.md'})
        specific = self.source(self.root / 'work/copied-source')
        result = maintenance.scan_source(self.cfg, {'source_id': specific['id']})
        self.assertEqual([r['title'] for r in result['items']], ['README.md'])
        self.assertTrue(all((self.root / relative).exists() for relative in paths))

    def test_failed_subdirectory_preserves_history_and_does_not_block_sibling(self):
        for name in ('a', 'b'):
            (self.root / name).mkdir()
            (self.root / name / 'report.md').write_text(name)
        source = self.source()
        maintenance.scan_source(self.cfg, {'source_id': source['id']})
        original = os.scandir
        def fail_a(path):
            if Path(path) == self.root / 'a':
                raise PermissionError('fixture denied')
            return original(path)
        with mock.patch.object(maintenance.os, 'scandir', side_effect=fail_a):
            result = maintenance.scan_source(self.cfg, {'source_id': source['id']})
        self.assertTrue(result['truncated'])
        self.assertTrue(result['errors'])
        self.assertEqual(maintenance.inventory(self.cfg)['total'], 2)
        self.assertEqual([Path(r['path']).parent.name for r in result['items']], ['b'])
        recovered = maintenance.scan_source(self.cfg, {'source_id': source['id']})
        self.assertFalse(recovered['truncated'])

    def test_scan_symlink_directory_does_not_grant_read_scope(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'private.md').write_text('not indexed')
        try:
            (self.root / 'linked').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Creating a symlink requires Windows privilege')
        source = self.source()
        self.assertEqual(maintenance.scan_source(self.cfg, {'source_id': source['id']})['items'], [])

    def test_reparse_simulation_is_excluded_without_privilege(self):
        folder = self.root / 'linked'
        folder.mkdir()
        (folder / 'private.md').write_text('not indexed')
        original = os.lstat
        def reparse(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if Path(path) == folder:
                return mock.Mock(st_mode=value.st_mode, st_file_attributes=0x400, st_nlink=1)
            return value
        source = self.source()
        with mock.patch.object(maintenance.os, 'lstat', side_effect=reparse):
            self.assertEqual(maintenance.scan_source(self.cfg, {'source_id': source['id']})['items'], [])

    def test_expired_worker_cannot_commit_or_release_new_lease(self):
        (self.root / 'report.md').write_text('keep')
        source = self.source()
        original = os.scandir
        replaced = []
        def replace_lease(path):
            if not replaced:
                replaced.append(True)
                with maintenance._db() as con, con:
                    con.execute('UPDATE source_scan_progress SET state=?,lease_until=? WHERE source_id=?',
                                ('{"new_worker":true}', time.time() + 60, source['id']))
            return original(path)
        with mock.patch.object(maintenance.os, 'scandir', side_effect=replace_lease):
            with self.assertRaisesRegex(ValueError, '租约'):
                maintenance.scan_source(self.cfg, {'source_id': source['id']})
        self.assertEqual(maintenance.inventory(self.cfg)['total'], 0)
        with maintenance._db() as con:
            row = con.execute('SELECT state,lease_until FROM source_scan_progress WHERE source_id=?', (source['id'],)).fetchone()
        self.assertEqual(row['state'], '{"new_worker":true}')
        self.assertGreater(row['lease_until'], time.time())

    def test_cursor_outside_source_fails_preserving_inventory(self):
        (self.root / 'report.md').write_text('keep')
        source = self.source()
        maintenance.scan_source(self.cfg, {'source_id': source['id']})
        with maintenance._db() as con, con:
            row = con.execute('SELECT state FROM source_scan_progress WHERE source_id=?', (source['id'],)).fetchone()
            state = json.loads(row['state'])
            state['queue'] = [['../outside', 1, '']]
            con.execute('UPDATE source_scan_progress SET state=? WHERE source_id=?', (json.dumps(state), source['id']))
        with self.assertRaises(ValueError):
            maintenance.scan_source(self.cfg, {'source_id': source['id']})
        self.assertEqual(maintenance.inventory(self.cfg)['total'], 1)


if __name__ == '__main__':
    unittest.main()
