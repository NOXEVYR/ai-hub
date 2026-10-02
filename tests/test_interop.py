"""Interop tests use synthetic declarations, identities and SQLite stores only."""
import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import tracemalloc
import unittest
import uuid
from unittest.mock import patch

from aihub import capabilities, collaboration, config, harnesses, interop, service_control, workspace


class InteropTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihub-interop-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / '工作区'
        self.root.mkdir()
        self.data = self.base / 'data'
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True}
        patcher = patch.object(config, 'DATA_DIR', str(self.data))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.identity = {'app': 'ai-hub', 'service_instance_id': str(uuid.uuid4()),
                         'install_root': str(self.base / 'App'), 'control_protocol': service_control.PROTOCOL,
                         'port': 8765, 'server_version': '2.13.9'}
        self.item = {'key': 'demo.code', 'name': '中文代码检查', 'kind': 'mcp_tool', 'domains': ['code'],
                     'description': '只声明，不直接运行。', 'inputs': {'type': 'object',
                     'properties': {'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 80}},
                     'required': ['prompt'], 'additionalProperties': False}, 'outputs': ['report'],
                     'constraints': ['需要工作端领取']}

    def execute(self, action, body=None, cfg=None, identity=None):
        return interop.execute(cfg or self.cfg, action, body, public_identity=self.identity if identity is None else identity)

    def describe(self, **kwargs):
        return self.execute('interop_describe', **kwargs)

    def declare(self, item=None, client='alice', tool='codex', cfg=None):
        cfg = cfg or self.cfg
        if tool not in harnesses.allowed_ids(cfg, include_disabled=True):
            harnesses.save(cfg, {'id': tool, 'revision': 0, 'connection_mode': 'mcp_stdio'})
        collaboration.execute(cfg, 'client_heartbeat', {'client_id': client, 'tool': tool, 'name': client, 'protocol_version': 1})
        capabilities.publish(cfg, {'client_id': client, 'capabilities_json': json.dumps([item or self.item], ensure_ascii=False)})
        with contextlib.closing(sqlite3.connect(self.data / 'capabilities.sqlite3')) as con:
            root = config._key(cfg['ai_root'])
            return con.execute('SELECT id FROM capabilities WHERE root=? AND client_id=?', (root, client)).fetchone()[0]

    def query(self, identifier):
        return {'capability_id': identifier, 'connection_revision': self.describe()['connection_revision'],
                '_workspace_root': str(self.root)}

    def snapshot(self, identifier, **extra):
        return self.execute('interop_capability_snapshot', dict(self.query(identifier), **extra))

    def assert_error(self, code, operation, status=None):
        with self.assertRaises(interop.InteropError) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)
        if status is not None:
            self.assertEqual(raised.exception.http_status, status)
        return raised.exception

    def data_state(self):
        if not self.data.exists():
            return None
        # SQLite read locks may affect an existing SHM reader slot. DB and WAL
        # bytes, and every pathname, must remain unchanged by observations.
        return tuple(sorted((str(file.relative_to(self.data)), file.is_dir(),
                             None if file.is_dir() or file.name.endswith('-shm') else hashlib.sha256(file.read_bytes()).hexdigest())
                            for file in self.data.rglob('*')))

    def test_empty_describe_never_creates_store_or_reads_environment_private_config(self):
        cfg = {'ai_root': '', 'workspace_managed': False}
        with patch.object(sqlite3, 'connect', side_effect=AssertionError('database access')), \
                patch.object(workspace, '_revision', side_effect=AssertionError('private config')), \
                patch.object(os, 'environ', {}) as environ:
            result = interop.execute(cfg, 'interop_describe')
        self.assertEqual(result['protocol'], 'aihub-interop/1')
        self.assertEqual(result['identity']['status'], 'identity_unavailable')
        self.assertEqual(result['workspace']['status'], 'workspace_unavailable')
        self.assertEqual(result['workspace_root'], '')
        self.assertIsNone(result['connection_revision'])
        self.assertFalse(self.data.exists())
        self.assertFalse(result['operations']['direct_execution'])
        self.assertFalse(result['operations']['native_cancel'])
        self.assertFalse(result['operations']['cloud_upload'])
        self.assertEqual(result['limits']['report_bytes'], 1024 * 1024)
        self.assertEqual(result['limits']['output_bytes'], 64 * 1024 * 1024)

    def test_workspace_binding_ignores_private_config_and_directory_mtime(self):
        with patch.object(workspace, '_revision', side_effect=AssertionError('private config')), patch.object(sqlite3, 'connect', side_effect=AssertionError('database access')):
            first = self.describe()
            (self.root / 'ordinary-output.txt').write_text('output', encoding='utf-8')
            second = self.describe(cfg=dict(self.cfg, private_token='do-not-export', unrelated_setting=123))
        self.assertEqual(first['workspace']['binding_revision'], second['workspace']['binding_revision'])
        self.assertEqual(first['connection_revision'], second['connection_revision'])
        self.assertNotIn('do-not-export', json.dumps(second))
        self.assertFalse(second['workspace']['full_configuration_revision'])
        self.assertFalse(second['workspace']['cross_machine_uuid'])

    def test_identity_only_comes_from_trusted_keyword_and_whitelisted_fields(self):
        result = self.describe(identity=dict(self.identity, token='private-value'))
        self.assertNotIn('private-value', json.dumps(result))
        self.assertEqual(result['identity']['app'], 'ai-hub')
        error = self.assert_error('invalid_request', lambda: self.execute('interop_describe', {'identity': {'token': 'private-value'}}), 400)
        self.assertNotIn('private-value', str(error))
        self.assertEqual(interop.execute(self.cfg, 'interop_describe')['identity']['status'], 'identity_unavailable')
        self.assertEqual(self.describe(identity=dict(self.identity, control_protocol=1))['identity']['status'], 'identity_unavailable')

    def test_sqlite_read_budget_interrupts_expensive_queries(self):
        self.declare()
        with patch.object(interop, 'READ_TIMEOUT_SECONDS', 0.01):
            def expensive():
                with interop._readonly('capabilities.sqlite3', {'capabilities': {'id'}}) as con:
                    con.execute('WITH RECURSIVE count(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM count WHERE x<100000000) SELECT sum(x) FROM count').fetchone()
            self.assert_error('storage_unavailable', expensive, 503)

    def test_oversized_database_metadata_is_not_materialized(self):
        identifier = self.declare()
        huge = 'x' * (4 * 1024 * 1024)
        cases = [('collaboration.sqlite3', 'clients', 'last_seen', 'invalid_client_metadata'),
                 ('capabilities.sqlite3', 'capabilities', 'client_id', 'invalid_declaration'),
                 ('capabilities.sqlite3', 'capabilities', 'key', 'invalid_declaration'),
                 ('capabilities.sqlite3', 'capabilities', 'tool', 'invalid_declaration'),
                 ('capabilities.sqlite3', 'capabilities', 'updated_at', 'invalid_declaration')]
        for name, table, field, error in cases:
            with self.subTest(field=field):
                with contextlib.closing(sqlite3.connect(self.data / name)) as con:
                    original = con.execute('SELECT ' + field + ' FROM ' + table + ' LIMIT 1').fetchone()[0]
                    con.execute('UPDATE ' + table + ' SET ' + field + '=?', (huge,))
                    con.commit()
                tracemalloc.start()
                try:
                    self.assert_error(error, lambda: self.snapshot(identifier), 422)
                    self.assertLess(tracemalloc.get_traced_memory()[1], 1024 * 1024)
                finally:
                    tracemalloc.stop()
                    with contextlib.closing(sqlite3.connect(self.data / name)) as con:
                        con.execute('UPDATE ' + table + ' SET ' + field + '=?', (original,))
                        con.commit()

    def test_view_cannot_substitute_for_registry_table(self):
        identifier = self.declare()
        with contextlib.closing(sqlite3.connect(self.data / 'capabilities.sqlite3')) as con:
            con.execute('ALTER TABLE capabilities RENAME TO saved_capabilities')
            con.execute('CREATE VIEW capabilities AS SELECT * FROM saved_capabilities')
            con.commit()
        self.assert_error('storage_schema_unavailable', lambda: self.snapshot(identifier), 503)

    def test_exact_snapshot_preserves_stored_utf8_bytes_and_origin(self):
        identifier = self.declare()
        with contextlib.closing(sqlite3.connect(self.data / 'capabilities.sqlite3')) as con:
            original = con.execute('SELECT payload FROM capabilities WHERE id=?', (identifier,)).fetchone()[0]
        before = self.data_state()
        result = self.snapshot(identifier)
        declaration = result['declaration']
        self.assertEqual((declaration['text'], declaration['bytes'], declaration['sha256']),
                         (original, len(original.encode('utf-8')), hashlib.sha256(original.encode('utf-8')).hexdigest()))
        self.assertEqual(declaration['parsed'], json.loads(original))
        self.assertEqual(declaration['origin'], 'stored_normalized_declaration')
        self.assertFalse(result['evidence']['declaration']['original_publisher_bytes_preserved'])
        self.assertEqual(result['evidence']['actual_invocation']['status'], 'unverified')
        self.assertEqual(result['execution']['routing'], 'target_tool_not_declared_client')
        self.assertFalse(result['execution']['dispatch_supports_declaration_cas'])
        self.assertEqual(self.data_state(), before)

    def test_republish_same_key_keeps_id_but_expected_old_hash_conflicts_and_frozen_object_stays(self):
        identifier = self.declare()
        exported = self.snapshot(identifier)
        frozen = copy.deepcopy(exported)
        again = self.declare()
        same = self.snapshot(again)
        self.assertEqual(again, identifier)
        self.assertEqual(same['snapshot_id'], exported['snapshot_id'])
        self.declare(dict(self.item, description='新声明正文'))
        self.assert_error('declaration_changed', lambda: self.snapshot(identifier,
                          expected_declaration_sha256=exported['declaration']['sha256']), 409)
        latest = self.snapshot(identifier)
        self.assertNotEqual(latest['snapshot_id'], exported['snapshot_id'])
        self.assertEqual(exported, frozen)

    def test_two_source_clients_remain_distinct_under_same_tool_and_key(self):
        first, second = self.declare(), self.declare(client='bob')
        self.assertNotEqual(first, second)
        a, b = self.snapshot(first), self.snapshot(second)
        self.assertEqual((a['selected']['client_id'], b['selected']['client_id']), ('alice', 'bob'))
        self.assertEqual((a['selected']['target_tool'], b['selected']['target_tool']), ('codex', 'codex'))
        self.assertNotEqual(a['snapshot_id'], b['snapshot_id'])

    def test_existing_multibyte_client_id_contract_is_preserved(self):
        for client in ('甲' * 41, '甲' * 120, '😀' * 120):
            with self.subTest(characters=len(client)):
                identifier = self.declare(client=client)
                self.assertEqual(self.snapshot(identifier)['selected']['client_id'], client)

    def test_connection_rejects_workspace_switch_root_replacement_and_instance_changes(self):
        identifier = self.declare()
        body = self.query(identifier)
        other = self.base / 'other-workspace'
        other.mkdir()
        self.assert_error('workspace_changed', lambda: self.execute('interop_capability_snapshot', body,
                          cfg=dict(self.cfg, ai_root=str(other))), 409)
        self.assert_error('workspace_changed', lambda: self.describe(body={'_workspace_root': str(other)}), 409)
        for changed in ({'service_instance_id': str(uuid.uuid4())}, {'port': 12345}, {'install_root': str(self.base / 'other-App')}):
            self.assert_error('connection_changed', lambda changed=changed: self.execute('interop_capability_snapshot', body,
                              identity=dict(self.identity, **changed)), 409)
        self.root.rename(self.base / 'retained-workspace')
        self.root.mkdir()
        self.assert_error('connection_changed', lambda: self.execute('interop_capability_snapshot', body), 409)

    def test_disabled_harness_and_stale_heartbeat_are_distinct_from_execution(self):
        identifier = self.declare()
        harnesses.save(self.cfg, {'id': 'codex', 'revision': 1, 'enabled': False})
        with contextlib.closing(sqlite3.connect(self.data / 'collaboration.sqlite3')) as con:
            con.execute('UPDATE clients SET last_seen=? WHERE id=?', ('2000-01-01T00:00:00+00:00', 'alice'))
            con.commit()
        result = self.snapshot(identifier)
        self.assertEqual((result['harness']['revision'], result['harness']['enabled'], result['harness']['origin']), (2, False, 'explicit'))
        self.assertEqual(result['evidence']['heartbeat']['status'], 'not_recently_seen')
        self.assertFalse(result['evidence']['heartbeat']['recent'])
        self.assertEqual(result['evidence']['actual_invocation']['status'], 'unverified')

    def test_missing_and_unreadable_stores_are_not_conflated(self):
        identifier = 'a' * 32
        self.assert_error('capability_catalog_missing', lambda: self.snapshot(identifier), 404)
        self.assertFalse(self.data.exists())
        actual = self.declare()
        self.assert_error('capability_not_found', lambda: self.snapshot(identifier), 404)
        path = self.data / 'capabilities.sqlite3'
        path.write_bytes(b'not a sqlite database')
        self.assert_error('storage_unavailable', lambda: self.snapshot(actual), 503)

    def test_wal_committed_declaration_is_visible_without_database_or_wal_changes(self):
        identifier = self.declare()
        path = self.data / 'capabilities.sqlite3'
        with contextlib.closing(sqlite3.connect(path)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            payload = capabilities._capability(dict(self.item, description='仅在 WAL 中的已提交声明'))
            exact = json.dumps(payload, ensure_ascii=False)
            writer.execute('UPDATE capabilities SET payload=? WHERE id=?', (exact, identifier))
            writer.commit()
            self.assertTrue(Path(str(path) + '-wal').exists())
            before = self.data_state()
            exported = self.snapshot(identifier)
            self.assertEqual(exported['declaration']['text'], exact)
            self.assertEqual(self.data_state(), before)

    def test_wal_without_shm_is_refused_without_creating_sidecars(self):
        identifier = self.declare()
        path = self.data / 'capabilities.sqlite3'
        Path(str(path) + '-wal').write_bytes(b'synthetic WAL without SHM')
        before = self.data_state()
        self.assert_error('storage_unavailable', lambda: self.snapshot(identifier), 503)
        self.assertEqual(self.data_state(), before)
        self.assertFalse(Path(str(path) + '-shm').exists())

    def test_all_associated_database_hardlinks_are_refused(self):
        identifier = self.declare()
        for name in ('capabilities.sqlite3', 'harnesses.sqlite3', 'collaboration.sqlite3'):
            with self.subTest(name=name):
                link = self.base / 'synthetic-hardlink'
                os.link(self.data / name, link)
                try:
                    self.assert_error('unsafe_storage', lambda: self.snapshot(identifier), 409)
                finally:
                    link.unlink()

    def test_linked_data_ancestor_is_refused(self):
        identifier = self.declare()
        alias = self.base / 'linked-data'
        try:
            os.symlink(self.data, alias, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('host does not permit synthetic directory symlinks')
        with patch.object(config, 'DATA_DIR', str(alias)):
            self.assert_error('unsafe_storage', lambda: self.snapshot(identifier), 409)

    def test_invalid_schema_credentials_and_duplicate_fields_never_export(self):
        identifier = self.declare()
        for payload in (dict(capabilities._capability(self.item), credentials='private-token'),
                        dict(capabilities._capability(self.item), inputs={'type': 'object', '$ref': 'https://example.invalid/private'}),
                        dict(capabilities._capability(self.item), description='Bearer abcdefghijklmnopqrstuvwxyz')):
            with contextlib.closing(sqlite3.connect(self.data / 'capabilities.sqlite3')) as con:
                con.execute('UPDATE capabilities SET payload=? WHERE id=?', (json.dumps(payload), identifier))
                con.commit()
            error = self.assert_error('invalid_declaration', lambda: self.snapshot(identifier), 422)
            self.assertNotIn('private-token', str(error))
            self.assertNotIn('abcdefghijklmnopqrstuvwxyz', str(error))
        with contextlib.closing(sqlite3.connect(self.data / 'capabilities.sqlite3')) as con:
            con.execute('UPDATE capabilities SET payload=? WHERE id=?', ('{"key":"a","key":"b"}', identifier))
            con.commit()
        self.assert_error('invalid_declaration', lambda: self.snapshot(identifier), 422)

    def test_github_credential_patterns_are_refused_including_escaped_json(self):
        identifier = self.declare()
        for token in ('ghp_' + 'A' * 36, 'github_pat_' + 'B' * 40):
            payload = capabilities._capability(dict(self.item, description='fixture ' + token))
            for escaped in (False, True):
                with self.subTest(prefix=token.split('_')[0], escaped=escaped):
                    text = json.dumps(payload, ensure_ascii=False)
                    if escaped:
                        text = text.replace(token[0], '\\u%04x' % ord(token[0]))
                    with contextlib.closing(sqlite3.connect(self.data / 'capabilities.sqlite3')) as con:
                        con.execute('UPDATE capabilities SET payload=? WHERE id=?', (text, identifier))
                        con.commit()
                    error = self.assert_error('invalid_declaration', lambda: self.snapshot(identifier), 422)
                    self.assertNotIn(token, str(error))

    def test_declaration_and_response_limits_refuse_instead_of_truncating(self):
        identifier = self.declare()
        with patch.object(interop, 'MAX_RESPONSE_BYTES', 100):
            self.assert_error('response_too_large', lambda: self.snapshot(identifier), 413)
        with contextlib.closing(sqlite3.connect(self.data / 'capabilities.sqlite3')) as con:
            con.execute('UPDATE capabilities SET payload=? WHERE id=?', ('x' * (interop.MAX_DECLARATION_BYTES + 1), identifier))
            con.commit()
        self.assert_error('declaration_too_large', lambda: self.snapshot(identifier), 413)

    def test_association_absence_and_schema_failure_are_explicit(self):
        identifier = self.declare()
        (self.data / 'harnesses.sqlite3').unlink()
        result = self.snapshot(identifier)
        self.assertEqual(result['harness']['status'], 'registration_unavailable')
        self.assertEqual(result['harness']['origin'], 'legacy_usage')
        self.assertIsNone(result['harness']['enabled'])
        with contextlib.closing(sqlite3.connect(self.data / 'collaboration.sqlite3')) as con:
            con.execute('DROP TABLE clients')
            con.commit()
        self.assert_error('storage_schema_unavailable', lambda: self.snapshot(identifier), 503)

    def test_existing_capability_and_report_contracts_are_not_rewritten(self):
        identifier = self.declare()
        task = collaboration.execute(self.cfg, 'task_create', {'project': '合成', 'title': '报告兼容'})
        claim = collaboration.execute(self.cfg, 'task_claim', {'task_id': task['id'], 'client_id': 'alice'})
        lease = {'task_id': task['id'], 'client_id': 'alice', 'lease_token': claim['lease_token']}
        collaboration.execute(self.cfg, 'artifact_write', dict(lease, kind='report', category='report',
            title='本次报告', filename='report.md', content='结果', memory_candidates=[]))
        before = self.data_state()
        self.snapshot(identifier)
        self.assertEqual(self.data_state(), before)
        self.assertEqual(collaboration.execute(self.cfg, 'task_finish', lease)['report_submission']['status'], 'submitted')


if __name__ == '__main__':
    unittest.main()
