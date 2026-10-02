"""Durable execution kernel on owned temporary stores; never native generation."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from aihub import capabilities, collaboration as co, collaboration_resources as resources, config, execution as ex, harnesses, service_control


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihub-execution-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / '工作区'
        self.root.mkdir()
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True}
        p = patch.object(config, 'DATA_DIR', str(self.base / 'data'))
        p.start(); self.addCleanup(p.stop)
        self.identity = {'app': 'ai-hub', 'service_instance_id': 'synthetic-instance',
                         'install_root': str(self.base / 'app'), 'port': 19876,
                         'server_version': 'candidate', 'control_protocol': service_control.PROTOCOL}
        harnesses.save(self.cfg, {'id': 'codex', 'revision': 0, 'connection_mode': 'mcp_stdio'})
        for client in ('publisher', 'other'):
            co.execute(self.cfg, 'client_heartbeat', {'client_id': client, 'tool': 'codex', 'name': client, 'protocol_version': 1})
        self.declaration = {'key': 'fixture.echo', 'name': '合成回声', 'kind': 'mcp_tool', 'domains': ['document'],
                            'inputs': {'type': 'object', 'properties': {'prompt': {'type': 'string'}}, 'required': ['prompt']}}
        self.publish()
        self.source = self.call('grant_create', {'role': 'source', 'subject': 'yingxu-synthetic'})['token']
        self.worker = self.call('grant_create', {'role': 'worker', 'subject': 'publisher'})['token']
        self.wrong_worker = self.call('grant_create', {'role': 'worker', 'subject': 'other'})['token']
        desc = self.call('describe', {})
        item = capabilities.catalog(self.cfg)['items'][0]
        with capabilities.store(self.cfg) as (con, root):
            text = con.execute('SELECT payload FROM capabilities WHERE root=? AND id=?', (root, item['id'])).fetchone()[0]
        inputs = ' \r\n {"prompt":"中文🌞 é"} \r\n '
        self.intent = {'protocol': ex.PROTOCOL, 'request_id': str(uuid.uuid4()),
                       'origin': {'authority_id': 'yingxu-synthetic', 'project_id': 'project-1', 'task_id': 'task-1',
                                  'run_id': 'run-1', 'call_id': 'attempt-1', 'input_revision': 'revision-1'},
                       'workspace_binding_revision': desc['workspace']['binding_revision'],
                       'connection_revision': desc['connection_revision'], 'execution_authority_id': desc['execution_authority_id'],
                       'ledger_epoch': desc['ledger_epoch'], 'capability_id': item['id'],
                       'expected_declaration_sha256': ex._sha(text), 'input_json': inputs, 'input_sha256': ex._sha(inputs),
                       'hub_project': 'Synthetic', 'title': '三端合成任务'}

    def call(self, operation, body, token=None):
        payload = dict(body)
        if operation != 'describe':
            payload.setdefault('_workspace_root', str(self.root))
        return ex.execute(self.cfg, operation, payload, bearer=token, public_identity=self.identity,
                          owner_authorized=operation in {'grant_create', 'grant_revoke', 'grant_list'})

    def publish(self, items=None):
        capabilities.publish(self.cfg, {'client_id': 'publisher', 'capabilities_json': json.dumps(items if items is not None else [self.declaration], ensure_ascii=False)})

    def test_claim_recovery_keeps_resource_cleanup_bound_to_same_claim(self):
        accepted = self.call('accept', self.intent, self.source)
        claim_id = str(uuid.uuid4())
        first = self.claim(accepted, claim_id=claim_id)
        owner = {'task_id': first['task']['id'], 'client_id': 'publisher', 'lease_token': first['lease_token'], '_workspace_root': str(self.root)}
        identity = {'browser_id': 'test-browser', 'session_id': 'test-session', 'tab_id': 'test-tab'}
        resource = resources.execute(self.cfg, 'resource_register', {**owner,
            'resource_type': 'browser_tab', 'identity': identity,
            'ownership': 'task_exclusive', 'temporary': True})
        unrelated_task = co.execute(self.cfg, 'task_create', {'project': 'Synthetic', 'title': 'Unrelated'})
        unrelated_claim = co.execute(self.cfg, 'task_claim', {'task_id': unrelated_task['id'], 'client_id': 'publisher'})
        unrelated = resources.execute(self.cfg, 'resource_register', {'task_id': unrelated_task['id'],
            'client_id': 'publisher', 'lease_token': unrelated_claim['lease_token'], '_workspace_root': str(self.root), 'resource_type': 'browser_tab',
            'identity': {**identity, 'tab_id': 'other-tab'}, 'ownership': 'task_exclusive', 'temporary': True})
        with co.store(self.cfg) as (con, root):
            old_rows = {r['id']: dict(r) for r in con.execute('SELECT * FROM task_resources')}
            original_claim = con.execute('SELECT claim_id FROM task_report_contracts WHERE task_id=?', (first['task']['id'],)).fetchone()[0]
        second = self.claim(accepted, claim_id=claim_id)
        recovered = self.claim(accepted, claim_id=claim_id)
        self.assertTrue(second['lease_rotated'])
        self.assertTrue(recovered['lease_rotated'])
        with co.store(self.cfg) as (con, root):
            rows = {r['id']: dict(r) for r in con.execute('SELECT * FROM task_resources')}
            self.assertEqual(con.execute('SELECT claim_id FROM task_report_contracts WHERE task_id=?', (first['task']['id'],)).fetchone()[0], original_claim)
        self.assertEqual(rows[unrelated['id']], old_rows[unrelated['id']])
        expected = {**old_rows[resource['id']], 'lease_hash': ex._sha(recovered['lease_token'])}
        self.assertEqual(rows[resource['id']], expected)
        evidence = {'identity': identity, 'observed_at': co._now(), 'outcome': 'absent'}
        request = {'resource_id': resource['id'], 'state': 'closed', 'evidence': evidence}
        for stale_token in (first['lease_token'], second['lease_token']):
            with self.assertRaises(ValueError):
                resources.execute(self.cfg, 'resource_cleanup_report', {**owner, **request, 'lease_token': stale_token})
        closed = resources.execute(self.cfg, 'resource_cleanup_report', {**owner, **request, 'lease_token': recovered['lease_token']})
        self.assertEqual(closed['state'], 'closed')

    def accept(self, **changes):
        return self.call('accept', dict(self.intent, **changes), self.source)

    def claim(self, receipt, token=None, claim_id=None):
        return self.call('claim', {'execution_id': receipt['execution_id'], 'claim_request_id': claim_id or str(uuid.uuid4())}, token or self.worker)

    def observe(self, claim, state, **extra):
        return self.call('observe', {'execution_id': claim['execution_id'], 'lease_token': claim['lease_token'],
                                    'observation_id': str(uuid.uuid4()), 'provider_state': state,
                                    'provider_request_id': 'fixture-native-request', 'results': [], **extra}, self.worker)

    def counts(self):
        with co.store(self.cfg) as (con, _):
            return tuple(con.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ('executions', 'tasks'))

    def test_concurrent_accept_is_one_durable_task_and_exact_input(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            receipts = list(pool.map(lambda _: self.accept(), range(12)))
        self.assertEqual(len({r['execution_id'] for r in receipts}), 1)
        self.assertEqual(self.counts(), (1, 1))
        claim = self.claim(receipts[0])
        self.assertEqual(claim['input_json'], self.intent['input_json'])
        self.assertEqual(claim['input_sha256'], hashlib.sha256(self.intent['input_json'].encode('utf-8')).hexdigest())
        self.assertEqual(receipts[0]['dispatch_state'], 'queued_ready')

    def test_worker_inbox_scopes_pages_and_does_not_claim(self):
        receipts = [self.accept(request_id=str(uuid.uuid4())) for _ in range(3)]
        # Equal creation times must still page without repeats or missing rows.
        with co.store(self.cfg) as (con, _):
            con.execute("UPDATE executions SET created_at='2026-10-02T00:00:00Z'")
        first = self.call('inbox', {'limit': 2}, self.worker)
        self.assertTrue(first['has_more'])
        second = self.call('inbox', {'limit': 2, 'after_execution_id': first['next_after_execution_id']}, self.worker)
        identifiers = [item['execution_id'] for item in first['items'] + second['items']]
        self.assertEqual(identifiers, sorted(r['execution_id'] for r in receipts))
        self.assertFalse(second['has_more'])
        self.assertIsNone(second['next_after_execution_id'])
        self.assertFalse(first['claim_performed'])
        self.assertFalse(first['native_work_started'])
        self.assertEqual(self.call('inbox', {}, self.worker), self.call('inbox', {}, self.worker))
        self.assertTrue(all(item['dispatch_state'] == 'queued_ready' for item in first['items']))
        self.assertEqual(self.call('inbox', {}, self.wrong_worker)['items'], [])
        serialized = json.dumps(first)
        for private in ('lease_token', 'input_json', 'declaration_text', str(self.root), self.worker):
            self.assertNotIn(private, serialized)
        self.assertEqual(self.counts(), (3, 3))

    def test_worker_inbox_rejects_other_roles_cursors_limits_and_revoked_grants(self):
        receipt = self.accept()
        for token in (self.source, None):
            with self.assertRaises(ex.ExecutionError) as error:
                self.call('inbox', {}, token)
            self.assertEqual(error.exception.code, 'credential_denied')
        with self.assertRaises(ex.ExecutionError) as error:
            self.call('inbox', {'after_execution_id': receipt['execution_id']}, self.wrong_worker)
        self.assertEqual(error.exception.code, 'scope_denied')
        for limit in (True, 0, 26, '2', 1.5):
            with self.assertRaises(ex.ExecutionError) as error:
                self.call('inbox', {'limit': limit}, self.worker)
            self.assertEqual(error.exception.code, 'invalid_request')
        with self.assertRaises(ex.ExecutionError):
            self.call('inbox', {'execution_id': receipt['execution_id']}, self.worker)
        grant = self.call('grant_create', {'role': 'worker', 'subject': 'publisher'})
        self.call('grant_revoke', {'grant_id': grant['grant_id']})
        with self.assertRaises(ex.ExecutionError) as error:
            self.call('inbox', {}, grant['token'])
        self.assertEqual(error.exception.code, 'credential_denied')

    def test_worker_inbox_keeps_terminal_report_recovery_until_finished(self):
        cancelled = self.accept()
        self.call('cancel', {'execution_id': cancelled['execution_id']}, self.source)
        pending = self.accept(request_id=str(uuid.uuid4()))
        claim = self.claim(pending)
        self.observe(claim, 'submitting')
        self.call('cancel', {'execution_id': claim['execution_id']}, self.source)
        self.observe(claim, 'failed', error_code='synthetic_failure')
        items = self.call('inbox', {}, self.worker)['items']
        self.assertEqual([item['execution_id'] for item in items], [claim['execution_id']])
        self.assertEqual(items[0]['provider_state'], 'failed')
        self.assertTrue(items[0]['cancel_requested'])
        owner = {'task_id': pending['queue_task_id'], 'client_id': 'publisher', 'lease_token': claim['lease_token']}
        co.execute(self.cfg, 'report_submit', dict(owner, category='report', filename='failure.md',
            title='故障报告', content='合成故障结论', submission_id=str(uuid.uuid4()), memory_candidates=[]))
        co.execute(self.cfg, 'task_finish', dict(owner, summary='已报告失败'))
        self.assertEqual(self.call('inbox', {}, self.worker)['items'], [])
        # A closed page marker still identifies its old position, without re-claiming.
        self.assertEqual(self.call('inbox', {'after_execution_id': pending['execution_id']}, self.worker)['items'], [])

    def test_retry_after_republication_revocation_and_instance_restart(self):
        receipt = self.accept()
        self.declaration['name'] = '新声明'
        self.publish()
        self.identity['service_instance_id'] = 'restarted-instance'
        replay = self.accept()
        self.assertEqual(receipt, replay)
        self.publish([])
        self.assertEqual(self.accept()['execution_id'], receipt['execution_id'])
        with self.assertRaisesRegex(ex.ExecutionError, '当前服务'):
            self.accept(request_id=str(uuid.uuid4()))
        self.assertEqual(self.counts(), (1, 1))

    def test_same_request_different_inputs_or_scope_conflicts(self):
        self.accept()
        changed = '{"prompt":"changed"}'
        with self.assertRaisesRegex(ex.ExecutionError, '其他业务'):
            self.accept(input_json=changed, input_sha256=ex._sha(changed))
        foreign = dict(self.intent['origin'], authority_id='other-software')
        with self.assertRaisesRegex(ex.ExecutionError, '来源接入'):
            self.accept(origin=foreign)
        self.assertEqual(self.counts(), (1, 1))

    def test_prior_receipt_prevents_silent_accept_after_missing_history(self):
        receipt = self.accept()
        self.assertEqual(self.accept(prior_execution_id=receipt['execution_id']), receipt)
        with self.assertRaisesRegex(ex.ExecutionError, '身份不匹配'):
            self.accept(prior_execution_id=str(uuid.uuid4()))
        with self.assertRaisesRegex(ex.ExecutionError, '先前执行收据'):
            self.accept(request_id=str(uuid.uuid4()), prior_execution_id=receipt['execution_id'])
        self.assertEqual(self.counts(), (1, 1))

    def test_existing_execution_schema_migration_uses_committed_sqlite_backup(self):
        receipt = self.accept()
        with co.store(self.cfg) as (con, _):
            con.execute('ALTER TABLE executions DROP COLUMN outcome_json')
        backup_dir = Path(config.DATA_DIR) / 'collaboration-schema-backups'
        before = set(backup_dir.glob('*.sqlite3')) if backup_dir.exists() else set()
        current = self.call('status', {'execution_id': receipt['execution_id']}, self.source)
        self.assertEqual(current['outcome'], {})
        added = set(backup_dir.glob('*.sqlite3')) - before
        self.assertEqual(len(added), 1)
        import contextlib, sqlite3
        with contextlib.closing(sqlite3.connect(str(added.pop()))) as backup:
            self.assertEqual(backup.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(backup.execute('SELECT COUNT(*) FROM executions').fetchone()[0], 1)
            self.assertNotIn('outcome_json', {row[1] for row in backup.execute('PRAGMA table_info(executions)')})

    def test_first_execution_upgrade_keeps_old_store_backup(self):
        import contextlib, sqlite3
        backups = list((Path(config.DATA_DIR) / 'collaboration-schema-backups').glob('*.sqlite3'))
        self.assertEqual(len(backups), 1)
        with contextlib.closing(sqlite3.connect(str(backups[0]))) as con:
            self.assertEqual(con.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(con.execute('SELECT COUNT(*) FROM clients').fetchone()[0], 2)
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn('executions', tables)
            self.assertNotIn('execution_grants', tables)

    def test_pending_materialization_protects_workspace_detach(self):
        with patch.object(ex, '_materialize', side_effect=ConnectionError('accepted pending')):
            with self.assertRaises(ConnectionError): self.accept()
        self.assertFalse(co.can_stop(self.cfg))
        receipt = self.call('status', {'request_id': self.intent['request_id']}, self.source)
        self.call('cancel', {'execution_id': receipt['execution_id']}, self.source)
        self.assertTrue(co.can_stop(self.cfg))

    def test_result_sha_is_hexadecimal_text_not_lossy_numeric_json(self):
        result = {'result_id': 'fixture-result', 'kind': 'document', 'media_type': 'text/plain',
                  'bytes': 1, 'locator': 'fixture/output', 'sha256': int('1' * 64)}
        with self.assertRaises(ex.ExecutionError): ex._results([result])
        self.assertEqual(ex._results([dict(result, sha256='1' * 64)])[0]['sha256'], '1' * 64)

    def test_cas_and_input_errors_have_no_partial_tasks(self):
        self.declaration['name'] = '改变声明'; self.publish()
        with self.assertRaisesRegex(ex.ExecutionError, '声明'):
            self.accept()
        for text in ('{"prompt":"a","prompt":"b"}', '{"prompt":NaN}', '{"prompt":"ok","token":"no"}'):
            with self.assertRaises(ValueError):
                self.accept(input_json=text, input_sha256=ex._sha(text))
        with self.assertRaises(ValueError):
            self.accept(input_sha256='0' * 64)
        self.assertEqual(self.counts(), (0, 0))

    def test_materialization_commit_gap_recovers_original_identity(self):
        with patch.object(ex, '_materialize', side_effect=ConnectionError('lost after commit')):
            with self.assertRaises(ConnectionError):
                self.accept()
        self.assertEqual(self.counts(), (1, 1))
        receipt = self.call('status', {'request_id': self.intent['request_id']}, self.source)
        self.assertEqual(receipt['dispatch_state'], 'materialization_pending')
        with self.assertRaises(ex.ExecutionError):
            self.claim(receipt)
        replay = self.accept()
        self.assertEqual(replay['execution_id'], receipt['execution_id'])
        self.assertEqual(replay['dispatch_state'], 'queued_ready')

    def test_process_exit_after_accept_commit_recovers_original_task(self):
        script = '''import json,os,sys
from aihub import config,execution
value=json.load(sys.stdin)
config.DATA_DIR=value['data']
execution._materialize=lambda *args:os._exit(73)
execution.execute(value['cfg'],'accept',value['body'],bearer=value['bearer'],public_identity=value['identity'])
'''
        value = {'cfg': self.cfg, 'data': config.DATA_DIR, 'body': dict(self.intent, _workspace_root=str(self.root)),
                 'bearer': self.source, 'identity': self.identity}
        result = subprocess.run([sys.executable, '-B', '-c', script], input=json.dumps(value).encode('utf-8'),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        self.assertEqual(result.returncode, 73, 'owned fixture process did not reach commit exit')
        pending = self.call('status', {'request_id': self.intent['request_id']}, self.source)
        self.assertEqual(pending['dispatch_state'], 'materialization_pending')
        ready = self.accept()
        self.assertEqual(ready['execution_id'], pending['execution_id'])
        self.assertEqual(self.counts(), (1, 1))

    def test_accept_transaction_failure_creates_no_task_directory(self):
        with patch.object(co, '_audit', side_effect=RuntimeError('owned fixture transaction failure')):
            with self.assertRaises(RuntimeError): self.accept()
        self.assertEqual(self.counts(), (0, 0))
        self.assertFalse((self.root / '40_Projects').exists())

    def test_unknown_files_block_recovery_without_overwrite(self):
        with patch.object(ex, '_materialize', side_effect=ConnectionError('gap')):
            with self.assertRaises(ConnectionError): self.accept()
        with co.store(self.cfg) as (con, _):
            task = con.execute('SELECT paths FROM tasks').fetchone()
        work = Path(json.loads(task['paths'])['work']); work.mkdir(parents=True)
        user_file = work / 'important.txt'; user_file.write_text('preserve', encoding='utf-8')
        receipt = self.accept()
        self.assertEqual(receipt['dispatch_state'], 'materialization_pending')
        self.assertEqual(receipt['materialization_error'], 'path_or_content_conflict')
        self.assertEqual(user_file.read_text(encoding='utf-8'), 'preserve')
        self.assertEqual(self.counts(), (1, 1))

    def test_precise_claim_and_legacy_routes_cannot_bypass(self):
        receipt = self.accept()
        with self.assertRaises(ex.ExecutionError): self.claim(receipt, self.wrong_worker)
        with self.assertRaisesRegex(ValueError, 'execution/claim'):
            co.execute(self.cfg, 'task_claim', {'task_id': receipt['queue_task_id'], 'client_id': 'publisher'})
        claim = self.claim(receipt)
        owner = {'task_id': receipt['queue_task_id'], 'client_id': 'publisher', 'lease_token': claim['lease_token']}
        for action, payload in [('task_requeue', owner), ('task_handoff', dict(owner, target_tool='any', summary='handoff')),
                                ('task_finish', dict(owner, summary='done')),
                                ('record_delete_preview', {'_workspace_root': str(self.root), 'entity_type': 'task', 'entity_id': receipt['queue_task_id']})]:
            with self.assertRaises(ValueError): co.execute(self.cfg, action, payload)

    def test_lost_claim_reply_rotates_lease_but_not_report_claim(self):
        receipt = self.accept(); request = str(uuid.uuid4())
        first = self.claim(receipt, claim_id=request)
        with co.store(self.cfg) as (con, root): claim_id = con.execute('SELECT claim_id FROM task_report_contracts').fetchone()[0]
        second = self.claim(receipt, claim_id=request)
        self.assertTrue(second['lease_rotated']); self.assertNotEqual(first['lease_token'], second['lease_token'])
        with co.store(self.cfg) as (con, root):
            self.assertEqual(con.execute('SELECT claim_id FROM task_report_contracts').fetchone()[0], claim_id)
            with self.assertRaises(ValueError):
                co._lease(con, root, {'task_id': receipt['queue_task_id'], 'client_id': 'publisher', 'lease_token': first['lease_token']})

    def test_report_memory_and_terminal_execution_remain_distinct(self):
        receipt = self.accept(); claim = self.claim(receipt)
        self.observe(claim, 'submitting'); self.observe(claim, 'running')
        result = {'result_id': 'result-fixed', 'kind': 'document', 'media_type': 'text/plain', 'bytes': 2,
                  'sha256': ex._sha('ok'), 'locator': 'fixture/result-fixed'}
        final = self.observe(claim, 'succeeded', results=[result])
        self.assertEqual(final['provider_state'], 'succeeded')
        owner = {'task_id': receipt['queue_task_id'], 'client_id': 'publisher', 'lease_token': claim['lease_token']}
        with self.assertRaisesRegex(ValueError, '报告'):
            co.execute(self.cfg, 'task_finish', dict(owner, summary='success'))
        report = co.execute(self.cfg, 'report_submit', dict(owner, category='report', filename='report.md', title='分类报告',
            submission_id=str(uuid.uuid4()), content='合成执行结果，有明确来源。', memory_candidates=[{'title': '合成约定', 'content': '只有审核后的内容进入共享记忆', 'scope': 'project'}]))
        task = co.execute(self.cfg, 'task_finish', dict(owner, summary='done'))
        self.assertEqual(task['execution']['dispatch_state'], 'completed')
        self.assertEqual(co.execute(self.cfg, 'memory_search', {})['items'], [])
        status = self.call('status', {'execution_id': receipt['execution_id']}, self.source)
        self.assertEqual(status['results'], [result])
        self.assertFalse(status['native_execution_verified_by_hub'])

    def test_cancel_and_claim_race_is_atomic_and_running_not_falsely_cancelled(self):
        receipt = self.accept()
        cancelled = self.call('cancel', {'execution_id': receipt['execution_id']}, self.source)
        self.assertEqual(cancelled['provider_state'], 'cancelled')
        with self.assertRaises(ex.ExecutionError): self.claim(receipt)
        new = self.accept(request_id=str(uuid.uuid4())); claim = self.claim(new)
        self.observe(claim, 'submitting'); self.observe(claim, 'running')
        requested = self.call('cancel', {'execution_id': new['execution_id']}, self.source)
        self.assertTrue(requested['cancel_requested']); self.assertEqual(requested['provider_state'], 'running')
        with self.assertRaises(ValueError): self.observe(claim, 'cancelled')
        with self.assertRaises(ex.ExecutionError):
            self.observe(claim, 'cancelled', cancel_evidence={'kind': 'never_submitted', 'reference': 'wrong'})
        cancelled = self.observe(claim, 'cancelled', cancel_evidence={'kind': 'native_terminal', 'reference': 'fixture-job-terminal'})
        self.assertEqual(cancelled['provider_state'], 'cancelled')
        recovered = self.call('status', {'execution_id': claim['execution_id']}, self.source)
        self.assertEqual(recovered['outcome']['cancel_evidence'], {'kind': 'native_terminal', 'reference': 'fixture-job-terminal'})

    def test_concurrent_cancel_claim_has_one_legal_outcome(self):
        receipt = self.accept()
        def claim():
            try: return self.claim(receipt)
            except ex.ExecutionError: return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = pool.submit(claim)
            cancelled = pool.submit(self.call, 'cancel', {'execution_id': receipt['execution_id']}, self.source)
            a, b = claimed.result(), cancelled.result()
        current = self.call('status', {'execution_id': receipt['execution_id']}, self.source)
        if a is None:
            self.assertEqual(current['provider_state'], 'cancelled')
            self.assertEqual(current['dispatch_state'], 'cancelled_before_claim')
        else:
            self.assertEqual(current['dispatch_state'], 'claimed')
            self.assertEqual(current['provider_state'], 'not_started')
            self.assertTrue(current['cancel_requested'])

    def test_results_reordering_preserves_observation_identity(self):
        receipt = self.accept(); claim = self.claim(receipt)
        self.observe(claim, 'submitting')
        results = [{'result_id': key, 'kind': 'document', 'media_type': 'text/plain', 'bytes': 2,
                    'sha256': ex._sha('ok'), 'locator': 'fixture/' + key} for key in ('b-result', 'a-result')]
        payload = {'execution_id': claim['execution_id'], 'lease_token': claim['lease_token'],
                   'observation_id': str(uuid.uuid4()), 'provider_state': 'succeeded',
                   'provider_request_id': 'fixture-native-request', 'results': results}
        first = self.call('observe', payload, self.worker)
        second = self.call('observe', dict(payload, results=list(reversed(results))), self.worker)
        self.assertEqual(first, second)
        self.assertEqual([r['result_id'] for r in first['results']], ['a-result', 'b-result'])

    def test_unicode_result_identity_and_mime_parameters_survive_receipt(self):
        receipt = self.accept(); claim = self.claim(receipt)
        self.observe(claim, 'submitting')
        result = {'result_id': '成片_第一版-01', 'kind': 'document',
                  'media_type': 'text/plain; charset=utf-8; title="draft report"',
                  'bytes': 2, 'locator': 'fixture/result-one', 'sha256': ex._sha('ok')}
        final = self.observe(claim, 'succeeded', results=[result])
        self.assertEqual(final['results'], [result])
        recovered = self.call('status', {'execution_id': claim['execution_id']}, self.source)
        self.assertEqual(recovered['results'], [result])
        self.assertEqual(final['results_manifest_sha256'], recovered['results_manifest_sha256'])
        # Protocol order is Unicode scalar order, independent of a UI locale or
        # JavaScript's default UTF-16 code-unit comparison for supplementary IDs.
        rows = [dict(result, result_id=key) for key in ('🟡-result', '成片-01', '\ue000-result')]
        self.assertEqual([item['result_id'] for item in ex._results(rows)], ['成片-01', '\ue000-result', '🟡-result'])

    def test_result_metadata_rejects_paths_and_header_control_text(self):
        receipt = self.accept(); claim = self.claim(receipt)
        self.observe(claim, 'submitting')
        valid = {'result_id': 'result-one', 'kind': 'document', 'media_type': 'text/plain',
                 'bytes': 2, 'locator': 'fixture/result-one'}
        invalid = [('result_id', value) for value in (
            'C:\\Users\\private\\report.md', '/home/private/report', '../private', 'folder/report',
            'https:private', 'file:private', '..', 'private\x00suffix', 'private\x85suffix', 'bad\ud800')]
        invalid += [('media_type', value) for value in (
            'C:\\Users\\private\\report.md', 'not a mime', 'text/plain\r\nX-Test: private',
            'text/plain; charset="bad\x00value"', 'text/plain; charset=', 'text/plain; charset=utf-8;')]
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    self.observe(claim, 'succeeded', results=[dict(valid, **{field: value})])
        self.assertEqual(self.call('status', {'execution_id': claim['execution_id']}, self.source)['provider_state'], 'submitting')

    def test_native_identity_and_outcome_are_immutable_after_uncertainty(self):
        receipt = self.accept(); claim = self.claim(receipt)
        self.observe(claim, 'submitting'); self.observe(claim, 'uncertain')
        with self.assertRaises(ex.ExecutionError): self.observe(claim, 'submitting')
        with self.assertRaises(ex.ExecutionError): self.observe(claim, 'running', provider_request_id='new-native-request')
        self.observe(claim, 'failed', error_code='fixture_failed')
        self.assertEqual(self.call('status', {'execution_id': claim['execution_id']}, self.source)['outcome']['error_code'], 'fixture_failed')
        with self.assertRaises(ex.ExecutionError): self.observe(claim, 'running')

    def test_cancel_evidence_and_errors_do_not_export_paths(self):
        receipt = self.accept(); claim = self.claim(receipt)
        self.observe(claim, 'submitting')
        for value in ('C:\\Users\\private\\report.md', '/home/private/report', '../private', 'https://private.example/job', 'file:private'):
            with self.subTest(value=value):
                with self.assertRaises(ex.ExecutionError):
                    self.observe(claim, 'cancelled', cancel_evidence={'kind': 'native_terminal', 'reference': value})
                with self.assertRaises(ex.ExecutionError): self.observe(claim, 'failed', error_code=value)
        self.assertEqual(self.call('status', {'execution_id': claim['execution_id']}, self.source)['provider_state'], 'submitting')

    def test_outcome_fields_only_belong_to_matching_terminal_state(self):
        receipt = self.accept(); claim = self.claim(receipt)
        self.observe(claim, 'submitting')
        for state in ('running', 'uncertain', 'cancelled'):
            extra = {'error_code': 'wrong_outcome'}
            if state == 'cancelled': extra['cancel_evidence'] = {'kind': 'native_terminal', 'reference': 'fixture-terminal'}
            with self.assertRaises(ex.ExecutionError): self.observe(claim, state, **extra)
        with self.assertRaises(ex.ExecutionError):
            self.observe(claim, 'failed', error_code='fixture_error', cancel_evidence={'kind': 'native_terminal', 'reference': 'fixture-terminal'})
        self.assertEqual(self.call('status', {'execution_id': claim['execution_id']}, self.source)['provider_state'], 'submitting')

    def test_grant_rotation_preserves_namespace_revocation_and_read_scope(self):
        receipt = self.accept()
        grant = self.call('grant_create', {'role': 'source', 'subject': 'yingxu-synthetic'})
        self.assertEqual(self.call('accept', self.intent, grant['token'])['execution_id'], receipt['execution_id'])
        reader = self.call('grant_create', {'role': 'source_read', 'subject': 'yingxu-synthetic'})
        self.assertEqual(self.call('status', {'request_id': self.intent['request_id']}, reader['token'])['execution_id'], receipt['execution_id'])
        with self.assertRaises(ex.ExecutionError): self.call('accept', self.intent, reader['token'])
        self.call('grant_revoke', {'grant_id': grant['grant_id']})
        with self.assertRaises(ex.ExecutionError): self.call('accept', self.intent, grant['token'])
        with co.store(self.cfg) as (con, _):
            storage = json.dumps([dict(r) for r in con.execute('SELECT * FROM execution_grants')])
        self.assertNotIn(self.source, storage); self.assertNotIn(self.worker, storage)

    def test_describe_empty_never_initializes_store(self):
        isolated = dict(self.cfg, ai_root='', workspace_managed=False)
        directory = self.base / 'unused'
        with patch.object(config, 'DATA_DIR', str(directory)):
            result = ex.execute(isolated, 'describe', {}, public_identity=self.identity)
        self.assertIsNone(result['execution_authority_id']); self.assertFalse(directory.exists())

    def test_owner_grant_list_is_scoped_paged_readonly_and_never_returns_keys(self):
        created = self.call('grant_create', {'role': 'source_read', 'subject': 'read-only-origin'})
        self.call('grant_revoke', {'grant_id': created['grant_id']})
        with co.store(self.cfg) as (con, root):
            con.execute('UPDATE execution_grants SET created_at=?', ('2026-10-02T00:00:00+00:00',))
            count = con.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
            bad_cursor = str(uuid.uuid4())
            con.execute('INSERT INTO execution_grants VALUES(?,?,?,?,?,?,?,NULL)',
                        (bad_cursor, root, 'different-binding', 'source', 'foreign-origin', ex._sha('SYNTHETIC_PRIVATE_ONLY'), '2026-10-02T00:00:00+00:00'))
        pages, cursor = [], None
        while True:
            result = self.call('grant_list', {'limit': 1, **({'after_grant_id': cursor} if cursor else {})})
            pages += result['items']
            if not result['has_more']:
                self.assertIsNone(result['next_after_grant_id']); break
            cursor = result['next_after_grant_id']
        self.assertEqual(len(pages), 4)
        self.assertEqual(len({r['grant_id'] for r in pages}), 4)
        self.assertTrue(next(r for r in pages if r['grant_id'] == created['grant_id'])['revoked_at'])
        for row in pages:
            self.assertEqual(set(row), {'grant_id', 'role', 'subject', 'created_at', 'revoked_at'})
        public = json.dumps(result)
        for secret in (self.source, self.worker, self.wrong_worker, created['token'], ex._sha(created['token']), 'SYNTHETIC_PRIVATE_ONLY', str(self.root)):
            self.assertNotIn(secret, public)
        with co.store(self.cfg) as (con, _):
            self.assertEqual(con.execute('SELECT COUNT(*) FROM audit').fetchone()[0], count)
        for owner, bearer in ((False, None), (True, self.source), (True, self.worker)):
            with self.assertRaises(ex.ExecutionError) as caught:
                ex.execute(self.cfg, 'grant_list', {'_workspace_root': str(self.root)}, bearer=bearer, owner_authorized=owner)
            self.assertEqual(caught.exception.code, 'owner_operation')
        for limit in (False, 0, 51, '1'):
            with self.assertRaises(ex.ExecutionError): self.call('grant_list', {'limit': limit})
        for identifier, code in ((bad_cursor, 'scope_denied'), (str(uuid.uuid4()), 'grant_unknown')):
            with self.assertRaises(ex.ExecutionError) as caught: self.call('grant_list', {'after_grant_id': identifier})
            self.assertEqual(caught.exception.code, code)

    def test_owner_grant_list_empty_never_initializes_or_migrates_store(self):
        directory = self.base / 'no-store-yet'
        with patch.object(config, 'DATA_DIR', str(directory)):
            result = self.call('grant_list', {})
            self.assertIsNone(result['authority_id']); self.assertIsNone(result['ledger_epoch'])
            self.assertFalse(directory.exists())
        directory.mkdir()
        with patch.object(config, 'DATA_DIR', str(directory)):
            with co.store(self.cfg): pass
            file = directory / 'collaboration.sqlite3'
            before = file.read_bytes()
            result = self.call('grant_list', {})
            self.assertEqual(result['items'], []); self.assertIsNone(result['authority_id'])
            self.assertEqual(file.read_bytes(), before)
            with co.store(self.cfg) as (con, _):
                con.execute('CREATE TABLE execution_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            with self.assertRaises(ex.ExecutionError) as caught: self.call('grant_list', {})
            self.assertEqual(caught.exception.code, 'storage_unavailable')

    def test_owner_grant_list_corrupt_metadata_is_not_exported_as_safe_text(self):
        grant = self.call('grant_create', {'role': 'source', 'subject': 'clean-origin'})
        for field, value in (('subject', grant['token']), ('subject', 'origin\x7flabel'), ('subject', 'origin\x85label'), ('subject', 'origin\nlabel'),
                             ('role', 'owner'), ('created_at', 'private-raw-text'), ('created_at', '2026-10-02 00:00:00+00:00'),
                             ('revoked_at', '2026-10-02T00:00:00')):
            with co.store(self.cfg) as (con, _):
                original = con.execute('SELECT ' + field + ' FROM execution_grants WHERE id=?', (grant['grant_id'],)).fetchone()[0]
                con.execute('UPDATE execution_grants SET ' + field + '=? WHERE id=?', (value, grant['grant_id']))
            with self.assertRaises(ex.ExecutionError) as caught: self.call('grant_list', {})
            self.assertEqual(caught.exception.code, 'storage_unavailable')
            self.assertNotIn(value, str(caught.exception))
            with co.store(self.cfg) as (con, _):
                con.execute('UPDATE execution_grants SET ' + field + '=? WHERE id=?', (original, grant['grant_id']))

    def test_grant_create_rejects_non_displayable_subject_before_inserting(self):
        with co.store(self.cfg) as (con, _):
            before = con.execute('SELECT COUNT(*) FROM execution_grants').fetchone()[0]
        for subject in ('origin\x7flabel', 'origin\x85label', 'origin\nlabel', 'origin\ud800label', 'x' * 43, 'f' * 64):
            with self.subTest(subject=ascii(subject)):
                with self.assertRaises(ex.ExecutionError) as caught:
                    self.call('grant_create', {'role': 'source', 'subject': subject})
                self.assertEqual(caught.exception.code, 'invalid_request')
        with co.store(self.cfg) as (con, _):
            self.assertEqual(con.execute('SELECT COUNT(*) FROM execution_grants').fetchone()[0], before)
        for subject in (str(uuid.uuid4()), '映序·持久身份'):
            grant = self.call('grant_create', {'role': 'source', 'subject': subject})
            self.assertEqual(next(row for row in self.call('grant_list', {})['items'] if row['grant_id'] == grant['grant_id'])['subject'], subject)


if __name__ == '__main__': unittest.main()
