"""Isolated reversible record removal; no installed workspace or native records."""
import json
import contextlib
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from aihub import collaboration as core, collaboration_api as api
from aihub import collaboration_maintenance as maintenance, config, harnesses


class RecordLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='aihub-record-removal-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        self.data = self.base / 'data'
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True}
        override = patch.object(config, 'DATA_DIR', str(self.data))
        override.start()
        self.addCleanup(override.stop)
        self.addCleanup(core._RECORD_PREVIEWS.clear)
        self.addCleanup(maintenance._clear_scan_enumerators)
        harnesses.save(self.cfg, {'id': 'codex', 'revision': 0, 'connection_mode': 'mcp_stdio'})
        self.call('client_heartbeat', client_id='synthetic', tool='codex', name='合成客户端', protocol_version=1)

    def call(self, action, **body):
        return api.execute(self.cfg, action, {'_workspace_root': str(self.root), **body})

    def task(self, active=False):
        task = self.call('task_create', project='Synthetic', title='合成任务', report_policy='optional')
        if active:
            claim = self.call('task_claim', task_id=task['id'], client_id='synthetic')
            self.lease = {'task_id': task['id'], 'client_id': 'synthetic', 'lease_token': claim['lease_token']}
        return task

    def report(self, candidates=None, kind='report'):
        body = dict(self.lease, title='合成报告', kind=kind, category='report', filename='report-' + str(time.time_ns()) + '.md', content='报告正文不是共享记忆')
        if candidates is not None:
            body['memory_candidates'] = candidates
        return self.call('artifact_write', **body)

    def preview(self, entity_type, identifier):
        return self.call('record_delete_preview', entity_type=entity_type, entity_id=identifier)

    def remove(self, entity_type, identifier):
        preview = self.preview(entity_type, identifier)
        self.assertTrue(preview['can_apply'])
        return self.call('record_delete_apply', entity_type=entity_type, entity_id=identifier, preview_token=preview['preview_token'])

    def restore(self, entity_type, identifier):
        return self.call('record_restore', entity_type=entity_type, entity_id=identifier)

    def test_queued_task_delete_restore_keeps_files_and_blocks_claim(self):
        task = self.task()
        brief = Path(task['paths']['work']) / 'TASK_BRIEF.md'
        before = brief.read_bytes()
        result = self.remove('task', task['id'])
        self.assertTrue(result['files_preserved'])
        self.assertTrue(result['record']['deleted_at'])
        self.assertEqual(brief.read_bytes(), before)
        self.assertEqual(self.call('task_list')['items'], [])
        self.assertEqual(len(self.call('task_list', include_deleted=True)['items']), 1)
        self.assertTrue(core.can_stop(self.cfg))
        with self.assertRaisesRegex(ValueError, '移除'):
            self.call('task_claim', task_id=task['id'], client_id='synthetic')
        self.restore('task', task['id'])
        self.assertEqual(self.call('task_list')['items'][0]['status'], 'queued')
        self.assertFalse(core.can_stop(self.cfg))
        with core.store(self.cfg) as (con, root):
            self.assertEqual(con.execute('SELECT action FROM audit WHERE root=? AND object_id=? ORDER BY id', (root, task['id'])).fetchall()[-1]['action'], 'record_restore')

    def test_active_task_delete_refused_and_stale_claim_invalidates_preview(self):
        task = self.task()
        preview = self.preview('task', task['id'])
        claim = self.call('task_claim', task_id=task['id'], client_id='synthetic')
        self.assertFalse(self.preview('task', task['id'])['can_apply'])
        with self.assertRaises(ValueError):
            self.call('record_delete_apply', entity_type='task', entity_id=task['id'], preview_token=preview['preview_token'])
        self.call('task_finish', task_id=task['id'], client_id='synthetic', lease_token=claim['lease_token'])
        with self.assertRaisesRegex(ValueError, '改变'):
            self.call('record_delete_apply', entity_type='task', entity_id=task['id'], preview_token=preview['preview_token'])

    def test_preview_root_target_expiry_and_aba_bindings(self):
        task, other = self.task(), self.task()
        preview = self.preview('task', task['id'])
        with self.assertRaises(ValueError):
            self.call('record_delete_apply', entity_type='task', entity_id=other['id'], preview_token=preview['preview_token'])
        with self.assertRaises(ValueError):
            api.execute(self.cfg, 'record_delete_apply', {'entity_type': 'task', 'entity_id': task['id'], 'preview_token': preview['preview_token']})
        alternate = self.base / 'alternate'
        alternate.mkdir()
        with self.assertRaises(ValueError):
            api.execute(dict(self.cfg, ai_root=str(alternate)), 'record_delete_apply', {'_workspace_root': str(self.root), 'entity_type': 'task', 'entity_id': task['id'], 'preview_token': preview['preview_token']})
        self.remove('task', task['id'])
        self.restore('task', task['id'])
        with self.assertRaisesRegex(ValueError, '改变'):
            self.call('record_delete_apply', entity_type='task', entity_id=task['id'], preview_token=preview['preview_token'])
        fresh = self.preview('task', task['id'])
        core._RECORD_PREVIEWS[fresh['preview_token']]['expires'] = 0
        with self.assertRaisesRegex(ValueError, '过期'):
            self.call('record_delete_apply', entity_type='task', entity_id=task['id'], preview_token=fresh['preview_token'])

    def test_artifact_and_task_removal_preserve_approved_memory_references(self):
        task = self.task(True)
        report = self.report([{'title': '结论', 'content': '可复用结论', 'scope': 'project'}])
        memory_id = report['memory_candidate_ids'][0]
        self.call('memory_review', memory_id=memory_id, status='approved')
        self.call('task_finish', **self.lease)
        preview = self.preview('artifact', report['id'])
        self.assertEqual(preview['impacts']['approved_memories'], 1)
        self.remove('artifact', report['id'])
        self.assertTrue(Path(report['path']).is_file())
        self.assertEqual(self.call('artifact_list')['items'], [])
        memory = self.call('memory_search')['items'][0]
        self.assertEqual(memory['source_artifact_id'], report['id'])
        self.assertTrue(memory['source_deleted_at'])
        self.restore('artifact', report['id'])
        self.remove('task', task['id'])
        self.assertEqual(self.call('artifact_list')['items'], [])
        self.assertEqual(len(self.call('artifact_list', include_deleted=True)['items']), 1)
        self.assertEqual(self.call('memory_search')['items'][0]['id'], memory_id)
        with self.assertRaises(ValueError):
            self.report()
        self.restore('task', task['id'])
        self.assertEqual(len(self.call('artifact_list')['items']), 1)

    def test_approved_memory_removal_restores_review_state_and_statistics(self):
        self.task(True)
        report = self.report([{'title': '结论', 'content': '持久结论', 'scope': 'workspace'}])
        memory_id = report['memory_candidate_ids'][0]
        self.call('memory_review', memory_id=memory_id, status='approved')
        self.remove('memory', memory_id)
        self.assertEqual(self.call('memory_search')['items'], [])
        self.assertEqual(len(self.call('memory_list', include_deleted=True)['items']), 1)
        state = api.status(self.cfg)
        self.assertEqual(state['record_counts']['memories']['deleted'], 1)
        self.assertEqual(state['record_counts']['memories']['approved'], 0)
        self.restore('memory', memory_id)
        self.assertEqual(self.call('memory_search')['items'][0]['status'], 'approved')

    def test_changed_record_references_and_file_reject_stale_confirmation(self):
        self.task(True)
        report = self.report()
        preview = self.preview('artifact', report['id'])
        self.call('artifact_pin', artifact_id=report['id'], pinned=True)
        with self.assertRaisesRegex(ValueError, '改变'):
            self.call('record_delete_apply', entity_type='artifact', entity_id=report['id'], preview_token=preview['preview_token'])
        preview = self.preview('artifact', report['id'])
        self.call('memory_propose', source_artifact_id=report['id'], title='新增引用', content='结论', scope='workspace')
        with self.assertRaisesRegex(ValueError, '改变'):
            self.call('record_delete_apply', entity_type='artifact', entity_id=report['id'], preview_token=preview['preview_token'])
        preview = self.preview('artifact', report['id'])
        Path(report['path']).write_text('changed physical source', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '改变'):
            self.call('record_delete_apply', entity_type='artifact', entity_id=report['id'], preview_token=preview['preview_token'])

    def test_removed_temp_never_enters_retention_even_with_explicit_id(self):
        self.task(True)
        artifact = self.report(kind='temp')
        self.call('task_finish', **self.lease)
        with core.store(self.cfg) as (con, root):
            con.execute('UPDATE artifacts SET expires_at=? WHERE root=? AND id=?', ('2000-01-01T00:00:00+00:00', root, artifact['id']))
        self.remove('artifact', artifact['id'])
        self.assertEqual(core.retention_count(self.cfg), 0)
        self.assertEqual(core.retention_preview(self.cfg)['items'], [])
        calls = []
        result = core.recycle_candidate(self.cfg, artifact['id'], lambda path: calls.append(path))
        self.assertEqual(result['status'], 'protected')
        self.assertEqual(calls, [])
        self.assertTrue(Path(artifact['path']).exists())

    def source(self):
        folder = self.root / '00_Management' / 'Reports'
        folder.mkdir(parents=True)
        (folder / 'native-report.md').write_text('native source record', encoding='utf-8')
        return self.call('source_add', path=str(folder), label='合成来源', tool='any'), folder

    def test_source_revoke_blocks_rescan_readd_inventory_and_candidates(self):
        source, folder = self.source()
        self.call('source_scan', source_id=source['id'])
        self.assertEqual(maintenance.inventory(self.cfg)['total'], 1)
        self.remove('source', source['id'])
        self.assertTrue((folder / 'native-report.md').exists())
        self.assertEqual(self.call('source_list')['items'], [])
        self.assertEqual(maintenance.inventory(self.cfg)['total'], 0)
        self.assertEqual(maintenance.inventory(self.cfg, include_deleted=True)['total'], 1)
        self.assertNotIn(str(folder), [row['path'] for row in self.call('source_candidates')['items']])
        with self.assertRaises(ValueError):
            self.call('source_scan', source_id=source['id'])
        with self.assertRaisesRegex(ValueError, '移除'):
            self.call('source_add', path=str(folder))
        self.assertEqual(api.status(self.cfg)['record_counts']['sources']['deleted'], 1)
        self.restore('source', source['id'])
        self.assertEqual(maintenance.inventory(self.cfg)['total'], 1)
        self.call('source_scan', source_id=source['id'])
        with maintenance._db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM source_record_audit').fetchone()[0], 2)

    def test_source_scan_revision_and_lease_invalidate_preview(self):
        source, _ = self.source()
        preview = self.preview('source', source['id'])
        self.call('source_scan', source_id=source['id'])
        with self.assertRaisesRegex(ValueError, '改变'):
            self.call('record_delete_apply', entity_type='source', entity_id=source['id'], preview_token=preview['preview_token'])
        with maintenance._db() as con, con:
            con.execute('UPDATE source_scan_progress SET lease_until=? WHERE source_id=?', (time.time() + 3600, source['id']))
        self.assertFalse(self.preview('source', source['id'])['can_apply'])

    def test_lifecycle_is_ui_only_and_list_flags_are_strict(self):
        task = self.task()
        for action in ('record_delete_preview', 'record_delete_apply', 'record_restore'):
            with self.assertRaises(PermissionError):
                api.execute(self.cfg, action, {'_workspace_root': str(self.root), 'entity_type': 'task', 'entity_id': task['id']}, actor='mcp')
        for action in ('task_list', 'artifact_list', 'memory_list', 'source_list', 'source_inventory'):
            with self.assertRaises(ValueError):
                self.call(action, include_deleted='yes')

    def test_report_candidates_are_atomic_scoped_and_not_automatically_approved(self):
        task = self.task(True)
        candidates = [{'title': '项目结论', 'content': '复用原则', 'scope': 'project'}, {'title': '工作区结论', 'content': '通用原则', 'scope': 'workspace'}]
        report = self.report(candidates)
        self.assertEqual(report['memory_candidate_count'], 2)
        memories = self.call('memory_list')['items']
        self.assertEqual({m['source_artifact_id'] for m in memories}, {report['id']})
        self.assertEqual({m['status'] for m in memories}, {'candidate'})
        self.assertEqual({m['project'] for m in memories}, {task['project'], ''})
        self.assertEqual(self.call('memory_search')['items'], [])
        self.report([])
        state = api.status(self.cfg)
        self.assertEqual(state['memory_pipeline'], {'reports': 2, 'reports_with_candidates': 1, 'reports_without_candidates': 1,
            'reports_evaluated': 2, 'reports_unevaluated': 0, 'reports_evaluated_without_candidates': 1,
            'candidates': 2, 'approved': 0})
        with core.store(self.cfg) as (con, root):
            self.assertEqual(con.execute("SELECT count(*) FROM audit WHERE root=? AND action='memory_propose'", (root,)).fetchone()[0], 2)

    def test_invalid_candidate_batches_make_no_file_or_rows(self):
        task = self.task(True)
        invalid = [None, {}, [{'title': 'x', 'content': 'y', 'scope': 'global'}], [{'title': 'x', 'content': 'y', 'scope': 'project', 'project': 'Forged'}], [{'title': 'x' * 201, 'content': 'y', 'scope': 'project'}], [{'title': 'x', 'content': 'y' * 20001, 'scope': 'project'}], [{'title': 'x', 'content': 'y', 'scope': 'project'}] * 6]
        for candidates in invalid:
            with self.subTest(candidates=str(candidates)[:80]), self.assertRaises(ValueError):
                self.call('artifact_write', **self.lease, title='invalid', kind='report', category='report', filename='invalid.md', content='invalid', memory_candidates=candidates)
        with self.assertRaises(ValueError):
            self.report([{'title': 'x', 'content': 'y', 'scope': 'workspace'}], kind='output')
        self.assertEqual(list(Path(task['paths']['reports']).iterdir()), [])
        self.assertEqual(self.call('artifact_list')['items'], [])
        self.assertEqual(self.call('memory_list')['items'], [])

    def test_old_database_upgrade_backs_up_committed_rows_before_altering(self):
        task = self.task()
        with contextlib.closing(sqlite3.connect(self.data / 'collaboration.sqlite3')) as con, con:
            con.execute('PRAGMA journal_mode=WAL')
            for table in ('tasks', 'artifacts', 'memories'):
                for column in ('deleted_at', 'deleted_by', 'record_revision'):
                    con.execute('ALTER TABLE ' + table + ' DROP COLUMN ' + column)
            con.execute('UPDATE tasks SET title=? WHERE id=?', ('WAL 合成记录', task['id']))
        self.assertEqual(self.call('task_list')['items'][0]['title'], 'WAL 合成记录')
        backups = list((self.data / 'collaboration-schema-backups').glob('*.sqlite3'))
        self.assertEqual(len(backups), 1)
        with contextlib.closing(sqlite3.connect(backups[0])) as con:
            self.assertEqual(con.execute('SELECT title FROM tasks WHERE id=?', (task['id'],)).fetchone()[0], 'WAL 合成记录')
            self.assertNotIn('deleted_at', {r[1] for r in con.execute('PRAGMA table_info(tasks)')})

    def test_registered_report_candidates_use_same_transaction(self):
        task = self.task(True)
        report_path = Path(task['paths']['reports']) / 'existing.md'
        report_path.write_text('existing original', encoding='utf-8')
        result = self.call('artifact_register', **self.lease, title='既有报告', kind='report', category='report',
            path=str(report_path), memory_candidates=[{'title': '现有结论', 'content': '可复用内容', 'scope': 'project'}])
        self.assertEqual(result['memory_candidate_count'], 1)
        self.assertEqual(self.call('memory_list')['items'][0]['source_artifact_id'], result['id'])
        original_audit = core._audit
        def fail_memory_audit(con, root, action, *args, **kwargs):
            if action == 'memory_propose':
                raise sqlite3.OperationalError('synthetic candidate transaction failure')
            return original_audit(con, root, action, *args, **kwargs)
        with patch.object(core, '_audit', side_effect=fail_memory_audit), self.assertRaises(sqlite3.OperationalError):
            self.call('artifact_write', **self.lease, title='失败报告', kind='report', category='report', filename='rollback.md',
                content='rollback', memory_candidates=[{'title': '失败候选', 'content': '不能留下', 'scope': 'workspace'}])
        self.assertFalse((Path(task['paths']['reports']) / 'rollback.md').exists())
        self.assertEqual(len(self.call('artifact_list')['items']), 1)
        self.assertEqual(len(self.call('memory_list')['items']), 1)
        self.assertEqual(report_path.read_text(encoding='utf-8'), 'existing original')

    def test_source_migration_backup_retains_inventory_before_columns_added(self):
        source, _ = self.source()
        self.call('source_scan', source_id=source['id'])
        with contextlib.closing(sqlite3.connect(self.data / 'collaboration-maintenance.sqlite3')) as con, con:
            for column in ('deleted_at', 'deleted_by', 'record_revision'):
                con.execute('ALTER TABLE sources DROP COLUMN ' + column)
        self.assertEqual(self.call('source_list')['items'][0]['id'], source['id'])
        backups = list((self.data / 'maintenance-schema-backups').glob('*.sqlite3'))
        self.assertEqual(len(backups), 1)
        with contextlib.closing(sqlite3.connect(backups[0])) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM inventory').fetchone()[0], 1)
            self.assertNotIn('deleted_at', {r[1] for r in con.execute('PRAGMA table_info(sources)')})


if __name__ == '__main__':
    unittest.main()
