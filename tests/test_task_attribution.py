"""Task routing and current ownership never replace actual submission attribution."""
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from aihub import collaboration as c, config, workcenter
from tests import test_workcenter as fixtures


class TaskAttributionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkcenterTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.cfg, self.data = self.fixture.cfg, self.fixture.data
        self.client('author-one', 'codex', '编程窗口 A')

    def client(self, identifier, tool, name):
        return c.execute(self.cfg, 'client_heartbeat', dict(client_id=identifier, tool=tool, name=name, protocol_version=1))

    def task(self, client='author-one', project='One', title='共同标题'):
        task = c.execute(self.cfg, 'task_create', dict(project=project, title=title, description='private task description'))
        claimed = c.execute(self.cfg, 'task_claim', dict(task_id=task['id'], client_id=client))
        return task, dict(task_id=task['id'], client_id=client, lease_token=claimed['lease_token'])

    def submit(self, owner, filename='result.md'):
        return c.execute(self.cfg, 'artifact_write', dict(owner, kind='report', category='report',
                         title='完成报告', filename=filename, content='safe report'))

    def doc(self, artifact):
        return workcenter.lookup_document(self.cfg, artifact['path'])

    def test_handoff_and_new_owner_do_not_change_original_submitter(self):
        task, owner = self.task()
        artifact = self.submit(owner)
        doc = self.doc(artifact)
        self.assertEqual((doc['task_title'], doc['task_status'], doc['task_target_tool']), ('共同标题', 'active', 'any'))
        self.assertEqual((doc['source_client_id'], doc['source_client_name'], doc['source_tool']),
                         ('author-one', '编程窗口 A', 'codex'))
        self.assertEqual((doc['task_owner_id'], doc['task_owner_name'], doc['task_owner_tool']),
                         ('author-one', '编程窗口 A', 'codex'))
        self.client('executor-next', 'dsh', '执行窗口 B')
        c.execute(self.cfg, 'task_handoff', dict(owner, target_tool='dsh', summary='next'))
        queued = self.doc(artifact)
        self.assertEqual((queued['task_status'], queued['task_target_tool'], queued['task_owner_id']), ('queued', 'dsh', None))
        claimed = c.execute(self.cfg, 'task_claim', dict(task_id=task['id'], client_id='executor-next'))
        current = self.doc(artifact)
        self.assertEqual((current['task_owner_id'], current['task_owner_name'], current['task_owner_tool']),
                         ('executor-next', '执行窗口 B', 'dsh'))
        self.assertEqual((current['source_client_id'], current['source_client_name'], current['source_tool']),
                         ('author-one', '编程窗口 A', 'codex'))
        c.execute(self.cfg, 'artifact_write', dict(task_id=task['id'], client_id='executor-next',
                  lease_token=claimed['lease_token'], kind='report', category='report', title='复核报告',
                  filename='review.md', content='本次领取复核完成', memory_candidates=[]))
        c.execute(self.cfg, 'task_finish', dict(task_id=task['id'], client_id='executor-next',
                  lease_token=claimed['lease_token'], summary='done'))
        completed = self.doc(artifact)
        self.assertEqual((completed['task_status'], completed['task_owner_id'], completed['task_owner_name']),
                         ('completed', None, None))
        public = json.dumps(workcenter.read_document(self.cfg, completed['id']))
        self.assertNotIn('private task description', public)
        self.assertNotIn(owner['lease_token'], public)
        self.assertNotIn('lease_hash', public)

    def test_same_tool_distinct_clients_and_same_task_titles_remain_separate(self):
        first_task, first_owner = self.task()
        self.submit(first_owner)
        self.client('author-two', 'codex', '编程窗口 B')
        second_task, second_owner = self.task('author-two', project='Two')
        self.submit(second_owner)
        result = workcenter.list_documents(self.cfg)
        self.assertEqual(result['total'], 2)
        self.assertEqual({d['source_client_name'] for d in result['items']}, {'编程窗口 A', '编程窗口 B'})
        self.assertEqual({f['value'] for f in result['facets']['tasks']}, {first_task['id'], second_task['id']})
        self.assertTrue(all(f['value'] in f['label'] for f in result['facets']['tasks']))
        self.assertEqual({f['value'] for f in result['facets']['submitters']}, {'author-one', 'author-two'})
        self.assertEqual(workcenter.list_documents(self.cfg, {'task_id': second_task['id']})['total'], 1)
        self.assertEqual(workcenter.list_documents(self.cfg, {'source_client_id': 'author-one'})['total'], 1)
        self.assertEqual(workcenter.list_documents(self.cfg, {'query': '编程窗口 B'})['total'], 1)
        self.assertEqual(workcenter.list_documents(self.cfg, {'query': second_task['id']})['total'], 1)
        self.assertEqual(workcenter.list_projects(self.cfg, {'entry_kind': 'all', 'task_id': first_task['id']})['total'], 1)

    def test_indexed_paths_do_not_guess_author_or_task_and_unknown_filter_is_explicit(self):
        task, owner = self.task()
        self.submit(owner)
        indexed = self.fixture.file('codex/author-one/共同标题/report.md')
        self.fixture.scan(indexed.parent)
        result = workcenter.list_documents(self.cfg, {'task_id': workcenter.UNKNOWN_ATTRIBUTION})
        self.assertEqual(result['total'], 1)
        doc = result['items'][0]
        for key in ('task_id', 'task_title', 'task_status', 'task_target_tool', 'task_owner_id', 'task_owner_name',
                    'task_owner_tool', 'source_client_id', 'source_client_name', 'source_tool'):
            self.assertIsNone(doc[key], key)
        self.assertEqual(workcenter.list_documents(self.cfg, {'source_client_id': workcenter.UNKNOWN_ATTRIBUTION})['total'], 1)
        self.assertIn({'value': workcenter.UNKNOWN_ATTRIBUTION, 'label': '提交者未知', 'count': 1}, result['facets']['submitters'])
        self.assertIn({'value': workcenter.UNKNOWN_ATTRIBUTION, 'label': '未登记任务', 'count': 1}, result['facets']['tasks'])

    def test_unknown_filter_marker_cannot_shadow_a_valid_client_id(self):
        self.client('__unknown__', 'codex', '有实际登记的客户端')
        _, owner = self.task('__unknown__')
        artifact = self.submit(owner)
        indexed = self.fixture.file('Indexed/report.md')
        self.fixture.scan(indexed.parent)
        result = workcenter.list_documents(self.cfg, {'source_client_id': '__unknown__'})
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['artifact_id'], artifact['id'])
        self.assertIn({'value': '__unknown__', 'label': '有实际登记的客户端 · __unknown__', 'count': 1},
                      result['facets']['submitters'])
        unknown = workcenter.list_documents(self.cfg, {'source_client_id': workcenter.UNKNOWN_ATTRIBUTION})
        self.assertEqual(unknown['total'], 1)
        self.assertIsNone(unknown['items'][0]['source_client_id'])

    def test_client_name_is_current_root_scoped_declaration_with_tool_match(self):
        _, owner = self.task()
        artifact = self.submit(owner)
        self.client('author-one', 'codex', '新的自报名')
        self.assertEqual(self.doc(artifact)['source_client_name'], '新的自报名')
        other_root = self.fixture.base / 'other-root'
        other_root.mkdir()
        path = self.data / 'collaboration.sqlite3'
        with sqlite3.connect(path) as con:
            con.execute('INSERT INTO clients VALUES(?,?,?,?,?,?)',
                        ('author-one', config._key(other_root), 'dsh', '不属于当前工作区', 'date', 1))
        self.assertEqual(self.doc(artifact)['source_client_name'], '新的自报名')
        with sqlite3.connect(path) as con:
            con.execute('UPDATE clients SET tool=? WHERE root=? AND id=?', ('dsh', config._key(self.cfg['ai_root']), 'author-one'))
        rebound = self.doc(artifact)
        self.assertIsNone(rebound['source_client_name'])
        self.assertEqual((rebound['source_client_id'], rebound['source_tool']), ('author-one', 'codex'))
        with sqlite3.connect(path) as con:
            con.execute('DELETE FROM clients WHERE root=?', (config._key(self.cfg['ai_root']),))
        self.assertIsNone(self.doc(artifact)['source_client_name'])

    def test_legacy_schema_attribution_missing_is_readonly_compatible(self):
        task, owner = self.task()
        artifact = self.submit(owner)
        path = self.data / 'collaboration.sqlite3'
        with sqlite3.connect(path) as con:
            for column in ('source_tool', 'source_client_id'):
                con.execute('ALTER TABLE artifacts DROP COLUMN ' + column)
            for column in ('title', 'status', 'target_tool', 'owner'):
                con.execute('ALTER TABLE tasks DROP COLUMN ' + column)
            con.execute('DROP TABLE clients')
        before = path.read_bytes()
        doc = self.doc(artifact)
        self.assertEqual(doc['task_id'], task['id'])
        for key in ('task_title', 'task_status', 'task_target_tool', 'task_owner_id', 'task_owner_name',
                    'task_owner_tool', 'source_client_id', 'source_client_name', 'source_tool'):
            self.assertIsNone(doc[key], key)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.data / 'collaboration-schema-backups').exists())

    def test_multiple_registered_documents_use_one_bulk_clients_query(self):
        _, owner = self.task()
        for number in range(20):
            self.submit(owner, 'result-%02d.md' % number)
        queries = []
        original = sqlite3.connect
        def trace_connection(*args, **kwargs):
            con = original(*args, **kwargs)
            con.set_trace_callback(queries.append)
            return con
        with patch.object(workcenter.sqlite3, 'connect', side_effect=trace_connection):
            result = workcenter.list_documents(self.cfg)
        self.assertEqual(result['total'], 20)
        self.assertEqual(sum('SELECT id,name,tool FROM clients' in query for query in queries), 1)
        self.assertEqual(sum('FROM artifacts a' in query for query in queries), 1)


if __name__ == '__main__':
    unittest.main()
