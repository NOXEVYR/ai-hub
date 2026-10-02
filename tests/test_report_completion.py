"""Report completion and response-loss retry contracts use isolated workspaces."""
import json
import contextlib
import hashlib
import http.client
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

import server
from aihub import collaboration as c, collaboration_api, config, harnesses


class ReportCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihub-report-contract-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / '工作区'
        self.root.mkdir()
        self.data = self.base / 'data'
        patcher = patch.object(config, 'DATA_DIR', str(self.data))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True}
        harnesses.save(self.cfg, {'id': 'codex', 'revision': 0, 'connection_mode': 'mcp_stdio'})
        for client in ('one', 'two'):
            self.call('client_heartbeat', client_id=client, tool='codex', name=client, protocol_version=1)
        self.task = self.call('task_create', project='合成', title='测试任务')
        self.owner = self.claim(self.task)

    def call(self, action, **body):
        return c.execute(self.cfg, action, body)

    def claim(self, task, client='one'):
        claimed = self.call('task_claim', task_id=task['id'], client_id=client)
        return {'task_id': task['id'], 'client_id': client, 'lease_token': claimed['lease_token']}

    def report(self, **extra):
        body = {**self.owner, 'kind': 'report', 'category': 'report', 'title': '验收',
                'filename': 'report.md', 'content': '# 结果\n验证和未完成事项', 'memory_candidates': []}
        body.update(extra)
        return self.call('artifact_write', **body)

    def current(self):
        return next(t for t in self.call('task_list')['items'] if t['id'] == self.task['id'])

    def assert_pending(self):
        before = self.current()
        with self.assertRaisesRegex(ValueError, '任务尚不能完成'):
            self.call('task_finish', **self.owner, summary='摘要不能代替报告')
        after = self.current()
        self.assertEqual((after['status'], after['owner'], after['summary'], after['updated_at']),
                         (before['status'], before['owner'], before['summary'], before['updated_at']))
        self.assertEqual(after['report_submission']['status'], 'pending')

    def test_default_required_empty_decision_satisfies_finish_and_summary_is_preserved(self):
        self.assertEqual(self.task['report_policy'], 'required')
        self.assert_pending()
        artifact = self.report()
        self.assertEqual(self.current()['report_submission']['artifact_id'], artifact['id'])
        finished = self.call('task_finish', **self.owner, summary='真实完成摘要')
        self.assertEqual((finished['status'], finished['summary']), ('completed', '真实完成摘要'))
        self.assertEqual(finished['report_submission']['status'], 'submitted')
        self.assertEqual(c.status(self.cfg)['tasks'][0]['report_submission']['status'], 'submitted')

    def test_optional_is_explicit_and_invalid_policy_creates_nothing(self):
        task = self.call('task_create', project='合成', title='简单任务', report_policy='optional')
        self.assertEqual(task['report_submission']['status'], 'not_required')
        self.assertEqual(self.call('task_finish', **self.claim(task))['status'], 'completed')
        for value in ('legacy', None, [], False):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.call('task_create', project='合成', title='invalid', report_policy=value)

    def test_legacy_migration_backs_up_wal_without_rewriting_task_or_artifacts(self):
        self.report()
        database = self.data / 'collaboration.sqlite3'
        with contextlib.closing(sqlite3.connect(database)) as old:
            for table in ('artifact_submissions', 'artifact_claims', 'task_report_contracts'):
                old.execute('DROP TABLE ' + table)
            old.execute('PRAGMA journal_mode=WAL')
            old.execute('UPDATE tasks SET summary=? WHERE id=?', ('历史原始摘要', self.task['id']))
            old.commit()
            rows_before = list(old.execute('SELECT * FROM tasks'))
            artifacts_before = list(old.execute('SELECT * FROM artifacts'))
            migrated = self.current()
            self.assertEqual(migrated['report_policy'], 'legacy')
            self.assertEqual(migrated['report_submission']['status'], 'not_required')
            self.assertEqual(rows_before, list(old.execute('SELECT * FROM tasks')))
            self.assertEqual(artifacts_before, list(old.execute('SELECT * FROM artifacts')))
            self.assertEqual(old.execute('SELECT count(*) FROM task_report_contracts').fetchone()[0], 0)
            backups = list((self.data / 'collaboration-schema-backups').glob('*.sqlite3'))
            self.assertEqual(len(backups), 1)
            with contextlib.closing(sqlite3.connect(backups[0])) as backup:
                self.assertEqual(backup.execute('SELECT summary FROM tasks').fetchone()[0], '历史原始摘要')
        self.assertEqual(self.call('task_finish', **self.owner)['status'], 'completed')

    def test_indexed_unregistered_and_other_task_report_do_not_satisfy_completion(self):
        (Path(self.task['paths']['reports']) / 'inventory.md').write_text('仅文件索引', encoding='utf-8')
        other = self.call('task_create', project='合成', title='别的任务')
        self.report(**self.claim(other))
        self.assert_pending()

    def test_undeclared_and_unclassified_reports_are_pending(self):
        self.call('artifact_write', **self.owner, kind='report', category='report', title='旧报告',
                  filename='undeclared.md', content='未声明')
        self.assert_pending()
        self.report(filename='unknown.md', category='other_text')
        self.assert_pending()
        self.assertIn('明确用途分类', self.current()['report_submission']['reason'])

    def test_handoff_and_same_client_reclaim_cannot_reuse_old_report(self):
        artifact = self.report()
        self.call('task_handoff', **self.owner, target_tool='any', summary='无报告可交接')
        self.owner = self.claim(self.task)
        self.assert_pending()
        fresh = self.report(filename='fresh.md')
        self.assertNotEqual(artifact['id'], fresh['id'])
        self.call('task_finish', **self.owner)

    def test_owner_change_cannot_use_predecessor_report(self):
        self.report()
        self.call('task_requeue', task_id=self.task['id'])
        self.owner = self.claim(self.task, 'two')
        self.assert_pending()

    def test_deleted_changed_or_removed_report_blocks_finish(self):
        artifact = self.report()
        path = Path(artifact['path'])
        path.unlink()
        self.assert_pending()
        artifact = self.report(filename='changed.md')
        path = Path(artifact['path'])
        before = path.stat()
        body = path.read_bytes()
        path.write_bytes(b'X' + body[1:])
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assert_pending()  # finish hashes even if metadata has been preserved

    def test_soft_deleted_report_is_not_completion_evidence(self):
        artifact = self.report()
        with c.store(self.cfg) as (con, root):
            con.execute('UPDATE artifacts SET deleted_at=? WHERE root=? AND id=?', (c._now(), root, artifact['id']))
        self.assert_pending()

    def test_response_lost_retry_recovers_same_artifact_and_candidates(self):
        identifier = str(uuid.uuid4())
        candidates = [{'title': '约定', 'content': '可复用', 'scope': 'project'}]
        lost_response = self.report(submission_id=identifier, memory_candidates=candidates)
        response = self.report(submission_id=identifier, memory_candidates=candidates)
        self.assertEqual(response, lost_response)
        self.assertEqual(len(list(Path(self.task['paths']['reports']).iterdir())), 1)
        self.assertEqual(len(self.call('artifact_list')['items']), 1)
        self.assertEqual(len(self.call('memory_list')['items']), 1)
        self.assertEqual(self.current()['report_submission']['submission_id'], identifier)
        self.assertEqual(self.call('task_finish', **self.owner)['report_submission']['submission_id'], identifier)
        with self.assertRaises(ValueError):
            self.report(submission_id=identifier, memory_candidates=candidates)

    def test_real_http_dropped_success_response_can_be_retried(self):
        captured = []
        class DropOnce(server.Handler):
            def log_message(self, *_args):
                pass
            def _send(self, status, headers, body):
                if self.path == '/api/collaboration/mcp/artifact_write' and status == 200 and not captured:
                    captured.append(json.loads(body))
                    self.close_connection = True  # business transaction already committed
                    return
                super()._send(status, headers, body)
        with patch.object(server, 'CFG', self.cfg), patch.object(server, 'DB_OBJ', None):
            httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), DropOnce)
            thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
            thread.start()
            body = dict(self.owner, kind='report', category='report', title='HTTP验收', filename='http.md',
                        content='本次报告', memory_candidates=[{'title': '约定', 'content': '可复用', 'scope': 'workspace'}],
                        submission_id=str(uuid.uuid4()))
            def send():
                connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
                try:
                    connection.request('POST', '/api/collaboration/mcp/artifact_write', json.dumps(body).encode(),
                                       {'Content-Type': 'application/json'})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    return json.loads(response.read())
                finally:
                    connection.close()
            try:
                with self.assertRaises(http.client.RemoteDisconnected):
                    send()
                retry = send()
                self.assertEqual(retry['id'], captured[0]['id'])
                self.assertEqual(retry['memory_candidate_ids'], captured[0]['memory_candidate_ids'])
                self.assertEqual(len(self.call('artifact_list')['items']), 1)
                self.assertEqual(len(self.call('memory_list')['items']), 1)
                self.assertEqual(self.call('task_finish', **self.owner)['report_submission']['status'], 'submitted')
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(3)
                self.assertFalse(thread.is_alive())

    def test_submission_id_conflict_and_old_claim_cannot_replay(self):
        identifier = str(uuid.uuid4())
        self.report(submission_id=identifier)
        with self.assertRaisesRegex(ValueError, '内容不一致'):
            self.report(submission_id=identifier, content='另一份内容')
        old_owner = dict(self.owner)
        self.call('task_requeue', task_id=self.task['id'])
        self.owner = self.claim(self.task)
        with self.assertRaisesRegex(ValueError, '前一次领取'):
            self.report(submission_id=identifier)
        with self.assertRaisesRegex(ValueError, '有效领取凭据'):
            self.report(**old_owner, submission_id=identifier)
        self.assert_pending()

    def test_exact_task_lookup_is_not_limited_by_newer_task_page(self):
        with c.store(self.cfg) as (con, root):
            for number in range(c.LIMIT + 5):
                con.execute('''INSERT INTO tasks(id,root,project,title,description,target_tool,status,
                    created_at,updated_at,paths) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (str(uuid.uuid4()), root, '合成', '新任务%d' % number, '', 'any', 'queued',
                     c._now('2100-01-01T00:00:00Z'), c._now('2100-01-01T00:00:00Z'), json.dumps(self.task['paths'])))
        self.assertNotIn(self.task['id'], [task['id'] for task in self.call('task_list')['items']])
        exact = collaboration_api.execute(self.cfg, 'task_list', {'client_id': 'one', 'task_id': self.task['id']}, actor='mcp')
        self.assertEqual([task['id'] for task in exact['items']], [self.task['id']])
        self.assertEqual(exact['items'][0]['status'], 'active')
        self.assertEqual(self.call('task_list', task_id=str(uuid.uuid4()))['items'], [])

    def test_optional_and_legacy_receipt_recovers_after_task_completed(self):
        for policy in ('optional', 'legacy'):
            with self.subTest(policy=policy):
                self.task = self.call('task_create', project='合成', title='完成后恢复', report_policy='optional')
                if policy == 'legacy':
                    with c.store(self.cfg) as (con, root):
                        con.execute('DELETE FROM task_report_contracts WHERE root=? AND task_id=?', (root, self.task['id']))
                self.owner = self.claim(self.task)
                identifier = str(uuid.uuid4())
                report = self.report(submission_id=identifier,
                                     memory_candidates=[{'title': '恢复约定', 'content': '可复用', 'scope': 'workspace'}])
                self.call('task_finish', **self.owner)
                self.assertEqual(self.current()['report_submission']['status'], 'not_required')
                receipt = collaboration_api.execute(self.cfg, 'submission_receipt',
                    {'task_id': self.task['id'], 'submission_id': identifier, 'client_id': 'one'}, actor='mcp')
                self.assertTrue(receipt['found'] and receipt['valid'])
                self.assertEqual((receipt['artifact_id'], receipt['memory_candidate_ids']),
                                 (report['id'], report['memory_candidate_ids']))
                self.assertNotIn('lease', json.dumps(receipt))
                self.assertNotIn(self.owner['lease_token'], json.dumps(receipt))
                with self.assertRaisesRegex(ValueError, '有效领取凭据'):
                    self.report(submission_id=identifier)

    def test_receipt_is_client_workspace_scoped_and_live_file_validated(self):
        identifier = str(uuid.uuid4())
        report = self.report(submission_id=identifier)
        query = {'task_id': self.task['id'], 'submission_id': identifier, 'client_id': 'one'}
        self.assertTrue(self.call('submission_receipt', **query)['valid'])
        self.assertFalse(self.call('submission_receipt', **dict(query, client_id='two'))['found'])
        self.assertFalse(self.call('submission_receipt', **dict(query, task_id=str(uuid.uuid4())))['found'])
        self.assertFalse(self.call('submission_receipt', **dict(query, submission_id=str(uuid.uuid4())))['found'])
        other = self.base / 'other-workspace'
        other.mkdir()
        cfg = dict(self.cfg, ai_root=str(other))
        harnesses.save(cfg, {'id': 'codex', 'revision': 0, 'connection_mode': 'mcp_stdio'})
        c.execute(cfg, 'client_heartbeat', {'client_id': 'one', 'tool': 'codex', 'name': 'one', 'protocol_version': 1})
        self.assertFalse(c.execute(cfg, 'submission_receipt', query)['found'])
        path = Path(report['path'])
        before = path.stat()
        body = path.read_bytes()
        path.write_bytes(b'X' + body[1:])
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        invalid = self.call('submission_receipt', **query)
        self.assertTrue(invalid['found'])
        self.assertFalse(invalid['valid'])
        self.assertEqual(invalid['error_code'], 'submission_invalid')
        self.assertNotIn('path', invalid)

    def test_removed_receipt_source_cannot_confirm_submission(self):
        identifier = str(uuid.uuid4())
        report = self.report(submission_id=identifier)
        query = {'task_id': self.task['id'], 'submission_id': identifier, 'client_id': 'one'}
        with c.store(self.cfg) as (con, root):
            con.execute('UPDATE artifacts SET deleted_at=? WHERE root=? AND id=?', (c._now(), root, report['id']))
        receipt = self.call('submission_receipt', **query)
        self.assertTrue(receipt['found'])
        self.assertFalse(receipt['valid'])

    def test_new_report_size_is_bounded_while_large_outputs_still_register(self):
        path = Path(self.task['paths']['reports']) / 'large-report.md'
        path.write_bytes(b'x' * (c.MAX_REPORT_BYTES + 1))
        with self.assertRaisesRegex(ValueError, '大小上限'):
            self.call('artifact_register', **self.owner, kind='report', category='report', title='过大报告',
                      path=str(path), memory_candidates=[], submission_id=str(uuid.uuid4()))
        self.assertTrue(path.exists())  # existing registered source is never deleted on rejection
        self.assertEqual(self.call('artifact_list')['items'], [])
        output = Path(self.task['paths']['outputs']) / 'large-output.txt'
        output.write_bytes(path.read_bytes())
        registered = self.call('artifact_register', **self.owner, kind='output', category='delivery',
                               title='大型资料', path=str(output))
        self.assertEqual(registered['size'], c.MAX_REPORT_BYTES + 1)

    def test_required_task_report_hashing_has_a_per_task_budget(self):
        for number in range(3):
            artifact = self.report(filename='invalid-%d.md' % number, content='x' * c.MAX_REPORT_BYTES)
            path = Path(artifact['path'])
            before = path.stat()
            with path.open('r+b') as handle:
                handle.write(b'y')
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        original = c.file_snapshot
        read_sizes = []
        def counted(path, *args, **kwargs):
            read_sizes.append(Path(path).stat().st_size)
            return original(path, *args, **kwargs)
        with patch.object(c, 'file_snapshot', side_effect=counted):
            self.assertEqual(self.current()['report_submission']['status'], 'pending')
        self.assertLessEqual(sum(read_sizes), c.REPORT_VALIDATION_BYTES)
        self.assertEqual(len(read_sizes), 2)

    def test_receipt_carries_exact_request_fingerprint_and_original_action(self):
        identifier = str(uuid.uuid4())
        payload = dict(self.owner, title='中文报告', category='report', filename='fingerprint.md',
                       content='中文\n雪☃', memory_candidates=[], submission_id=identifier)
        report = self.call('report_submit', **payload)
        receipt = self.call('submission_receipt', task_id=self.task['id'], submission_id=identifier, client_id='one')
        normalized = {key: value for key, value in dict(payload, kind='report').items()
                      if key not in {'lease_token', 'client_id', 'task_id', '_workspace_root', 'submission_id'}}
        expected = hashlib.sha256(json.dumps({'action': 'artifact_write', 'payload': normalized},
            ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
        self.assertEqual(receipt['payload_hash'], expected)
        self.assertEqual((receipt['artifact_id'], receipt['submission_action'], receipt['kind']),
                         (report['id'], 'artifact_write', 'report'))
        different = c._submission_payload_hash(dict(payload, kind='report', content='待交正文 B'), 'artifact_write')
        self.assertNotEqual(receipt['payload_hash'], different)
        with self.assertRaisesRegex(ValueError, '内容不一致'):
            self.call('report_submit', **dict(payload, content='待交正文 B'))
        output_id = str(uuid.uuid4())
        output_payload = dict(self.owner, kind='output', category='delivery', title='输出',
                              filename='output.md', content='输出 A', submission_id=output_id)
        self.call('artifact_write', **output_payload)
        output_receipt = self.call('submission_receipt', task_id=self.task['id'], submission_id=output_id, client_id='one')
        self.assertEqual(output_receipt['kind'], 'output')
        self.assertNotEqual(output_receipt['payload_hash'], c._submission_payload_hash(dict(output_payload, kind='report'), 'artifact_write'))
        path = Path(self.task['paths']['reports']) / 'registered-fingerprint.md'
        path.write_text('登记正文', encoding='utf-8')
        registered_id = str(uuid.uuid4())
        self.call('artifact_register', **self.owner, kind='report', category='report', title='登记',
                  path=str(path), memory_candidates=[], submission_id=registered_id)
        self.assertEqual(self.call('submission_receipt', task_id=self.task['id'], submission_id=registered_id,
                                   client_id='one')['submission_action'], 'artifact_register')

    def test_task_list_and_status_share_a_request_wide_report_byte_budget(self):
        for number in range(6):
            self.task = self.call('task_create', project='合成', title='预算任务%d' % number)
            self.owner = self.claim(self.task)
            self.report(content='x' * c.MAX_REPORT_BYTES)
        original = c.file_snapshot
        read_sizes = []
        def counted(path, *args, **kwargs):
            read_sizes.append(Path(path).stat().st_size)
            return original(path, *args, **kwargs)
        with patch.object(c, 'REPORT_LIST_VALIDATION_BYTES', 2 * c.MAX_REPORT_BYTES + 16), patch.object(c, 'REPORT_LIST_VALIDATION_SECONDS', 100):
            for operation in (lambda: self.call('task_list')['items'], lambda: c.status(self.cfg)['tasks']):
                read_sizes.clear()
                with patch.object(c, 'file_snapshot', side_effect=counted):
                    tasks = operation()
                self.assertLessEqual(sum(read_sizes), 2 * c.MAX_REPORT_BYTES + 16)
                deferred = [task for task in tasks if task['report_submission']['status'] == 'deferred']
                self.assertTrue(deferred)
                self.assertIn('待核验', deferred[0]['report_submission']['reason'])
        # Completion remains a fresh complete validation of this task's report.
        self.assertEqual(self.call('task_finish', **self.owner)['status'], 'completed')

    def test_task_list_deadline_defers_without_skipping_completion_validation(self):
        self.report()
        with patch.object(c, '_report_list_budget', return_value={'remaining_bytes': c.REPORT_LIST_VALIDATION_BYTES, 'deadline': 0}), patch.object(c, 'file_snapshot') as snapshot:
            self.assertEqual(self.current()['report_submission']['status'], 'deferred')
            snapshot.assert_not_called()
        self.assertEqual(self.call('task_finish', **self.owner)['report_submission']['status'], 'submitted')

    def test_registration_idempotency_and_report_submit_alias(self):
        path = Path(self.task['paths']['reports']) / 'registered.md'
        path.write_text('报告', encoding='utf-8')
        body = dict(self.owner, path=str(path), kind='report', category='report', title='登记',
                    memory_candidates=[], submission_id=str(uuid.uuid4()))
        first = self.call('artifact_register', **body)
        self.assertEqual(self.call('artifact_register', **body), first)
        identifier = str(uuid.uuid4())
        body = dict(self.owner, category='report', title='别名', filename='alias.md', content='报告',
                    memory_candidates=[], submission_id=identifier)
        first = collaboration_api.execute(self.cfg, 'report_submit', body, actor='mcp')
        retry = self.call('artifact_write', **body, kind='report')
        self.assertEqual(retry['id'], first['id'])

    def test_candidate_failure_is_atomic_and_does_not_persist_token(self):
        identifier = str(uuid.uuid4())
        original = c._audit
        def fail_candidate(con, root, action, *args, **kwargs):
            if action == 'memory_propose':
                raise sqlite3.OperationalError('candidate failure')
            return original(con, root, action, *args, **kwargs)
        with patch.object(c, '_audit', side_effect=fail_candidate), self.assertRaises(sqlite3.OperationalError):
            self.report(submission_id=identifier, memory_candidates=[{'title': 'x', 'content': 'x', 'scope': 'workspace'}])
        self.assertFalse((Path(self.task['paths']['reports']) / 'report.md').exists())
        self.assertEqual(self.call('artifact_list')['items'], [])
        self.assertEqual(self.call('memory_list')['items'], [])
        self.report(submission_id=identifier)
        public = json.dumps(c.status(self.cfg)) + json.dumps(self.call('task_list'))
        self.assertNotIn(self.owner['lease_token'], public)
        self.assertNotIn('lease_hash', public)
        for file in self.data.rglob('*'):
            if file.is_file():
                self.assertNotIn(self.owner['lease_token'].encode(), file.read_bytes())


if __name__ == '__main__':
    unittest.main()
