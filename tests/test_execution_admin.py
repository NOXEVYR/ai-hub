"""Native owner CLI on disposable HTTP instances; no installed service or keys."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

import server
from aihub import service_control
import test_execution_http as http_fixture
from tools import execution_admin as admin


class ExecutionAdminTests(unittest.TestCase):
    def setUp(self):
        self.fixture = http_fixture.ExecutionHTTPTests('test_describe_is_readonly_and_handler_supplies_public_identity')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.seed()
        self.fixture.control.publish()  # Only this newly-created synthetic installation.
        self.output = self.fixture.base / '私有 接入.json'
        self.install = self.fixture.base / 'app'

    def args(self, **overrides):
        fields = {'install-root': str(self.install), 'workspace-root': str(self.fixture.root),
                  'role': 'source', 'subject': 'source-one', 'output': str(self.output)}
        fields.update(overrides)
        return [part for key, value in fields.items() if value is not None for part in ('--' + key, value)]

    def invoke(self, args=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = admin.main(args if args is not None else self.args())
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_safe(self, stdout, stderr):
        text = stdout + stderr
        secrets = self.fixture.secrets + [self.fixture.control.record['token']]
        if self.output.is_file():
            try:
                secret = json.loads(self.output.read_text(encoding='utf-8')).get('token')
                if secret:
                    secrets.append(secret)
            except (ValueError, OSError):
                pass
        self.assertFalse(any(value in text for value in secrets), 'terminal leaked a credential')
        self.assertNotIn('Traceback', text)

    def grants(self):
        with contextlib.closing(sqlite3.connect(self.fixture.data / 'collaboration.sqlite3')) as con:
            return con.execute('SELECT COUNT(*) FROM execution_grants').fetchone()[0]

    def list_args(self, *, limit=None, after=None):
        args = ['--install-root', str(self.install), '--workspace-root', str(self.fixture.root), '--list']
        if limit is not None:
            args += ['--limit', str(limit)]
        if after is not None:
            args += ['--after-grant-id', after]
        return args

    def listed(self, **kwargs):
        code, stdout, stderr = self.invoke(self.list_args(**kwargs))
        self.assertEqual((code, stderr), (0, ''))
        self.assert_safe(stdout, stderr)
        result = json.loads(stdout)
        self.assertEqual(set(result), admin.LIST_KEYS)
        return result

    def test_list_reads_exact_safe_metadata_pages_and_revoked_records_without_mutation(self):
        before = self.grants()
        first = self.listed(limit=1)
        self.assertEqual(len(first['items']), 1)
        self.assertTrue(first['has_more'])
        self.assertEqual(first['next_after_grant_id'], first['items'][0]['grant_id'])
        self.assertEqual(set(first['items'][0]), admin.ITEM_KEYS)
        last = self.listed(limit=1, after=first['next_after_grant_id'])
        self.assertEqual(len(last['items']), 1)
        self.assertFalse(last['has_more'])
        self.assertIsNone(last['next_after_grant_id'])
        self.assertEqual(self.grants(), before)
        self.assertFalse(self.output.exists())
        identifier = first['items'][0]['grant_id']
        code, stdout, stderr = self.invoke(self.args(role=None, subject=None, output=None, **{'grant-id': identifier}))
        self.assertEqual(code, 0)
        listed = self.listed()
        self.assertIsNotNone(next(i['revoked_at'] for i in listed['items'] if i['grant_id'] == identifier))
        self.assertEqual(self.grants(), before)
        self.assert_safe(stdout, stderr)

    def test_dropped_create_can_be_listed_and_revoked_without_remint_or_token_export(self):
        send = server.Handler._send
        def drop(handler, status, headers, body):
            if handler.path == '/api/execution/grant_create' and status == 200:
                handler.close_connection = True
                return
            return send(handler, status, headers, body)
        with patch.object(server.Handler, '_send', new=drop):
            code, stdout, stderr = self.invoke()
        self.assertEqual(code, 1)
        self.assert_safe(stdout, stderr)
        self.assertFalse(self.output.exists())
        before = self.grants()
        candidate = next(i for i in self.listed()['items'] if i['subject'] == 'source-one')
        self.assertEqual(self.grants(), before)
        self.assertFalse(self.output.exists())
        code, stdout, stderr = self.invoke(self.args(role=None, subject=None, output=None, **{'grant-id': candidate['grant_id']}))
        self.assertEqual(code, 0)
        self.assert_safe(stdout, stderr)
        self.assertIsNotNone(next(i['revoked_at'] for i in self.listed()['items'] if i['grant_id'] == candidate['grant_id']))
        self.assertEqual(self.grants(), before)

    def test_save_failure_can_be_reconciled_without_reexporting_or_overwriting_private_file(self):
        with patch.object(admin.os, 'fsync', side_effect=OSError('untrusted private error')):
            code, stdout, stderr = self.invoke()
        self.assertEqual(code, 1)
        original = self.output.read_bytes()
        before = self.grants()
        listed = self.listed()
        self.assertTrue(any(i['subject'] == 'source-one' for i in listed['items']))
        self.assertEqual(self.output.read_bytes(), original)
        self.assertEqual(self.grants(), before)
        self.assert_safe(stdout, stderr)

    def test_list_unknown_cursor_and_owner_denial_are_safe_and_not_retried(self):
        code, stdout, stderr = self.invoke(self.list_args(after=str(uuid.uuid4())))
        self.assertEqual((code, stdout, stderr.strip()), (1, '', 'grant_list_denied'))
        with patch.object(self.fixture.control, 'authorize', return_value=False):
            code, stdout, stderr = self.invoke(self.list_args())
        self.assertEqual((code, stdout, stderr.strip()), (1, '', 'grant_list_denied'))
        self.assert_safe(stdout, stderr)
        self.assertEqual(self.grants(), 2)

    def test_list_arguments_are_exclusive_and_bounded_before_control_or_network(self):
        invalid = [self.list_args(limit=0), self.list_args(limit=51), self.list_args(limit='true'),
                   self.list_args(after='Bearer synthetic-secret123456789'), self.list_args() + ['--role', 'source'],
                   self.list_args() + ['--subject', 'synthetic-secret'], self.list_args() + ['--output', str(self.output)],
                   self.list_args() + ['--grant-id', str(uuid.uuid4())], self.args() + ['--limit', '1'],
                   self.args() + ['--after-grant-id', str(uuid.uuid4())]]
        with patch.object(admin, '_control', side_effect=AssertionError('must not read control')), \
                patch.object(admin, '_http', side_effect=AssertionError('must not connect')):
            for args in invalid:
                code, stdout, stderr = self.invoke(args)
                self.assertEqual((code, stdout, stderr.strip()), (1, '', 'invalid_arguments'))
                self.assertNotIn('synthetic-secret', stdout + stderr)

    def test_list_rejects_unknown_fields_secrets_invalid_items_and_page_metadata(self):
        real_http = admin._http
        cases = [lambda r: r.update(token=self.fixture.secrets[0]),
                 lambda r: r.update(authority_id=None), lambda r: r.update(ledger_epoch='wrong'),
                 lambda r: r.update(protocol='wrong'), lambda r: r.update(has_more=1),
                 lambda r: r.update(next_after_grant_id=r['items'][0]['grant_id']),
                 lambda r: r['items'][0].update(token=self.fixture.secrets[0]),
                 lambda r: r['items'][0].update(subject=self.fixture.control.record['token']),
                 lambda r: r['items'][0].update(subject='Bearer synthetic-secret123456789'),
                 lambda r: r['items'][0].update(subject='x' * 201),
                 lambda r: r['items'][0].update(subject='unsafe\x7fsubject'),
                 lambda r: r['items'][0].update(role='owner'),
                 lambda r: r['items'][0].update(grant_id='wrong'),
                 lambda r: r['items'][0].update(created_at='2026-13-02T00:00:00+00:00'),
                 lambda r: r['items'][0].update(created_at='2026-10-02T00:00:00+01:00'),
                 lambda r: r['items'][0].update(revoked_at='2026-13-02T00:00:00Z'),
                 lambda r: r['items'].append(dict(r['items'][0])),
                 lambda r: r['items'].reverse()]
        for mutate in cases:
            def tamper(record, path, body=None, owner=False):
                code, result = real_http(record, path, body, owner)
                if path == '/api/execution/grant_list':
                    mutate(result)
                return code, result
            with patch.object(admin, '_http', side_effect=tamper):
                code, stdout, stderr = self.invoke(self.list_args())
            self.assertEqual((code, stdout, stderr.strip()), (1, '', 'grant_list_unverified'))
            self.assert_safe(stdout, stderr)
        self.assertEqual(self.grants(), 2)

    def test_list_scope_or_ledger_change_after_read_does_not_print_the_page(self):
        real_http = admin._http
        for change in ('connection_revision', 'binding_revision', 'execution_authority_id', 'ledger_epoch'):
            descriptions = []
            def changed(record, path, body=None, owner=False):
                code, result = real_http(record, path, body, owner)
                if path == '/api/execution/describe':
                    descriptions.append(True)
                    if len(descriptions) == 2:
                        if change == 'binding_revision':
                            result['workspace'][change] = 'c' * 64
                        else:
                            result[change] = 'c' * 64 if change == 'connection_revision' else str(uuid.uuid4())
                return code, result
            with patch.object(admin, '_http', side_effect=changed):
                code, stdout, stderr = self.invoke(self.list_args())
            self.assertEqual((code, stdout, stderr.strip()), (1, '', 'grant_list_unverified'))
            self.assert_safe(stdout, stderr)
        self.assertEqual(self.grants(), 2)

    def test_actual_list_subprocess_has_only_public_json_stdout(self):
        result = subprocess.run([sys.executable, '-B', str(Path(admin.__file__).resolve()), *self.list_args(limit=1)],
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual((result.returncode, result.stderr), (0, ''))
        self.assert_safe(result.stdout, result.stderr)
        self.assertEqual(set(json.loads(result.stdout)), admin.LIST_KEYS)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.grants(), 2)

    def test_list_bad_raw_http_json_or_oversized_body_never_reaches_stdout(self):
        send = server.Handler._send
        for invalid in (b'{"protocol":"aihub-execution/1","protocol":"private-invalid"}',
                        b'{"token":"' + self.fixture.secrets[0].encode('ascii') + b'"}',
                        b'x' * (admin.MAX_JSON_BYTES + 1)):
            def tamper(handler, status, headers, body):
                if handler.path == '/api/execution/grant_list' and status == 200:
                    body = invalid
                return send(handler, status, headers, body)
            with patch.object(server.Handler, '_send', new=tamper):
                code, stdout, stderr = self.invoke(self.list_args())
            self.assertEqual((code, stdout), (1, ''))
            self.assert_safe(stdout, stderr)
            self.assertNotIn('private-invalid', stderr)
        self.assertEqual(self.grants(), 2)

    def test_native_create_private_export_works_for_source_status_and_revoke(self):
        private = service_control._private
        with patch.object(service_control, '_private', wraps=private) as protect:
            code, stdout, stderr = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(stderr, '')
        self.assertIn(str(self.output), stdout)
        protect.assert_called_once_with(self.output)
        self.assert_safe(stdout, stderr)
        exported = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(exported['schema'], admin.EXPORT_SCHEMA)
        self.assertEqual(exported['role'], 'source')
        self.assertEqual(exported['subject'], 'source-one')
        self.assertEqual(exported['connection']['host'], '127.0.0.1')
        self.assertEqual(exported['connection']['port'], self.fixture.port)
        self.assertEqual(exported['connection']['service_instance_id'], self.fixture.control.record['instance_id'])
        self.assertTrue(admin._uuid(exported['grant_id']))
        self.assertFalse(self.fixture.control.record['token'] in self.output.read_text(encoding='utf-8'))
        if os.name != 'nt':
            self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        receipt = self.fixture.accept()
        code, result = self.fixture.request('/api/execution/status', {
            '_workspace_root': str(self.fixture.root), 'execution_id': receipt['execution_id']},
            bearer=exported['token'])
        self.assertEqual(code, 200)
        self.assertEqual(result['execution_id'], receipt['execution_id'])
        code, stdout, stderr = self.invoke(self.args(role=None, subject=None, output=None,
                                                    **{'grant-id': exported['grant_id']}))
        self.assertEqual(code, 0)
        self.assert_safe(stdout, stderr)
        code, _ = self.fixture.request('/api/execution/status', {
            '_workspace_root': str(self.fixture.root), 'execution_id': receipt['execution_id']},
            bearer=exported['token'])
        self.assertEqual(code, 403)

    def test_existing_output_is_preserved_before_control_read_or_network(self):
        self.output.write_text('用户原文件', encoding='utf-8')
        with patch.object(admin, '_http', side_effect=AssertionError('must not connect')), \
                patch.object(admin, '_control', side_effect=AssertionError('must not read control')):
            code, stdout, stderr = self.invoke()
        self.assertEqual((code, stdout, stderr.strip()), (1, '', 'output_exists'))
        self.assertEqual(self.output.read_text(encoding='utf-8'), '用户原文件')
        self.assertEqual(self.grants(), 2)

    def test_missing_invalid_duplicate_or_hardlinked_control_never_connects(self):
        path = self.fixture.control.path
        original = path.read_text(encoding='utf-8')
        path.unlink()
        with patch.object(admin, '_http', side_effect=AssertionError('must not connect')):
            code, stdout, stderr = self.invoke()
        self.assertEqual((code, stdout, stderr.strip()), (1, '', 'control_invalid'))
        path.write_text(original, encoding='utf-8')
        for changes in ({'schema': 'wrong'}, {'install_root': str(self.fixture.base / 'wrong')},
                        {'data_root': str(self.fixture.base / 'wrong-data')}, {'port': True},
                        {'app': 'wrong'}, {'instance_id': 'wrong'}, {'token': 'synthetic-invalid-token'}):
            record = dict(self.fixture.control.record, **changes)
            path.write_text(json.dumps(record), encoding='utf-8')
            with patch.object(admin, '_http', side_effect=AssertionError('must not connect')):
                code, stdout, stderr = self.invoke()
            self.assertEqual((code, stdout, stderr.strip()), (1, '', 'control_invalid'))
            self.assert_safe(stdout, stderr)
        path.write_text(original[:-1] + ',"port":12345}', encoding='utf-8')
        code, stdout, stderr = self.invoke()
        self.assertEqual((code, stdout, stderr.strip()), (1, '', 'control_invalid'))
        path.write_text(original, encoding='utf-8')
        link = self.fixture.base / 'linked-control.json'
        os.link(path, link)
        with patch.object(admin, '_http', side_effect=AssertionError('must not connect')):
            code, stdout, stderr = self.invoke()
        self.assertEqual((code, stdout, stderr.strip()), (1, '', 'control_invalid'))
        link.unlink()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.grants(), 2)

    def test_proxy_environment_does_not_affect_actual_cli_loopback_connection(self):
        env = dict(os.environ)
        env.update({key: 'http://127.0.0.1:1' for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                                       'http_proxy', 'https_proxy', 'all_proxy')})
        env['NO_PROXY'] = ''
        env['no_proxy'] = ''
        result = subprocess.run([sys.executable, '-B', str(Path(admin.__file__).resolve()), *self.args()],
                                capture_output=True, text=True, encoding='utf-8', env=env, timeout=15)
        self.assertEqual(result.returncode, 0)
        self.assert_safe(result.stdout, result.stderr)
        self.assertTrue(self.output.is_file())
        self.assertEqual(self.grants(), 3)

    def test_owner_authentication_failure_is_fixed_error_and_not_retried(self):
        with patch.object(self.fixture.control, 'authorize', return_value=False):
            code, stdout, stderr = self.invoke()
        self.assertEqual((code, stdout, stderr.strip()), (1, '', 'grant_denied'))
        self.assert_safe(stdout, stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.grants(), 2)

    def test_dropped_grant_reply_is_uncertain_and_never_creates_a_second_grant(self):
        send = server.Handler._send
        dropped = []
        def drop(handler, status, headers, body):
            if handler.path == '/api/execution/grant_create' and status == 200:
                dropped.append(True)
                handler.close_connection = True
                return
            send(handler, status, headers, body)
        with patch.object(server.Handler, '_send', new=drop):
            code, stdout, stderr = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(stdout, '')
        self.assertTrue(stderr.startswith('grant_status_unverified '))
        self.assertIn('接入待核对', stderr)
        self.assert_safe(stdout, stderr)
        self.assertEqual(dropped, [True])
        self.assertEqual(self.grants(), 3)
        self.assertFalse(self.output.exists())

    def test_save_failure_preserves_private_received_grant_without_success_message(self):
        with patch.object(admin.os, 'fsync', side_effect=OSError('synthetic-secret-in-exception')):
            code, stdout, stderr = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(stdout, '')
        self.assertTrue(stderr.startswith('grant_accepted_export_failed '))
        self.assertNotIn('synthetic-secret-in-exception', stderr)
        self.assert_safe(stdout, stderr)
        self.assertEqual(self.grants(), 3)
        self.assertTrue(self.output.is_file())
        self.assertTrue(bool(json.loads(self.output.read_text(encoding='utf-8')).get('token')))

    def test_invalid_grant_reply_cannot_be_exported_or_retried(self):
        send = server.Handler._send
        cases = [('role', 'worker'), ('subject', 'wrong-subject'), ('protocol', 'wrong-protocol'),
                 ('grant_id', 'wrong'), ('authority_id', 'wrong'), ('ledger_epoch', 'wrong'),
                 ('token', 'a' * 42), ('token', int('1' * 43)),
                 ('token', self.fixture.control.record['token']), ('secret_returned_once', False)]
        for index, (field, value) in enumerate(cases):
            def tamper(handler, status, headers, body):
                if handler.path == '/api/execution/grant_create' and status == 200:
                    result = json.loads(body)
                    result[field] = value
                    body = json.dumps(result).encode('utf-8')
                return send(handler, status, headers, body)
            with patch.object(server.Handler, '_send', new=tamper):
                code, stdout, stderr = self.invoke()
            self.assertEqual(code, 1)
            self.assertEqual(stdout, '')
            self.assertTrue(stderr.startswith('grant_status_unverified '))
            self.assert_safe(stdout, stderr)
            self.assertFalse(self.output.exists())
            self.assertEqual(self.grants(), 3 + index)

    def test_health_mismatch_or_redirect_does_not_mint_or_follow(self):
        real_http = admin._http
        for field, value in (('app', 'wrong'), ('service_instance_id', 'wrong'),
                             ('control_protocol', 'wrong'), ('install_root', str(self.fixture.base)),
                             ('port', self.fixture.port + 1)):
            calls = []
            def mismatched(record, path, body=None, owner=False):
                calls.append(path)
                code, result = real_http(record, path, body, owner)
                if path == '/api/health':
                    result[field] = value
                return code, result
            with patch.object(admin, '_http', side_effect=mismatched):
                code, stdout, stderr = self.invoke()
            self.assertEqual((code, stdout, stderr.strip()), (1, '', 'identity_unverified'))
            self.assertEqual(calls, ['/api/health'])
        send = server.Handler._send
        paths = []
        def redirect(handler, status, headers, body):
            paths.append(handler.path)
            if handler.path == '/api/health':
                return send(handler, 302, {'Location': 'http://example.invalid/synthetic-secret'}, b'synthetic-secret')
            return send(handler, status, headers, body)
        with patch.object(server.Handler, '_send', new=redirect):
            code, stdout, stderr = self.invoke()
        self.assertEqual((code, stdout, stderr.strip()), (1, '', 'identity_unverified'))
        self.assertEqual(paths, ['/api/health'])
        self.assertEqual(self.grants(), 2)
        self.assertFalse(self.output.exists())

    def test_argument_errors_do_not_echo_potential_secrets(self):
        for args in (self.args(role='synthetic-secret-bad-role'), self.args(subject='Bearer synthetic-secret123456789'),
                     self.args() + ['--unknown-synthetic-secret']):
            code, stdout, stderr = self.invoke(args)
            self.assertEqual((code, stdout, stderr.strip()), (1, '', 'invalid_arguments'))
            self.assertNotIn('synthetic-secret', stdout + stderr)
        for overrides in ({'subject': self.fixture.control.record['token']},
                          {'output': str(self.fixture.base / (self.fixture.control.record['token'] + '.json'))}):
            code, stdout, stderr = self.invoke(self.args(**overrides))
            self.assertEqual((code, stdout, stderr.strip()), (1, '', 'invalid_arguments'))
            self.assert_safe(stdout, stderr)
        self.assertEqual(self.grants(), 2)

    def test_create_subject_uses_list_safe_rule_before_network_or_output_reservation(self):
        subjects = ['origin\x7flabel', 'origin\x85label', 'origin\nlabel', 'origin\rlabel',
                    'origin\tlabel', 'origin\ud800label', 'origin\udc00label', 'x' * 43, 'a' * 64]
        with patch.object(admin, '_control', side_effect=AssertionError('must not read control')), \
                patch.object(admin, '_http', side_effect=AssertionError('must not connect')), \
                patch.object(admin, '_output_path', side_effect=AssertionError('must not reserve output')), \
                patch.object(admin.os, 'open', side_effect=AssertionError('must not reserve output')):
            for subject in subjects:
                code, stdout, stderr = self.invoke(self.args(subject=subject))
                self.assertEqual((code, stdout, stderr.strip()), (1, '', 'invalid_arguments'))
        self.assertFalse(self.output.exists())
        self.assertEqual(self.grants(), 2)

    def test_create_safe_uuid_and_chinese_subject_remain_listable(self):
        subjects = [str(uuid.uuid4()), '映序持久身份😀']
        for index, subject in enumerate(subjects):
            output = self.fixture.base / ('合法身份%d.json' % index)
            code, stdout, stderr = self.invoke(self.args(subject=subject, output=str(output)))
            self.assertEqual((code, stderr), (0, ''))
            self.assert_safe(stdout, stderr)
            self.assertEqual(json.loads(output.read_text(encoding='utf-8'))['subject'], subject)
        listed = self.listed()
        self.assertTrue(all(any(row['subject'] == subject for row in listed['items']) for subject in subjects))
        self.assertEqual(self.grants(), 4)


class EmptyExecutionAdminTests(unittest.TestCase):
    def test_empty_and_legacy_store_list_do_not_initialize_execution_schema(self):
        fixture = http_fixture.ExecutionHTTPTests('test_describe_is_readonly_and_handler_supplies_public_identity')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.control.publish()
        args = ['--install-root', str(fixture.base / 'app'), '--workspace-root', str(fixture.root), '--list']
        store = fixture.data / 'collaboration.sqlite3'
        def check_empty():
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = admin.main(args)
            self.assertEqual((code, stderr.getvalue()), (0, ''))
            self.assertEqual(json.loads(stdout.getvalue()), {'protocol': admin.PROTOCOL, 'authority_id': None,
                'ledger_epoch': None, 'items': [], 'has_more': False, 'next_after_grant_id': None})
            self.assertNotIn(fixture.control.record['token'], stdout.getvalue())
        check_empty()
        self.assertFalse(store.exists())
        with contextlib.closing(sqlite3.connect(store)) as con:
            con.execute('CREATE TABLE prior_state(value TEXT)')
            con.execute("INSERT INTO prior_state VALUES('preserve')")
            con.commit()
        before = store.read_bytes()
        check_empty()
        self.assertEqual(store.read_bytes(), before)
        with contextlib.closing(sqlite3.connect(store)) as con:
            self.assertEqual(con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('prior_state',)])
        self.assertFalse(list(fixture.data.glob('collaboration.sqlite3*backup*')))


if __name__ == '__main__':
    unittest.main()
