"""Execution over real TCP, using only disposable stores and synthetic clients."""
import contextlib
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import urlencode

import server
from aihub import capabilities, collaboration, config, harnesses, service_control


class ExecutionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihub-execution-http-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / '合成工作区'
        self.root.mkdir()
        self.data = self.base / 'app' / 'data'
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True,
                    'server': {'port': 8765}}
        self.secrets = []
        self.drop_accept = False
        self.dropped_ids = []
        for target, name, value in (
                (config, 'DATA_DIR', str(self.data)), (server, 'CFG', self.cfg),
                (server, 'DB_OBJ', None),
                (service_control, 'GATE', service_control.ActivityGate())):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.start()
        self.addCleanup(self.stop)

    def start(self):
        fixture = self

        class QuietHandler(server.Handler):
            def log_message(self, *_args):
                pass

            def _send(self, status, headers, body):
                if (self.path == '/api/execution/accept' and status == 200
                        and fixture.drop_accept):
                    fixture.drop_accept = False
                    receipt = json.loads(body)
                    fixture.dropped_ids.append((receipt['execution_id'], receipt['queue_task_id']))
                    self.close_connection = True
                    return  # The durable acceptance committed; no HTTP reply reaches the client.
                super()._send(status, headers, body)

        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.port = self.http.server_address[1]
        self.cfg['server']['port'] = self.port
        self.control = service_control.ServiceControl(self.base / 'app', self.data, self.port)
        self.secrets.append(self.control.record['token'])
        self.http.service_control = self.control
        self.thread = threading.Thread(target=self.http.serve_forever,
                                       kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()

    def stop(self):
        if self.http is None:
            return
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.http = None

    def request(self, path, body=None, *, bearer=None, raw=None, header_pairs=()):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        encoded = (json.dumps(body, ensure_ascii=False).encode('utf-8')
                   if body is not None else None)
        if raw is not None:
            encoded = raw.encode('utf-8')
        try:
            conn.putrequest('POST' if encoded is not None else 'GET', path)
            conn.putheader('Content-Type', 'application/json')
            if encoded is not None:
                conn.putheader('Content-Length', str(len(encoded)))
            if bearer is not None:
                conn.putheader('Authorization', 'Bearer ' + bearer)
            for key, value in header_pairs:
                conn.putheader(key, value)
            conn.endheaders(encoded)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def success(self, path, body=None, **kwargs):
        code, result = self.request(path, body, **kwargs)
        # Never include a response containing a synthetic grant/lease in failure output.
        self.assertEqual(code, 200, result.get('code', 'unexpected HTTP status'))
        return result

    def execute(self, operation, body=None, bearer=None):
        payload = dict(body or {}, _workspace_root=str(self.root))
        if operation in {'grant_create', 'grant_revoke', 'grant_list'}:
            return self.success('/api/execution/' + operation, self.owner_body(payload), bearer=bearer,
                                header_pairs=self.owner_headers())
        return self.success('/api/execution/' + operation, payload, bearer=bearer)

    def owner_body(self, body):
        return dict(body, instance_id=self.control.record['instance_id'],
                    install_root=self.control.record['install_root'])

    def owner_headers(self):
        return [('X-AIHub-Control-Token', self.control.record['token'])]

    def grant(self, role, subject):
        result = self.execute('grant_create', {'role': role, 'subject': subject})
        self.assertTrue(result['secret_returned_once'])
        self.secrets.append(result['token'])
        return result

    def test_owner_grant_list_is_private_control_only_and_survives_restart(self):
        body = {'_workspace_root': str(self.root)}
        empty = self.execute('grant_list')
        self.assertEqual(empty['items'], [])
        self.assertIsNone(empty['authority_id'])
        self.assertFalse((self.data / 'collaboration.sqlite3').exists())
        grant = self.grant('source', 'owner-visible-origin')
        result = self.execute('grant_list', {'limit': 1})
        self.assertEqual(result['items'][0]['grant_id'], grant['grant_id'])
        serialized = json.dumps(result)
        for secret in (grant['token'], self.control.record['token'], str(self.root), 'token_hash'):
            self.assertNotIn(secret, serialized)
        denied = [({}, None, ()),
                  (self.owner_body(body), grant['token'], self.owner_headers()),
                  (self.owner_body(body), None, self.owner_headers() * 2),
                  (self.owner_body(body), None, self.owner_headers() + [('Origin', 'http://127.0.0.1:%s' % self.port)]),
                  ({**self.owner_body(body), 'instance_id': 'other-instance'}, None, self.owner_headers())]
        for payload, bearer, headers in denied:
            code, response = self.request('/api/execution/grant_list', payload, bearer=bearer, header_pairs=headers)
            self.assertEqual(code, 403)
            self.assertNotIn(grant['token'], json.dumps(response))
        self.execute('grant_revoke', {'grant_id': grant['grant_id']})
        self.stop(); self.start()
        recovered = self.execute('grant_list')
        self.assertEqual(recovered['authority_id'], result['authority_id'])
        self.assertEqual(recovered['ledger_epoch'], result['ledger_epoch'])
        self.assertEqual(recovered['items'][0]['grant_id'], grant['grant_id'])
        self.assertTrue(recovered['items'][0]['revoked_at'])

    def descriptor(self):
        return self.success('/api/execution/describe')

    def seed(self):
        harnesses.save(self.cfg, {'id': 'codex', 'revision': 0, 'connection_mode': 'mcp_stdio'})
        for client in ('worker-one', 'worker-two'):
            self.success('/api/collaboration/mcp/client_heartbeat', {
                '_workspace_root': str(self.root), 'client_id': client, 'tool': 'codex',
                'name': '合成客户端', 'protocol_version': 1})
        capabilities.publish(self.cfg, {'client_id': 'worker-one', 'capabilities_json': json.dumps([{
            'key': 'synthetic.document', 'name': '合成文档', 'kind': 'mcp_tool',
            'domains': ['document'], 'inputs': {'type': 'object', 'properties': {
                'prompt': {'type': 'string', 'maxLength': 200}}, 'required': ['prompt']},
            'constraints': ['仅合成夹具，不执行外部模型']}], ensure_ascii=False)})
        self.capability_id = capabilities.catalog_readonly(self.cfg)['items'][0]['id']
        self.source = self.grant('source', 'source-one')
        self.worker = self.grant('worker', 'worker-one')

    def intent(self):
        desc = self.descriptor()
        snapshot = self.success('/api/interop/capability-snapshot?' + urlencode({
            '_workspace_root': str(self.root), 'capability_id': self.capability_id,
            'connection_revision': desc['connection_revision']}))
        inputs = '{"prompt":"合成输入"}'
        return {'_workspace_root': str(self.root), 'protocol': 'aihub-execution/1',
                'request_id': str(uuid.uuid4()), 'origin': {
                    'authority_id': 'source-one', 'project_id': 'source-project',
                    'task_id': 'source-task', 'run_id': 'source-run', 'call_id': 'source-call',
                    'input_revision': 'revision-one'},
                'workspace_binding_revision': desc['workspace']['binding_revision'],
                'execution_authority_id': desc['execution_authority_id'],
                'ledger_epoch': desc['ledger_epoch'], 'connection_revision': desc['connection_revision'],
                'capability_id': self.capability_id,
                'expected_declaration_sha256': snapshot['declaration']['sha256'],
                'input_json': inputs, 'input_sha256': hashlib.sha256(inputs.encode('utf-8')).hexdigest(),
                'hub_project': '合成项目', 'title': '真实 TCP 合成执行'}

    def accept(self, intent=None):
        return self.success('/api/execution/accept', intent or self.intent(), bearer=self.source['token'])

    def claim(self, receipt):
        result = self.execute('claim', {'execution_id': receipt['execution_id'],
                                       'claim_request_id': str(uuid.uuid4())}, self.worker['token'])
        self.secrets.append(result['lease_token'])
        return result

    def assert_public(self, result):
        text = json.dumps(result, ensure_ascii=False)
        self.assertFalse(any(secret in text for secret in self.secrets), 'public response leaked a secret')

    def counts(self):
        with contextlib.closing(sqlite3.connect(self.data / 'collaboration.sqlite3')) as con:
            return tuple(con.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                         for name in ('executions', 'tasks'))

    def denied(self, operation, payload, bearer=None, expected=403):
        code, result = self.request('/api/execution/' + operation,
                                    dict(payload, _workspace_root=str(self.root)), bearer=bearer)
        self.assertEqual(code, expected, result.get('code', 'unexpected HTTP status'))
        self.assert_public(result)
        return result

    def test_describe_is_readonly_and_handler_supplies_public_identity(self):
        with patch.object(collaboration, 'store', side_effect=AssertionError('must stay readonly')):
            desc = self.descriptor()
        self.assertEqual(desc['protocol'], 'aihub-execution/1')
        self.assertEqual(desc['identity']['service_instance_id'], self.control.record['instance_id'])
        self.assertEqual(desc['identity']['port'], self.port)
        self.assertIsNone(desc['execution_authority_id'])
        self.assertIsNone(desc['ledger_epoch'])
        self.assertFalse(self.data.exists())
        self.assert_public(desc)
        for forged in ('public_identity', 'execution_bearer', 'bearer', 'token'):
            code, result = self.request('/api/execution/describe', {forged: 'synthetic-forgery'})
            self.assertEqual(code, 400)
            self.assert_public(result)
        del self.http.service_control
        unknown = self.descriptor()
        self.assertEqual(unknown['identity']['status'], 'identity_unavailable')
        self.assertIsNone(unknown['connection_revision'])

    def test_dropped_accept_reply_retries_original_request_without_duplicate_work(self):
        self.seed()
        intent = self.intent()
        self.drop_accept = True
        with self.assertRaises(http.client.RemoteDisconnected):
            self.request('/api/execution/accept', intent, bearer=self.source['token'])
        recovered = self.execute('status', {'request_id': intent['request_id']}, self.source['token'])
        replay = self.accept(intent)
        self.assertEqual((replay['execution_id'], replay['queue_task_id']), self.dropped_ids[0])
        self.assertEqual(recovered, replay)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(replay['dispatch_state'], 'queued_ready')
        self.assertEqual(replay['provider_state'], 'not_started')
        self.assertFalse(replay['native_execution_verified_by_hub'])
        self.assert_public(replay)
        work = self.root / '40_Projects' / '合成项目' / 'Work' / 'AIHub'
        self.assertEqual(len(list(work.iterdir())), 1)
        self.assertTrue((work / replay['queue_task_id'] / 'TASK_BRIEF.md').is_file())
        changed = dict(intent, title='同编号不同内容')
        self.assertEqual(self.denied('accept', changed, self.source['token'], 409)['code'], 'idempotency_conflict')
        self.assertEqual(self.counts(), (1, 1))

    def test_scoped_inbox_is_bounded_readonly_over_real_tcp_and_restart(self):
        self.seed()
        receipts = [self.accept() for _ in range(2)]
        page = self.execute('inbox', {'limit': 1}, self.worker['token'])
        self.assertEqual(len(page['items']), 1)
        self.assertTrue(page['has_more'])
        self.assertFalse(page['claim_performed'])
        self.assert_public(page)
        self.assertNotIn('lease_token', json.dumps(page))
        self.denied('inbox', {}, self.source['token'])
        other = self.grant('worker', 'worker-two')
        self.assertEqual(self.execute('inbox', {}, other['token'])['items'], [])
        self.denied('inbox', {'after_execution_id': receipts[0]['execution_id']}, other['token'])
        self.denied('inbox', {'limit': 26}, self.worker['token'], 400)
        self.stop()
        self.start()
        rest = self.execute('inbox', {'limit': 1, 'after_execution_id': page['next_after_execution_id']}, self.worker['token'])
        identifiers = [page['items'][0]['execution_id'], rest['items'][0]['execution_id']]
        self.assertEqual(set(identifiers), {item['execution_id'] for item in receipts})
        self.assertEqual(self.counts(), (2, 2))

    def test_restart_recovers_original_key_but_rejects_new_request_on_stale_connection(self):
        self.seed()
        intent = self.intent()
        self.drop_accept = True
        with self.assertRaises(http.client.RemoteDisconnected):
            self.request('/api/execution/accept', intent, bearer=self.source['token'])
        old_desc = self.descriptor()
        self.stop()
        self.start()  # A new real TCP server and public instance, with the same disposable ledger.
        desc = self.descriptor()
        self.assertNotEqual(old_desc['identity']['service_instance_id'], desc['identity']['service_instance_id'])
        self.assertNotEqual(old_desc['connection_revision'], desc['connection_revision'])
        self.assertEqual(old_desc['execution_authority_id'], desc['execution_authority_id'])
        self.assertEqual(old_desc['ledger_epoch'], desc['ledger_epoch'])
        accepted = self.accept(intent)
        self.assertEqual((accepted['execution_id'], accepted['queue_task_id']), self.dropped_ids[0])
        recovered = self.execute('status', {'request_id': intent['request_id']}, self.source['token'])
        self.assertEqual(recovered, accepted)
        stale_new = dict(intent, request_id=str(uuid.uuid4()))
        self.assertEqual(self.denied('accept', stale_new, self.source['token'], 409)['code'], 'connection_changed')
        self.assertEqual(self.counts(), (1, 1))
        fresh = self.accept(self.intent())
        self.assertNotEqual(fresh['execution_id'], accepted['execution_id'])
        self.assertEqual(self.counts(), (2, 2))

    def test_same_tool_different_client_and_legacy_claim_cannot_take_execution(self):
        self.seed()
        receipt = self.accept()
        wrong = self.grant('worker', 'worker-two')
        self.assertEqual(self.denied('claim', {'execution_id': receipt['execution_id'],
                         'claim_request_id': str(uuid.uuid4())}, wrong['token'])['code'], 'scope_denied')
        for client in ('worker-one', 'worker-two'):
            for prefix in ('', 'mcp/'):
                code, result = self.request('/api/collaboration/' + prefix + 'task_claim', {
                    '_workspace_root': str(self.root), 'task_id': receipt['queue_task_id'], 'client_id': client})
                self.assertEqual(code, 400)
                self.assert_public(result)
        current = self.execute('status', {'execution_id': receipt['execution_id']}, self.source['token'])
        self.assertEqual(current['dispatch_state'], 'queued_ready')
        self.assertEqual(self.claim(receipt)['executor']['client_id'], 'worker-one')

    def test_source_and_worker_credentials_enforce_role_and_subject_scope(self):
        self.seed()
        receipt = self.accept()
        other = self.grant('source', 'source-two')
        readonly = self.grant('source_read', 'source-one')
        worker_two = self.grant('worker', 'worker-two')
        identifier = {'execution_id': receipt['execution_id']}
        self.assertEqual(self.execute('status', identifier, readonly['token']), receipt)
        for grant in (other, worker_two):
            self.assertEqual(self.denied('status', identifier, grant['token'])['code'], 'scope_denied')
            self.denied('cancel', identifier, grant['token'])
        for grant in (self.source, readonly):
            self.denied('claim', dict(identifier, claim_request_id=str(uuid.uuid4())), grant['token'])
        for grant in (readonly, self.worker):
            self.denied('accept', self.intent(), grant['token'])
        forged_origin = self.intent()
        forged_origin['origin']['authority_id'] = 'source-two'
        self.assertEqual(self.denied('accept', forged_origin, self.source['token'])['code'], 'scope_denied')
        self.assertEqual(self.counts(), (1, 1))

    def test_missing_header_body_identity_and_credentials_injection_are_rejected(self):
        self.seed()
        intent = self.intent()
        self.denied('accept', intent)
        for key in ('bearer', 'execution_bearer', 'token', 'authorization',
                    'public_identity', 'client_id', 'target_client_id'):
            with self.subTest(field=key):
                forged = dict(intent, **{key: self.source['token'] if key == 'token' else 'synthetic-forgery'})
                self.denied('accept', forged, self.source['token'], 400)
        # Body credentials cannot substitute for the transport-owned header.
        self.denied('accept', dict(intent, execution_bearer=self.source['token']), expected=400)
        forged_input = dict(intent, input_json='{"prompt":"合成","token":"synthetic-credential"}')
        forged_input['input_sha256'] = hashlib.sha256(forged_input['input_json'].encode()).hexdigest()
        self.denied('accept', forged_input, self.source['token'], 400)
        self.assertEqual(self.counts(), (0, 0))

    def test_duplicate_json_fields_are_rejected_before_acceptance(self):
        self.seed()
        intent = self.intent()
        encoded = json.dumps(intent, ensure_ascii=False)
        variants = [encoded[:-1] + ',"request_id":' + json.dumps(intent['request_id']) + '}',
                    encoded[:-1] + ',"_workspace_root":' + json.dumps(str(self.root)) + '}',
                    encoded.replace('"authority_id": "source-one"',
                                    '"authority_id": "source-one", "authority_id": "source-two"')]
        for raw in variants:
            code, result = self.request('/api/execution/accept', raw=raw, bearer=self.source['token'])
            self.assertEqual(code, 400)
            self.assert_public(result)
        self.assertEqual(self.counts(), (0, 0))

    def test_duplicate_authorization_headers_are_rejected(self):
        self.seed()
        code, result = self.request('/api/execution/accept', self.intent(), bearer=self.source['token'],
                                    header_pairs=[('Authorization', 'Bearer ' + self.worker['token'])])
        self.assertIn(code, (400, 403))
        self.assert_public(result)
        self.assertEqual(self.counts(), (0, 0))

    def test_post_query_identity_or_credentials_are_not_ignored(self):
        self.seed()
        for query in ('public_identity=forged', 'bearer=forged', 'request_id=one&request_id=two'):
            code, result = self.request('/api/execution/accept?' + query, self.intent(), bearer=self.source['token'])
            self.assertEqual(code, 400)
            self.assert_public(result)
        self.assertEqual(self.counts(), (0, 0))

    def test_grant_registration_revocation_and_hashed_storage(self):
        self.seed()
        code, result = self.request('/api/execution/grant_create', self.owner_body({
            '_workspace_root': str(self.root), 'role': 'worker', 'subject': 'unregistered-client'}),
            header_pairs=self.owner_headers())
        self.assertEqual(code, 400)
        self.assert_public(result)
        for grant in (self.source, self.worker):
            self.denied('grant_create', {'role': 'source', 'subject': 'attempted-escalation'}, grant['token'])
            self.denied('grant_revoke', {'grant_id': self.source['grant_id']}, grant['token'])
        for header in ('Bearer malformed', 'Basic synthetic-denied', ''):
            code, result = self.request('/api/execution/grant_create', {
                '_workspace_root': str(self.root), 'role': 'source', 'subject': 'attempted-escalation'},
                header_pairs=[('Authorization', header)])
            self.assertEqual(code, 403)
            self.assert_public(result)
        receipt = self.accept()
        revoked = self.execute('grant_revoke', {'grant_id': self.source['grant_id']})
        self.assertTrue(revoked['revoked'])
        self.assertFalse(revoked['native_work_stopped'])
        self.denied('status', {'execution_id': receipt['execution_id']}, self.source['token'])
        with contextlib.closing(sqlite3.connect(self.data / 'collaboration.sqlite3')) as con:
            hashes = [row[0] for row in con.execute('SELECT token_hash FROM execution_grants')]
        self.assertEqual(len(hashes), 2)
        self.assertTrue(hashlib.sha256(self.source['token'].encode()).hexdigest() in hashes)
        self.assertFalse(any(secret in hashes for secret in self.secrets), 'grant stored plaintext')

    def test_grant_management_requires_trusted_native_owner_and_rejects_forgery(self):
        self.seed()
        operations = [('grant_create', {'role': 'source', 'subject': 'attempted-escalation'}),
                      ('grant_revoke', {'grant_id': self.source['grant_id']})]
        for operation, fields in operations:
            payload = dict(fields, _workspace_root=str(self.root))
            transport = self.owner_body(payload)
            cases = [
                (payload, [], None),
                (transport, [], None),
                (transport, [('X-AIHub-Control-Token', 'synthetic-invalid-control')], None),
                (dict(transport, instance_id='synthetic-wrong-instance'), self.owner_headers(), None),
                (dict(transport, install_root=str(self.base / 'other-install')), self.owner_headers(), None),
                (transport, self.owner_headers() * 2, None),
                (transport, self.owner_headers() + [('X-AIHub-Control-Token', 'synthetic-invalid-control')], None),
                (transport, self.owner_headers() + [('Origin', 'http://127.0.0.1:' + str(self.port))], None),
                (transport, self.owner_headers() + [('Sec-Fetch-Site', 'same-origin')], None),
                (transport, self.owner_headers(), self.source['token']),
                (transport, self.owner_headers(), self.worker['token']),
            ]
            for name in ('execution_owner', 'owner_authorized', 'public_identity'):
                cases.append((dict(payload, **{name: True}), [], None))
            for index, (body, headers, bearer) in enumerate(cases):
                with self.subTest(operation=operation, case=index):
                    code, result = self.request('/api/execution/' + operation, body,
                                                header_pairs=headers, bearer=bearer)
                    self.assertEqual(code, 403)
                    self.assert_public(result)
            # A genuine native owner still cannot provide unknown trusted-context flags.
            for name in ('execution_owner', 'owner_authorized'):
                code, result = self.request('/api/execution/' + operation, dict(transport, **{name: True}),
                                            header_pairs=self.owner_headers())
                self.assertEqual(code, 400)
                self.assert_public(result)
        with contextlib.closing(sqlite3.connect(self.data / 'collaboration.sqlite3')) as con:
            grants = list(con.execute('SELECT revoked_at FROM execution_grants'))
        self.assertEqual(len(grants), 2)
        self.assertTrue(all(row[0] is None for row in grants))
        receipt = self.accept()
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.execute('status', {'execution_id': receipt['execution_id']}, self.source['token']), receipt)

    def test_worker_observations_require_both_selected_client_grant_and_current_lease(self):
        self.seed()
        receipt = self.accept()
        claim = self.claim(receipt)
        other = self.grant('worker', 'worker-two')
        observation = {'execution_id': receipt['execution_id'], 'lease_token': claim['lease_token'],
                       'observation_id': str(uuid.uuid4()), 'provider_state': 'submitting',
                       'provider_request_id': 'adapter-request-one', 'results': []}
        self.assertEqual(self.denied('observe', observation, other['token'])['code'], 'scope_denied')
        self.denied('observe', observation, self.source['token'])
        self.denied('observe', dict(observation, lease_token='synthetic-invalid-lease'), self.worker['token'], 400)
        current = self.execute('status', {'execution_id': receipt['execution_id']}, self.source['token'])
        self.assertEqual(current['provider_state'], 'not_started')
        self.assertEqual(self.execute('observe', observation, self.worker['token'])['provider_state'], 'submitting')

    def test_credentials_cannot_cross_workspace_binding(self):
        self.seed()
        receipt = self.accept()
        before = self.counts()
        original = self.root
        other = self.base / '其他合成工作区'
        other.mkdir()
        try:
            self.root = other
            self.cfg['ai_root'] = str(other)
            self.denied('status', {'execution_id': receipt['execution_id']}, self.source['token'])
            self.denied('claim', {'execution_id': receipt['execution_id'],
                         'claim_request_id': str(uuid.uuid4())}, self.worker['token'])
            self.assertEqual(self.counts(), before)
        finally:
            self.root = original
            self.cfg['ai_root'] = str(original)
        self.assertEqual(self.execute('status', {'execution_id': receipt['execution_id']}, self.source['token']), receipt)

    def test_cancel_before_claim_and_running_cancel_preserve_execution_identity(self):
        self.seed()
        first = self.accept()
        cancelled = self.execute('cancel', {'execution_id': first['execution_id']}, self.source['token'])
        self.assertEqual(cancelled['provider_state'], 'cancelled')
        self.assertEqual(cancelled['dispatch_state'], 'cancelled_before_claim')
        self.assertEqual(cancelled['evidence_source'], 'queue_ledger')
        self.denied('claim', {'execution_id': first['execution_id'], 'claim_request_id': str(uuid.uuid4())},
                    self.worker['token'], 409)
        second = self.accept()
        claim = self.claim(second)
        self.execute('observe', {'execution_id': second['execution_id'], 'lease_token': claim['lease_token'],
                     'observation_id': str(uuid.uuid4()), 'provider_state': 'submitting',
                     'provider_request_id': 'adapter-request-one', 'results': []}, self.worker['token'])
        requested = self.execute('cancel', {'execution_id': second['execution_id']}, self.source['token'])
        self.assertTrue(requested['cancel_requested'])
        self.assertEqual(requested['provider_state'], 'submitting')
        self.assertFalse(requested['native_cancel_by_hub'])
        self.assert_public(requested)

    def test_cancel_and_failure_outcomes_survive_observation_replay_and_server_restart(self):
        self.seed()
        saved = []
        outcomes = [
            ('cancelled', {'cancel_evidence': {'kind': 'native_terminal', 'reference': 'adapter-terminal-one'}}, True),
            ('cancelled', {'cancel_evidence': {'kind': 'never_submitted', 'reference': 'adapter-not-submitted'}}, False),
            ('failed', {'error_code': 'adapter_generation_failed'}, True),
        ]
        for index, (state, outcome, run_first) in enumerate(outcomes):
            with self.subTest(state=state, index=index):
                receipt = self.accept()
                claim = self.claim(receipt)
                observation = {'execution_id': receipt['execution_id'], 'lease_token': claim['lease_token'],
                               'observation_id': str(uuid.uuid4()), 'provider_state': 'submitting',
                               'provider_request_id': 'adapter-outcome-' + str(index), 'results': []}
                self.execute('observe', observation, self.worker['token'])
                if run_first:
                    observation.update(observation_id=str(uuid.uuid4()), provider_state='running')
                    self.execute('observe', observation, self.worker['token'])
                observation.update(observation_id=str(uuid.uuid4()), provider_state=state, **outcome)
                result = self.execute('observe', observation, self.worker['token'])
                self.assertEqual(result['outcome'], outcome)
                self.assertEqual(self.execute('observe', observation, self.worker['token']), result)
                current = self.execute('status', {'execution_id': receipt['execution_id']}, self.source['token'])
                self.assertEqual(current['outcome'], outcome)
                self.assert_public(current)
                saved.append((receipt['execution_id'], state, outcome))
        self.stop()
        self.start()
        for identifier, state, outcome in saved:
            recovered = self.execute('status', {'execution_id': identifier}, self.source['token'])
            self.assertEqual(recovered['provider_state'], state)
            self.assertEqual(recovered['outcome'], outcome)
            self.assert_public(recovered)

    def test_full_worker_observations_report_empty_memory_finish_and_source_status(self):
        self.seed()
        receipt = self.accept()
        claim = self.claim(receipt)
        owner = {'_workspace_root': str(self.root), 'task_id': receipt['queue_task_id'],
                 'client_id': 'worker-one', 'lease_token': claim['lease_token']}
        code, _ = self.request('/api/collaboration/mcp/task_finish', dict(owner, summary='过早完成'))
        self.assertEqual(code, 400)
        for state in ('submitting', 'running', 'succeeded'):
            results = [] if state != 'succeeded' else [{
                'result_id': 'adapter-output-one', 'kind': 'document', 'media_type': 'text/plain',
                'bytes': 12, 'locator': 'adapter/results/output-one', 'sha256': 'a' * 64}]
            observation = {'execution_id': receipt['execution_id'], 'lease_token': claim['lease_token'],
                           'observation_id': str(uuid.uuid4()), 'provider_state': state,
                           'provider_request_id': 'adapter-request-one', 'results': results}
            observed = self.execute('observe', observation, self.worker['token'])
            self.assertEqual(observed['provider_state'], state)
            self.assertEqual(self.execute('observe', observation, self.worker['token']), observed)
            self.assert_public(observed)
        code, _ = self.request('/api/collaboration/mcp/task_finish', dict(owner, summary='遗漏报告'))
        self.assertEqual(code, 400)
        report = self.success('/api/collaboration/mcp/report_submit', dict(
            owner, submission_id=str(uuid.uuid4()), category='report', title='合成验收报告',
            filename='验收.md', content='# 合成结果\n仅声明适配器观察，不声称 Hub 验证原生执行。',
            memory_candidates=[]))
        self.assertTrue(Path(report['path']).is_file())
        self.assertEqual(report['memory_candidate_count'], 0)
        self.assertEqual(report['memory_candidate_ids'], [])
        finished = self.success('/api/collaboration/mcp/task_finish', dict(owner, summary='合成执行闭环'))
        self.assertEqual(finished['status'], 'completed')
        self.assertEqual(finished['report_submission']['status'], 'submitted')
        final = self.execute('status', {'execution_id': receipt['execution_id']}, self.source['token'])
        self.assertEqual(final['provider_state'], 'succeeded')
        self.assertEqual(final['dispatch_state'], 'completed')
        self.assertEqual(final['provider_request_id'], 'adapter-request-one')
        self.assertEqual(final['queue_task_id'], receipt['queue_task_id'])
        self.assertEqual(final['origin'], self.intent()['origin'])
        self.assertEqual(final['evidence_source'], 'worker_report')
        self.assertFalse(final['native_execution_verified_by_hub'])
        self.assertFalse(final['native_cancel_by_hub'])
        self.assert_public(final)
        with contextlib.closing(sqlite3.connect(self.data / 'collaboration.sqlite3')) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM memories').fetchone()[0], 0)
        self.assertEqual(self.counts(), (1, 1))


if __name__ == '__main__':
    unittest.main()
