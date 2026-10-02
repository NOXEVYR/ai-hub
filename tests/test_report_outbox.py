"""Synthetic HTTP delivery loss, private spool boundaries and restart recovery."""
import json
import base64
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from aihub import collaboration, collaboration_api
from tests import test_workcenter as fixtures
from tools.aihub_mcp import Bridge, BridgeError, BY_NAME, validate
from tools.report_outbox import ReportOutbox, OutboxError, MAX_ATTEMPTS, MAX_FILE, list_current


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.WorkcenterTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.cfg = fixture.cfg
        self.temp = tempfile.TemporaryDirectory(prefix='report-outbox-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.spool = self.base / 'AppData/AIHub/report-outbox'
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                action = self.path.rsplit('/', 1)[-1]
                owner.calls.append((action, body))
                if action == 'artifact_write' and owner.reject:
                    code, result = owner.reject, {'error': 'private server detail'}
                else:
                    try:
                        result = collaboration_api.execute(owner.cfg, action, body, 'mcp')
                        code = 200
                    except (ValueError, PermissionError):
                        code, result = 400, {'error': 'private server detail'}
                if action == 'artifact_write' and owner.drop and code == 200:
                    if owner.finish_after_drop:
                        collaboration.execute(owner.cfg, 'task_finish', dict(body, summary='synthetic completed after commit'))
                    owner.drop -= 1
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_response(code)
                self.end_headers()
                self.wfile.write(json.dumps(result).encode('utf-8'))

        self.calls, self.drop, self.reject, self.finish_after_drop = [], 0, 0, False
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.bridge = self.new_bridge()
        self.bridge.heartbeat()
        task = self.bridge.call_tool('task_create', {'project': 'Synthetic', 'title': 'Delivery'})
        claim = self.bridge.call_tool('task_claim', {'task_id': task['id']})
        self.report = {'task_id': task['id'], 'lease_token': claim['lease_token'], 'category': 'report',
                       'title': 'Synthetic report', 'filename': 'report.md', 'content': 'private fixture report body',
                       'memory_candidates': [{'title': 'Decision', 'content': 'synthetic durable decision', 'scope': 'project'}]}

    def stop(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)

    def new_bridge(self):
        return Bridge(self.http.server_port, 'outbox-client', 'codex', outbox_dir=self.spool)

    def test_real_lost_http_success_response_replays_exact_id_once(self):
        self.drop = 1
        receipt = self.bridge.call_tool('report_submit', self.report)
        self.assertEqual(receipt['status'], 'submitted')
        self.assertEqual(receipt['memory_candidate_count'], 1)
        writes = [body for action, body in self.calls if action == 'artifact_write']
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0], writes[1])
        uuid.UUID(writes[0]['submission_id'])
        self.assertEqual(receipt['delivery']['attempts'], 2)
        state = collaboration_api.status(self.cfg)
        self.assertEqual(len(state['artifacts']), 1)
        self.assertEqual(len(state['memories']), 1)
        self.assertEqual(state['tasks'][0]['report_submission']['submission_id'], receipt['submission_id'])
        raw = next(self.spool.glob('*/*.json')).read_text(encoding='utf-8')
        self.assertNotIn(self.report['lease_token'], raw)
        self.assertNotIn(self.report['content'], raw)
        self.assertNotIn('synthetic durable decision', raw)

    def test_pending_restart_without_lease_and_cli_explicit_retry(self):
        self.reject = 503
        result = self.bridge.call_tool('report_submit', self.report)
        self.assertEqual((result['status'], result['attempts'], result['error_code']), ('pending', 3, 'server_busy'))
        raw = next(self.spool.glob('*/*.json')).read_text(encoding='utf-8')
        self.assertIn(self.report['content'], raw)
        self.assertNotIn('lease_token', raw)
        self.assertNotIn(self.report['lease_token'], raw)
        self.assertNotIn('private server detail', raw)
        self.reject = 0
        environment = dict(os.environ)
        if os.name == 'nt':
            environment['APPDATA'] = str(self.base / 'AppData')
        else:
            environment['XDG_DATA_HOME'] = str(self.base / 'AppData')
        command = [sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'tools/aihub_mcp.py'),
                   '--port', str(self.http.server_port), '--client-id', 'outbox-client', '--tool', 'codex',
                   '--action', 'report_outbox_retry']
        before = len([a for a, _ in self.calls if a == 'artifact_write'])
        restarted = subprocess.run(command, input=json.dumps({'id': result['id']}), text=True,
                                   encoding='utf-8', capture_output=True, env=environment, timeout=15)
        self.assertEqual(restarted.returncode, 1)
        self.assertEqual(json.loads(restarted.stdout)['error_code'], 'needs_lease')
        self.assertEqual(before, len([a for a, _ in self.calls if a == 'artifact_write']))
        retry = subprocess.run(command, input=json.dumps({'id': result['id'], 'lease_token': self.report['lease_token']}),
                               text=True, encoding='utf-8', capture_output=True, env=environment, timeout=15)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout)['status'], 'submitted')
        self.assertNotIn(self.report['lease_token'], retry.stdout + retry.stderr)

    def test_retry_limit_is_persistent_and_no_secret_in_metadata(self):
        self.reject = 500
        result = self.bridge.call_tool('report_submit', self.report)
        for _ in range(3):
            result = self.bridge.call_tool('report_outbox_retry', {'id': result['id']})
        self.assertEqual((result['status'], result['attempts'], result['error_code']), ('failed', MAX_ATTEMPTS, 'retry_limit'))
        self.assertEqual(len([a for a, _ in self.calls if a == 'artifact_write']), MAX_ATTEMPTS)
        listed = list_current(str(self.cfg['ai_root']), self.spool)
        self.assertEqual(set(listed['items'][0]), {'id', 'task_id', 'title', 'client_id', 'tool', 'status', 'attempts', 'error_code'})
        self.assertNotIn(self.report['content'], str(listed))

    def test_workspace_switch_and_client_binding_cannot_redirect_pending_report(self):
        self.reject = 503
        result = self.bridge.call_tool('report_submit', self.report)
        self.assertEqual(list_current('different-workspace', self.spool)['items'], [])
        stranger = ReportOutbox(str(self.cfg['ai_root']), 'other-client', 'codex', self.spool)
        with self.assertRaises(FileNotFoundError):
            stranger.load(result['id'])
        before = len([a for a, _ in self.calls if a == 'artifact_write'])
        self.cfg = dict(self.cfg, ai_root=str(self.base / 'OtherWorkspace'))
        (self.base / 'OtherWorkspace').mkdir()
        response = self.bridge.call_tool('report_outbox_retry', {'id': result['id']})
        self.assertEqual(response['status'], 'failed')
        self.assertEqual(before, len([a for a, _ in self.calls if a == 'artifact_write']))

    def test_completed_and_missing_tasks_are_not_blindly_replayed(self):
        self.reject = 503
        result = self.bridge.call_tool('report_submit', self.report)
        owner = dict(self.report, client_id='outbox-client')
        collaboration.execute(self.cfg, 'artifact_write', dict(owner, kind='report', filename='other.md'))
        collaboration.execute(self.cfg, 'task_finish', dict(owner, summary='synthetic finish'))
        self.reject = 0
        before = len([a for a, _ in self.calls if a == 'artifact_write'])
        response = self.new_bridge().call_tool('report_outbox_retry', {'id': result['id'], 'lease_token': self.report['lease_token']})
        self.assertEqual(response['error_code'], 'task_not_active')
        self.assertEqual(before, len([a for a, _ in self.calls if a == 'artifact_write']))

    def test_offline_initial_binding_fails_without_staging_or_online_evidence(self):
        fresh = self.new_bridge()
        with patch.object(fresh, 'request', side_effect=BridgeError('offline', 'network_unavailable', True)):
            with self.assertRaises(BridgeError):
                fresh.call_tool('report_submit', self.report)
        self.assertIsNone(fresh.workspace_root)
        self.assertIsNone(fresh.last_heartbeat)
        self.assertFalse(self.spool.exists())

    def test_exact_task_lookup_finds_active_task_outside_newest_200(self):
        with collaboration.store(self.cfg) as (con, root):
            original = dict(con.execute('SELECT * FROM tasks WHERE id=?', (self.report['task_id'],)).fetchone())
            columns = list(original)
            for number in range(201):
                row = dict(original, id=str(uuid.uuid4()), created_at='2099-01-01T00:00:00+00:00',
                           updated_at='2099-01-01T00:00:00+00:00', status='queued', owner=None, lease_hash=None)
                con.execute('INSERT INTO tasks(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')',
                            [row[key] for key in columns])
        unfiltered = self.bridge.request('task_list', {})['items']
        self.assertEqual(len(unfiltered), 200)
        self.assertNotIn(self.report['task_id'], [row['id'] for row in unfiltered])
        receipt = self.bridge.call_tool('report_submit', self.report)
        self.assertEqual(receipt['status'], 'submitted')
        self.assertTrue(any(action == 'task_list' and body.get('task_id') == self.report['task_id'] for action, body in self.calls))

    def test_optional_and_legacy_lost_response_after_completion_recovers_by_receipt(self):
        for policy in ('optional', 'legacy'):
            with self.subTest(policy=policy):
                task = self.bridge.call_tool('task_create', {'project': 'Synthetic', 'title': policy, 'report_policy': 'optional'})
                if policy == 'legacy':
                    with collaboration.store(self.cfg) as (con, root):
                        con.execute('UPDATE task_report_contracts SET policy=? WHERE root=? AND task_id=?', ('legacy', root, task['id']))
                claim = self.bridge.call_tool('task_claim', {'task_id': task['id']})
                report = dict(self.report, task_id=task['id'], lease_token=claim['lease_token'])
                self.drop, self.finish_after_drop = 1, True
                first = self.bridge.call_tool('report_submit', report)
                self.assertNotEqual(first['status'], 'submitted')
                before = len([a for a, _ in self.calls if a == 'artifact_write'])
                confirmed = self.new_bridge().call_tool('report_outbox_retry', {'id': first['id']})
                self.assertEqual(confirmed['status'], 'submitted')
                self.assertEqual(confirmed['id'], confirmed['artifact_id'])
                self.assertEqual(before, len([a for a, _ in self.calls if a == 'artifact_write']))
                self.assertEqual(collaboration.execute(self.cfg, 'task_list', {'task_id': task['id']})['items'][0]['status'], 'completed')
                self.finish_after_drop = False

    def test_confirmed_report_changed_or_removed_is_not_reconfirmed_from_local_receipt(self):
        receipt = self.bridge.call_tool('report_submit', self.report)
        before = len([a for a, _ in self.calls if a == 'artifact_write'])
        Path(receipt['path']).write_text('changed fixture report', encoding='utf-8')
        changed = self.new_bridge().call_tool('report_outbox_retry', {'id': receipt['submission_id']})
        self.assertEqual((changed['status'], changed['error_code']), ('failed', 'submission_invalid'))
        with collaboration.store(self.cfg) as (con, root):
            con.execute('UPDATE artifacts SET deleted_at=? WHERE id=?', ('2099-01-01T00:00:00+00:00', receipt['id']))
        removed = self.new_bridge().call_tool('report_outbox_retry', {'id': receipt['submission_id']})
        self.assertEqual((removed['status'], removed['error_code']), ('failed', 'submission_invalid'))
        self.assertEqual(before, len([a for a, _ in self.calls if a == 'artifact_write']))

    def test_existing_server_uuid_with_empty_spool_rejects_different_body_even_completed(self):
        identifier = str(uuid.uuid4())
        original = self.bridge.request('artifact_write', dict(self.report, kind='report', submission_id=identifier))
        self.assertFalse(self.spool.exists())
        conflict = dict(self.report, submission_id=identifier, content='different fixture report B')
        rejected = self.bridge.call_tool('report_submit', conflict)
        self.assertEqual((rejected['status'], rejected['error_code'], rejected['attempts']), ('failed', 'submission_conflict', 0))
        self.assertEqual(self.bridge.report_queue().load(identifier)['payload']['content'], conflict['content'])
        self.assertEqual(Path(original['path']).read_text(encoding='utf-8'), self.report['content'])
        collaboration.execute(self.cfg, 'task_finish', dict(self.report, client_id='outbox-client', summary='completed synthetic A'))
        completed = self.new_bridge().call_tool('report_outbox_retry', {'id': identifier})
        self.assertEqual((completed['status'], completed['error_code']), ('failed', 'submission_conflict'))
        self.assertEqual(len([a for a, _ in self.calls if a == 'artifact_write']), 1)

    def test_output_uuid_cannot_confirm_report_with_identical_text(self):
        identifier = str(uuid.uuid4())
        output = {key: value for key, value in self.report.items() if key != 'memory_candidates'}
        self.bridge.request('artifact_write', dict(output, kind='output', submission_id=identifier))
        rejected = self.bridge.call_tool('report_submit', dict(self.report, submission_id=identifier))
        self.assertEqual((rejected['status'], rejected['error_code']), ('failed', 'submission_conflict'))
        self.assertEqual(self.bridge.report_queue().load(identifier)['payload']['content'], self.report['content'])
        self.assertEqual(len([a for a, _ in self.calls if a == 'artifact_write']), 1)

    def test_compact_local_receipt_does_not_discard_conflicting_new_body(self):
        original = self.bridge.call_tool('report_submit', self.report)
        conflicting = dict(self.report, submission_id=original['submission_id'], content='conflicting fixture B')
        result = self.bridge.call_tool('report_submit', conflicting)
        self.assertEqual((result['status'], result['error_code']), ('failed', 'submission_conflict'))
        self.assertEqual(self.bridge.report_queue().load(original['submission_id'])['payload']['content'], conflicting['content'])
        self.assertEqual(Path(original['path']).read_text(encoding='utf-8'), self.report['content'])

    def test_evicted_compact_receipt_recovers_only_identical_request_fingerprint(self):
        original = self.bridge.call_tool('report_submit', self.report)
        queue = self.bridge.report_queue()
        for number in range(65):
            entry = queue.stage(dict(self.report, filename='receipt-fixture-%s.md' % number))
            entry.update(status='submitted', artifact_id=str(uuid.uuid4()))
            queue.save(entry)
        with self.assertRaises(FileNotFoundError):
            queue.load(original['submission_id'])
        before = len([a for a, _ in self.calls if a == 'artifact_write'])
        recovered = self.bridge.call_tool('report_submit', dict(self.report, submission_id=original['submission_id']))
        self.assertEqual(recovered['status'], 'submitted')
        self.assertEqual(recovered['id'], original['id'])
        self.assertEqual(recovered['payload_hash'], queue.load(original['submission_id'])['payload_hash'])
        self.assertEqual(recovered['submission_action'], 'artifact_write')
        self.assertEqual(recovered['kind'], 'report')
        self.assertEqual(before, len([a for a, _ in self.calls if a == 'artifact_write']))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='outbox-storage-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.queue = ReportOutbox('workspace', 'client', 'codex', self.base / 'spool')
        self.payload = {'task_id': 'task', 'category': 'report', 'title': 'Report', 'filename': 'report.md',
                        'content': 'body', 'memory_candidates': [], 'lease_token': 'NEVER-PERSIST'}

    def test_same_submission_id_reuses_spool_and_rejects_changed_payload(self):
        first = self.queue.stage(self.payload)
        same = self.queue.stage(dict(self.payload, submission_id=first['id'], lease_token='different'))
        self.assertEqual(first['id'], same['id'])
        with self.assertRaises(OutboxError):
            self.queue.stage(dict(self.payload, submission_id=first['id'], content='changed'))
        self.assertNotIn('NEVER-PERSIST', str(self.queue.load(first['id'])))
        with self.assertRaises(OutboxError):
            self.queue.load('../escape')

    def test_size_cap_unknown_secret_fields_and_concurrent_lock(self):
        with self.assertRaises(OutboxError):
            self.queue.stage(dict(self.payload, content='x' * (MAX_FILE + 1)))
        with self.assertRaises(OutboxError):
            self.queue.stage(dict(self.payload, api_key='do-not-save'))
        with self.queue.locked(), self.assertRaises(OSError):
            with self.queue.locked():
                self.fail('Concurrent delivery lock was not enforced')

    def test_symlink_and_hardlink_rejected(self):
        entry = self.queue.stage(self.payload)
        path = self.queue.path / (entry['id'] + '.json')
        os.link(path, self.base / 'external-hardlink.json')
        with self.assertRaises(OutboxError):
            self.queue.load(entry['id'])
        self.assertTrue(list_current('workspace', self.base / 'spool')['partial'])
        try:
            (self.base / 'linked').symlink_to(self.queue.path, target_is_directory=True)
        except OSError:
            return  # Windows symlink privilege is optional; hardlink case still ran.
        with self.assertRaises(OutboxError):
            ReportOutbox('workspace', 'client', 'codex', self.base / 'linked')

    def test_schema_requires_explicit_memory_decision_and_bounded_uuid(self):
        schema = BY_NAME['aihub_report_submit']['inputSchema']
        validate(schema, self.payload)
        validate(schema, dict(self.payload, submission_id=str(uuid.uuid4())))
        for fields in ({'submission_id': '../escape'}, {'submission_id': 'x' * 37}):
            with self.assertRaises(Exception):
                validate(schema, dict(self.payload, **fields))

    def test_success_receipts_are_bounded_without_blocking_or_evicting_failed_reports(self):
        pending = self.queue.stage(dict(self.payload, filename='pending.md'))
        for number in range(70):
            entry = self.queue.stage(dict(self.payload, filename='success-%s.md' % number))
            entry.update(status='submitted', artifact_id=str(uuid.uuid4()))
            self.queue.save(entry)
        rows = self.queue.list()
        self.assertEqual(len([row for row in rows if row['status'] == 'submitted']), 64)
        self.assertEqual(self.queue.load(pending['id'])['payload']['content'], 'body')
        self.assertEqual(self.queue.stage(dict(self.payload, filename='next.md'))['status'], 'pending')

    def test_metadata_scan_does_not_read_large_report_bodies(self):
        self.queue.stage(dict(self.payload, content='z' * 1048576))
        with patch('tools.report_outbox._read', side_effect=AssertionError('Body read')):
            result = list_current('workspace', self.base / 'spool')
        self.assertEqual(len(result['items']), 1)
        self.assertFalse(result['partial'])

    def test_private_permissions_and_partial_metadata_preserve_current_items(self):
        self.queue.stage(self.payload)
        other = self.base / 'spool' / ('0' * 64)
        other.mkdir()
        (other / (str(uuid.uuid4()) + '.json')).write_text('invalid fixture')
        result = list_current('workspace', self.base / 'spool')
        self.assertEqual(len(result['items']), 1)
        self.assertTrue(result['partial'])
        self.assertIsNone(result['error_code'])
        if os.name == 'nt':
            script = ("$p='" + str(self.queue.path).replace("'", "''") + "'; "
                      "$a=[System.IO.Directory]::GetAccessControl($p); "
                      "$s=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
                      "$r=@($a.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier])); "
                      "[bool]($a.AreAccessRulesProtected -and $r.Count -eq 2 -and "
                      "@($r | Where-Object {$_.IdentityReference.Value -ne $s -and $_.IdentityReference.Value -ne 'S-1-5-18'}).Count -eq 0)")
            check = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                                    base64.b64encode(script.encode('utf-16-le')).decode('ascii')], capture_output=True, timeout=10)
            self.assertEqual(check.returncode, 0)
            self.assertEqual(check.stdout.strip(), b'True')
        else:
            self.assertEqual(self.queue.path.stat().st_mode & 0o777, 0o700)

    def test_pending_capacity_preserves_all_reports_and_metadata_return_budget(self):
        for number in range(32):
            self.queue.stage(dict(self.payload, filename='pending-%s.md' % number))
        with self.assertRaises(OutboxError):
            self.queue.stage(dict(self.payload, filename='overflow.md'))
        self.assertEqual(len(self.queue.list()), 32)
        for number in range(201):
            client = 'synthetic-%s' % number
            binding = hashlib.sha256(json.dumps(['workspace', client, 'codex'], ensure_ascii=False).encode()).hexdigest()
            directory = self.base / 'spool' / binding
            directory.mkdir()
            identifier = str(uuid.uuid4())
            header = {'id': identifier, 'workspace_root': 'workspace', 'client_id': client, 'tool': 'codex',
                      'task_id': 'fixture', 'title': 'Synthetic', 'status': 'pending', 'attempts': 0, 'error_code': None}
            (directory / (identifier + '.json')).write_text(json.dumps(header), encoding='utf-8')
        result = list_current('workspace', self.base / 'spool')
        self.assertLessEqual(len(result['items']), 200)
        self.assertTrue(result['truncated'])


if __name__ == '__main__':
    unittest.main()
