"""Only disposable documents and databases; no source deletion is permitted."""
from pathlib import Path
import sqlite3
from contextlib import closing
import tempfile
import unittest
from unittest import mock

from aihub import config, collaboration, collaboration_api, collaboration_maintenance as maintenance, harnesses, workcenter


class WorkcenterRemovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihub-removal-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        self.data = self.base / 'application' / 'data'
        self.data.mkdir(parents=True)
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True}
        for key, value in [('DATA_DIR', str(self.data)), ('APP_DIR', str(self.data.parent)), ('REPORTS_DIR', str(self.data / 'reports'))]:
            patch = mock.patch.object(config, key, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.reports = mock.patch.object(workcenter.management, 'reports', return_value=[])
        self.legacy = self.reports.start()
        self.addCleanup(self.reports.stop)
        self.projects = mock.patch.object(workcenter.management, 'projects', return_value={'items': []})
        self.projects.start()
        self.addCleanup(self.projects.stop)
        self.file = self.root / 'report.md'
        self.file.write_text('original fixture report', encoding='utf-8')
        self.source = maintenance.add_source(self.cfg, {'path': str(self.root), 'label': 'source', 'tool': 'any'})
        self.scan()
        self.doc = workcenter.list_documents(self.cfg)['items'][0]
        self.addCleanup(workcenter._REMOVAL_PREVIEWS.clear)

    def scan(self):
        maintenance.scan_source(self.cfg, {'source_id': self.source['id']})

    def body(self, **values):
        return {'_workspace_root': str(self.root), **values}

    def preview(self):
        return workcenter.removal_preview(self.cfg, self.body(document_id=self.doc['id']))

    def apply(self, preview=None):
        preview = preview or self.preview()
        return workcenter.removal_apply(self.cfg, self.body(preview_token=preview['preview_token']))

    def test_cancel_preview_and_empty_removals_are_read_only_no_body_access(self):
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('no body read')):
            self.assertEqual(workcenter.removal_list(self.cfg)['total'], 0)
            preview = self.preview()
        self.assertTrue(preview['files_preserved'])
        self.assertIn('正式报告', preview['warning'])
        self.assertFalse((self.data / 'workcenter.sqlite3').exists())
        self.assertEqual(workcenter.list_documents(self.cfg)['total'], 1)

    def test_apply_preserves_file_and_legacy_rescan_stays_hidden_restore_and_backups(self):
        before = self.file.read_bytes()
        workcenter.classify(self.cfg, {'document_id': self.doc['id'], 'category': 'report', 'project_name': 'renamed'})
        self.apply()
        self.assertEqual(workcenter.list_documents(self.cfg)['total'], 0)
        self.legacy.return_value = [{'path': str(self.file), 'name': 'legacy report', 'group': 'legacy', 'size': len(before), 'mtime': 0}]
        self.scan()
        self.assertEqual(workcenter.list_documents(self.cfg)['total'], 0)
        removed = workcenter.removal_list(self.cfg)
        self.assertEqual(removed['total'], 1)
        self.assertTrue(removed['items'][0]['can_restore'])
        self.assertEqual(removed['items'][0]['project_name'], 'renamed')
        self.assertEqual(self.file.read_bytes(), before)
        result = workcenter.removal_restore(self.cfg, self.body(document_id=self.doc['id']))
        self.assertTrue(result['restored'])
        self.assertEqual(workcenter.removal_list(self.cfg)['total'], 0)
        self.assertEqual(workcenter.list_documents(self.cfg)['items'][0]['project_name'], 'renamed')
        self.assertEqual(self.file.read_bytes(), before)
        backups = list(self.data.glob('workcenter.sqlite3.pre-removal-*.backup'))
        self.assertEqual(len(backups), 2)
        for backup in backups:
            with closing(sqlite3.connect(backup)) as con, con:
                self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_changed_file_and_metadata_previews_fail_without_creating_database(self):
        preview = self.preview()
        self.file.write_text('changed fixture', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '变化'):
            self.apply(preview)
        self.assertFalse((self.data / 'workcenter.sqlite3').exists())
        preview = self.preview()
        self.legacy.return_value = [{'path': str(self.file), 'name': 'new record', 'group': 'new label', 'size': 0, 'mtime': 0}]
        with self.assertRaisesRegex(ValueError, '变化'):
            self.apply(preview)
        self.assertFalse((self.data / 'workcenter.sqlite3').exists())

    def test_expired_and_root_mismatch_and_data_switch_previews_fail(self):
        preview = self.preview()
        with mock.patch.object(workcenter.time, 'time', return_value=preview['expires_at'] + 1):
            with self.assertRaisesRegex(ValueError, '过期'):
                self.apply(preview)
        other = self.base / 'other'
        other.mkdir()
        with self.assertRaisesRegex(ValueError, '切换'):
            workcenter.removal_preview({**self.cfg, 'ai_root': str(other)}, self.body(document_id=self.doc['id']))
        with mock.patch.object(config, 'DATA_DIR', str(self.base / 'other-data')):
            with self.assertRaisesRegex(ValueError, '变化'):
                self.apply(preview)
        self.assertFalse((self.data / 'workcenter.sqlite3').exists())

    def test_replaced_root_is_not_same_environment_and_restore_keeps_tombstone(self):
        self.apply()
        self.root.rename(self.base / 'old-root')
        self.root.mkdir()
        self.assertFalse(workcenter.removal_list(self.cfg)['items'][0]['can_restore'])
        with self.assertRaisesRegex(ValueError, '身份'):
            workcenter.removal_restore(self.cfg, self.body(document_id=self.doc['id']))
        self.assertEqual(workcenter.removal_list(self.cfg)['total'], 1)

    def test_restoring_missing_file_keeps_record_no_file_recreation(self):
        self.apply()
        self.file.unlink()  # Disposable fixture simulates external loss.
        workcenter.removal_restore(self.cfg, self.body(document_id=self.doc['id']))
        self.assertFalse(self.file.exists())
        self.assertEqual(workcenter.list_documents(self.cfg)['items'][0]['status'], 'missing')

    def test_old_preview_cannot_reapply_after_separate_remove_restore_cycle(self):
        old = self.preview()
        self.apply()
        workcenter.removal_restore(self.cfg, self.body(document_id=self.doc['id']))
        with self.assertRaisesRegex(ValueError, '变化'):
            self.apply(old)
        self.assertEqual(workcenter.list_documents(self.cfg)['total'], 1)

    def test_deleted_artifact_and_task_path_cannot_reappear_from_scans(self):
        harnesses.save(self.cfg, {'id': 'codex', 'revision': 0, 'connection_mode': 'mcp_stdio'})
        task = collaboration.execute(self.cfg, 'task_create', {'project': 'Fixture', 'title': 'Task', 'description': 'Fixture task', 'target_tool': 'codex'})
        collaboration.execute(self.cfg, 'client_heartbeat', {'client_id': 'fixture', 'tool': 'codex', 'name': 'Fixture', 'protocol_version': 1})
        claimed = collaboration.execute(self.cfg, 'task_claim', {'task_id': task['id'], 'client_id': 'fixture'})
        report = Path(task['paths']['reports']) / 'final.md'
        report.write_text('final fixture', encoding='utf-8')
        artifact = collaboration.execute(self.cfg, 'artifact_register', {'task_id': task['id'], 'client_id': 'fixture', 'lease_token': claimed['lease_token'], 'kind': 'report', 'path': str(report), 'title': 'Final'})
        self.scan()
        self.legacy.return_value = [{'path': str(report), 'name': 'legacy', 'group': 'legacy', 'size': 13, 'mtime': 0}]
        def paths():
            return {doc['path'] for doc in workcenter.list_documents(self.cfg)['items']}
        self.assertIn(str(report), paths())
        db = self.data / 'collaboration.sqlite3'
        with closing(sqlite3.connect(db)) as con, con:
            con.execute('UPDATE artifacts SET deleted_at=? WHERE id=?', ('2026-10-01T00:00:00Z', artifact['id']))
        self.assertNotIn(str(report), paths())
        with closing(sqlite3.connect(db)) as con, con:
            con.execute('UPDATE artifacts SET deleted_at=NULL WHERE id=?', (artifact['id'],))
            con.execute('UPDATE tasks SET deleted_at=? WHERE id=?', ('2026-10-01T00:00:00Z', task['id']))
        self.assertNotIn(str(report), paths())
        with closing(sqlite3.connect(db)) as con, con:
            con.execute('UPDATE tasks SET deleted_at=NULL WHERE id=?', (task['id'],))
        self.assertIn(str(report), paths())
        self.assertEqual(report.read_text(encoding='utf-8'), 'final fixture')

    def test_legacy_schema_read_does_not_add_deleted_columns(self):
        db = self.data / 'collaboration.sqlite3'
        with closing(sqlite3.connect(db)) as con, con:
            con.execute('CREATE TABLE tasks(id TEXT,root TEXT,project TEXT,title TEXT,status TEXT,target_tool TEXT)')
        self.assertEqual(workcenter.list_documents(self.cfg)['total'], 1)
        with closing(sqlite3.connect(db)) as con, con:
            self.assertNotIn('deleted_at', {row[1] for row in con.execute('PRAGMA table_info(tasks)')})

    def test_revoked_source_cannot_reappear_as_legacy_and_restore_retains_original(self):
        self.legacy.return_value = [{'path': str(self.file), 'name': 'legacy', 'group': 'legacy', 'size': 0, 'mtime': 0}]
        body = self.body(entity_type='source', entity_id=self.source['id'])
        preview = collaboration_api.execute(self.cfg, 'record_delete_preview', body)
        collaboration_api.execute(self.cfg, 'record_delete_apply', {**body, 'preview_token': preview['preview_token']})
        self.assertEqual(workcenter.list_documents(self.cfg)['total'], 0)
        self.assertEqual(self.file.read_text(encoding='utf-8'), 'original fixture report')
        collaboration_api.execute(self.cfg, 'record_restore', body)
        self.assertEqual(workcenter.list_documents(self.cfg)['total'], 1)

    def test_revoked_parent_source_preserves_independently_authorized_child_source(self):
        child = self.root / 'child'
        child.mkdir()
        report = child / 'still-authorized.md'
        report.write_text('independent source', encoding='utf-8')
        source = maintenance.add_source(self.cfg, {'path': str(child), 'label': 'child source', 'tool': 'any'})
        maintenance.scan_source(self.cfg, {'source_id': source['id']})
        self.legacy.return_value = [{'path': str(report), 'name': 'legacy', 'group': 'legacy', 'size': 0, 'mtime': 0}]
        body = self.body(entity_type='source', entity_id=self.source['id'])
        preview = collaboration_api.execute(self.cfg, 'record_delete_preview', body)
        collaboration_api.execute(self.cfg, 'record_delete_apply', {**body, 'preview_token': preview['preview_token']})
        listing = workcenter.list_documents(self.cfg)
        self.assertEqual(listing['total'], 1)
        self.assertEqual(listing['items'][0]['path'], str(report))
        self.assertEqual(listing['items'][0]['intake_status'], 'indexed')
        self.assertEqual(listing['items'][0]['source_labels'], ['child source'])

    def test_invalid_restore_does_not_create_management_database(self):
        with self.assertRaises(ValueError):
            workcenter.removal_restore(self.cfg, self.body(document_id='doc_' + '0' * 32))
        self.assertFalse((self.data / 'workcenter.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
