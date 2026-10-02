"""Structured AI declarations never enlarge file, memory or recycling authority."""
import sqlite3
from pathlib import Path
import unittest
from unittest.mock import patch

from aihub import collaboration as c, collaboration_api, workcenter
from tools.aihub_mcp import BY_NAME, RPCError, validate
from tests import test_workcenter as fixtures


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkcenterTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.cfg, self.data = self.fixture.cfg, self.fixture.data
        self.call('client_heartbeat', client_id='author', tool='codex', name='Author', protocol_version=1)
        self.task = self.call('task_create', project='Actual', title='Task')
        claim = self.call('task_claim', task_id=self.task['id'], client_id='author')
        self.owner = dict(task_id=self.task['id'], client_id='author', lease_token=claim['lease_token'])

    def call(self, action, **payload):
        return c.execute(self.cfg, action, payload)

    def submit(self, **extra):
        payload = dict(self.owner, kind='report', title='Result', filename='result.md', content='original')
        payload.update(extra)
        return self.call('artifact_write', **payload)

    def document(self, artifact):
        return workcenter.lookup_document(self.cfg, artifact['path'])

    def test_explicit_category_origin_and_task_identity_survive_handoff(self):
        artifact = self.submit(category='plan')
        self.call('task_handoff', **self.owner, target_tool='dsh', summary='Next')
        doc = self.document(artifact)
        self.assertEqual((doc['category'], doc['category_source'], doc['classification_status']), ('plan', 'submitted', 'classified'))
        self.assertEqual((doc['tool'], doc['source_client_id'], doc['task_id'], doc['artifact_id']),
                         ('codex', 'author', self.task['id'], artifact['id']))
        self.assertEqual(doc['project_name'], 'Actual')
        self.assertEqual(doc['retention'], 'retained')
        self.assertTrue(doc['retention_managed'])

    def test_register_existing_file_preserves_content_and_declares_semantic_category(self):
        path = Path(self.task['paths']['outputs']) / 'specification.md'
        path.write_bytes(b'original specification')
        artifact = self.call('artifact_register', **self.owner, kind='output', category='requirement',
                             path=str(path), title='Requirements')
        self.assertEqual(path.read_bytes(), b'original specification')
        doc = self.document(artifact)
        self.assertEqual((doc['category'], doc['category_source'], doc['retention']),
                         ('requirement', 'submitted', 'retained'))
        self.assertEqual(workcenter.read_document(self.cfg, doc['id'])['content'], 'original specification')
        read = workcenter.read_document(self.cfg, doc['id'])
        self.assertEqual((read['category'], read['category_source'], read['project_name'], read['retention']),
                         ('requirement', 'submitted', 'Actual', 'retained'))
        self.assertFalse(any(key.startswith('_') for key in read))

    def test_coverage_uses_deduped_catalog_and_keeps_raw_inventory_count(self):
        artifact = self.submit(category='report')
        legacy = self.fixture.file('Legacy/reference.md')
        self.fixture.scan(Path(self.task['paths']['reports']))
        self.fixture.reports.return_value = [
            {'path': artifact['path'], 'name': 'duplicate legacy', 'group': 'old', 'size': 8, 'mtime': 0},
            {'path': str(legacy), 'name': 'reference', 'group': 'old', 'size': 6, 'mtime': 0}]
        result = workcenter.list_documents(self.cfg)
        coverage = result['coverage']
        self.assertEqual(coverage['catalog_documents'], result['total'])
        self.assertEqual((coverage['registered_documents'], coverage['indexed_visible_documents'],
                          coverage['legacy_documents'], coverage['indexed_documents']), (1, 0, 1, 1))
        indexed = self.fixture.file('Extra/plan.md')
        self.fixture.scan(indexed.parent)
        filtered = workcenter.list_documents(self.cfg, {'category': 'report'})
        coverage = filtered['coverage']
        self.assertEqual(filtered['total'], 1)
        self.assertEqual((coverage['catalog_documents'], coverage['registered_documents'],
                          coverage['indexed_visible_documents'], coverage['legacy_documents'],
                          coverage['indexed_documents']), (3, 1, 1, 1, 2))

    def test_coverage_registered_without_scanned_sources_is_visible(self):
        self.submit(category='report')
        coverage = workcenter.list_documents(self.cfg)['coverage']
        self.assertEqual((coverage['catalog_documents'], coverage['registered_documents'],
                          coverage['indexed_documents']), (1, 1, 0))

    def test_missing_legacy_and_explicit_unknown_remain_pending_manual_wins(self):
        artifact = self.submit()
        doc = self.document(artifact)
        self.assertEqual((doc['category_source'], doc['classification_status']), ('unclassified', 'needs_review'))
        self.assertEqual(artifact['classification_status'], 'needs_review')
        saved = workcenter.classify(self.cfg, dict(document_id=doc['id'], category='reference'))
        self.assertEqual((saved['category_source'], saved['classification_status']), ('manual', 'classified'))
        read = workcenter.read_document(self.cfg, doc['id'])
        self.assertEqual((read['category_source'], read['category']), ('manual', 'reference'))
        unknown = self.submit(filename='unknown.md', category='other_text')
        self.assertEqual((self.document(unknown)['category_source'], self.document(unknown)['classification_status']),
                         ('submitted', 'needs_review'))
        result = workcenter.list_documents(self.cfg, {'classification_status': 'needs_review'})
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['facets']['needs_review_count'], 1)
        self.assertIn({'value': 'classified', 'label': '已分类', 'count': 1}, result['facets']['classification_statuses'])

    def test_category_cannot_promote_temporary_file_or_demote_report(self):
        report = self.submit(category='temp_candidate', memory_candidates=[])
        temp = self.submit(kind='temp', category='report', filename='temp.md')
        self.assertIsNone(report['expires_at'])
        self.assertEqual(report['retention'], 'retained')
        self.assertIsNotNone(temp['expires_at'])
        self.assertEqual(temp['retention'], 'temp_expiring')
        with self.assertRaises(ValueError):
            self.call('memory_propose', source_artifact_id=temp['id'], scope='workspace', title='False memory', content='no')
        self.call('artifact_pin', artifact_id=temp['id'], pinned=True)
        self.assertEqual(self.document(temp)['retention'], 'temp_pinned')
        self.call('task_finish', **self.owner, summary='done')
        self.assertEqual(c.retention_candidates(self.cfg, now='2100-01-01T00:00:00Z'), [])

    def test_invalid_or_impersonated_metadata_creates_no_file(self):
        for payload in ({'category': None}, {'category': 'invented'}, {'category': 'plan', 'project': 'Fake'},
                        {'category': 'report', 'source_tool': 'dsh'}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.submit(**payload)
            self.assertFalse((Path(self.task['paths']['reports']) / 'result.md').exists())
        with self.assertRaises(ValueError):
            self.submit(category='report', lease_token='wrong')

    def test_changed_file_never_reuses_submission_or_manual_evidence(self):
        artifact = self.submit(category='plan')
        doc = self.document(artifact)
        workcenter.classify(self.cfg, dict(document_id=doc['id'], category='delivery'))
        Path(artifact['path']).write_text('replacement', encoding='utf-8')
        changed = self.document(artifact)
        self.assertEqual((changed['submission_evidence'], changed['classification_status'], changed['status']),
                         ('snapshot_changed', 'needs_review', 'changed'))
        self.assertFalse(changed['category_manual'])
        listed = self.call('artifact_list')['items'][0]
        self.assertEqual(listed['submission_evidence'], 'snapshot_changed')
        with self.assertRaises(ValueError):
            workcenter.read_document(self.cfg, doc['id'])
        with self.assertRaises(ValueError):
            workcenter.resolve_document(self.cfg, doc['id'])

    def test_actual_opened_content_is_hashed_after_preverification_race(self):
        artifact = self.submit(category='report')
        doc = self.document(artifact)
        original = workcenter._verify_submission
        def change_after_verify(value):
            original(value)
            Path(artifact['path']).write_text('new unauthorized body', encoding='utf-8')
        with patch.object(workcenter, '_verify_submission', side_effect=change_after_verify), self.assertRaises(ValueError):
            workcenter.read_document(self.cfg, doc['id'])

    def test_schema_readonly_mcp_category_required_and_identity_not_client_supplied(self):
        schema = collaboration_api.execute(self.cfg, 'submission_schema', {'client_id': 'author'}, actor='mcp')
        self.assertTrue(schema['rules']['kind_controls_retention'])
        self.assertTrue(BY_NAME['aihub_submission_schema']['annotations']['readOnlyHint'])
        for name in ('artifact_write', 'artifact_register'):
            required = BY_NAME['aihub_' + name]['inputSchema']['required']
            self.assertIn('category', required)
            payload = {key: 'value' for key in required if key != 'category'}
            with self.assertRaises(RPCError):
                validate(BY_NAME['aihub_' + name]['inputSchema'], payload)
            self.assertNotIn('source_tool', BY_NAME['aihub_' + name]['inputSchema']['properties'])
        self.assertIn('category', (Path(self.task['paths']['work']) / 'TASK_BRIEF.md').read_text(encoding='utf-8'))

    def test_existing_schema_is_backed_up_with_committed_wal_before_alter(self):
        artifact = self.submit()
        path = self.data / 'collaboration.sqlite3'
        old = sqlite3.connect(path)
        try:
            old.execute('PRAGMA journal_mode=WAL')
            for column in ('category', 'source_tool', 'source_client_id'):
                old.execute('ALTER TABLE artifacts DROP COLUMN ' + column)
            old.execute('UPDATE artifacts SET title=? WHERE id=?', ('committed WAL title', artifact['id']))
            old.commit()
            items = self.call('artifact_list')['items']
            self.assertEqual(items[0]['title'], 'committed WAL title')
            self.assertEqual(items[0]['classification_status'], 'needs_review')
            backups = list((self.data / 'collaboration-schema-backups').glob('*.sqlite3'))
            self.assertEqual(len(backups), 1)
            with sqlite3.connect(backups[0]) as backup:
                self.assertNotIn('category', {r[1] for r in backup.execute('PRAGMA table_info(artifacts)')})
                self.assertEqual(backup.execute('SELECT title FROM artifacts').fetchone()[0], 'committed WAL title')
            self.call('artifact_list')
            self.assertEqual(len(list((self.data / 'collaboration-schema-backups').glob('*.sqlite3'))), 1)
        finally:
            old.close()

    def test_backup_failure_leaves_old_schema_untouched(self):
        path = self.data / 'collaboration.sqlite3'
        with sqlite3.connect(path) as old:
            old.execute('ALTER TABLE artifacts DROP COLUMN category')
        original = sqlite3.connect
        def deny_backup(path_value, *args, **kwargs):
            if 'collaboration-schema-backups' in str(path_value):
                raise sqlite3.OperationalError('synthetic backup failure')
            return original(path_value, *args, **kwargs)
        with patch.object(c.sqlite3, 'connect', side_effect=deny_backup), self.assertRaises(sqlite3.OperationalError):
            self.call('artifact_list')
        with original(path) as check:
            self.assertNotIn('category', {r[1] for r in check.execute('PRAGMA table_info(artifacts)')})


if __name__ == '__main__':
    unittest.main()
