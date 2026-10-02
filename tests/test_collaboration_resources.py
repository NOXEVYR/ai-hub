"""Synthetic task resource accounting; never closes a real tab or process."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from aihub import collaboration as core, collaboration_resources as resources
from aihub import collaboration_api as api, config, harnesses


class ResourceLedgerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='aihub-resource-ledger-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        self.data = self.base / 'data'
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True}
        override = patch.object(config, 'DATA_DIR', str(self.data))
        override.start()
        self.addCleanup(override.stop)
        harnesses.save(self.cfg, {'id': 'codex', 'revision': 0, 'connection_mode': 'mcp_stdio'})
        for client in ('owner', 'other'):
            api.execute(self.cfg, 'client_heartbeat', {'client_id': client, 'tool': 'codex',
                                                      'name': client, 'protocol_version': 1})
        self.task, self.lease = self.new_task()

    def new_task(self, client='owner'):
        task = api.execute(self.cfg, 'task_create', {'project': 'Synthetic', 'title': '合成资源任务', 'report_policy': 'optional'})
        claim = api.execute(self.cfg, 'task_claim', {'task_id': task['id'], 'client_id': client})
        return task, {'task_id': task['id'], 'client_id': client, 'lease_token': claim['lease_token']}

    def call(self, action, actor='ui', **payload):
        return resources.execute(self.cfg, action, {'_workspace_root': str(self.root), **payload}, actor)

    def register(self, resource_type='browser_tab', identity=None, **extra):
        if identity is None:
            identity = {'browser_id': 'test', 'session_id': 'synthetic-run', 'tab_id': 'tab-1'}
        return self.call('resource_register', **{**self.lease, 'resource_type': resource_type,
            'identity': identity, 'ownership': 'task_exclusive', 'temporary': True, **extra})

    def cleanup(self, row, state='closed', outcome='absent', **extra):
        evidence = {'identity': row['identity'], 'observed_at': core._now(), 'outcome': outcome,
                    'detail': '合成客户端证据，不表示本机独立核实'}
        return self.call('resource_cleanup_report', **{**self.lease, 'resource_id': row['id'],
                    'state': state, 'evidence': evidence, **extra})

    def finish(self):
        api.execute(self.cfg, 'task_finish', self.lease)

    def test_old_store_empty_list_does_not_create_resource_table(self):
        result = self.call('resource_list')
        self.assertEqual(result['items'], [])
        self.assertTrue(result['available'])
        with core.store(self.cfg) as (con, _):
            self.assertFalse(con.execute("SELECT 1 FROM sqlite_master WHERE name='task_resources'").fetchone())

    def test_registration_is_idempotent_and_preserves_metadata(self):
        row = self.register()
        duplicate = self.register()
        self.assertEqual(row['id'], duplicate['id'])
        self.assertTrue(duplicate['deduplicated'])
        self.assertEqual(row['state'], 'running')
        self.assertNotIn('lease_hash', row)
        self.assertFalse(row['host_verified'])
        self.assertFalse(row['automatic_control'])
        with self.assertRaises(ValueError):
            self.register(ownership='shared')
        with core.store(self.cfg) as (con, root):
            self.assertEqual(con.execute("SELECT count(*) FROM audit WHERE root=? AND action='resource_register'", (root,)).fetchone()[0], 1)

    def test_completed_task_becomes_pending_and_original_client_can_report(self):
        row = self.register()
        self.finish()
        listed = self.call('resource_list')['items'][0]
        self.assertEqual(listed['state'], 'cleanup_pending')
        closed = self.cleanup(row)
        self.assertEqual(closed['state'], 'closed')
        self.assertEqual(closed['evidence_source'], 'client_report')
        self.assertFalse(closed['host_verified'])
        with self.assertRaises(ValueError):
            self.register(identity={'browser_id': 'test', 'session_id': 'other-run', 'tab_id': 'tab-2'})

    def test_failed_cancelled_removed_task_pending_without_mutating_rows(self):
        self.register()
        for state in ('failed', 'cancelled'):
            with core.store(self.cfg) as (con, root):
                con.execute('UPDATE tasks SET status=? WHERE root=? AND id=?', (state, root, self.task['id']))
            self.assertEqual(self.call('resource_list')['items'][0]['state'], 'cleanup_pending')
        with core.store(self.cfg) as (con, root):
            con.execute('UPDATE tasks SET deleted_at=? WHERE root=? AND id=?', (core._now(), root, self.task['id']))
            self.assertEqual(con.execute('SELECT state FROM task_resources').fetchone()[0], 'running')
        self.assertTrue(self.call('resource_list')['items'][0]['task_removed'])

    def test_handoff_and_new_lease_cannot_steal_registration(self):
        row = self.register()
        original = dict(self.lease)
        api.execute(self.cfg, 'task_handoff', {**self.lease, 'target_tool': 'codex'})
        claim = api.execute(self.cfg, 'task_claim', {'task_id': self.task['id'], 'client_id': 'other'})
        self.assertEqual(self.call('resource_list')['items'][0]['state'], 'cleanup_pending')
        self.lease = {'task_id': self.task['id'], 'client_id': 'other', 'lease_token': claim['lease_token']}
        with self.assertRaises(ValueError):
            self.cleanup(row)
        with self.assertRaises(ValueError):
            self.register()
        self.lease = original
        self.assertEqual(self.cleanup(row)['state'], 'closed')

    def test_cross_task_client_root_and_token_are_rejected(self):
        row = self.register()
        task, lease = self.new_task('other')
        for fields in ({'task_id': task['id']}, {'client_id': 'other'}, {'lease_token': lease['lease_token']}):
            with self.assertRaises(ValueError):
                self.cleanup(row, **fields)
        with self.assertRaises(ValueError):
            resources.execute(self.cfg, 'resource_list', {})
        with self.assertRaises(ValueError):
            resources.execute(self.cfg, 'resource_list', {'_workspace_root': str(self.base)})

    def test_mcp_list_is_client_scoped_and_ui_summary_has_counts(self):
        self.register()
        task, lease = self.new_task('other')
        self.call('resource_register', **lease, resource_type='browser_tab',
                  identity={'browser_id': 'test', 'session_id': 'other', 'tab_id': 'tab-2'})
        all_rows = self.call('resource_list')
        self.assertEqual(all_rows['total'], 2)
        self.assertEqual(all_rows['counts'], {'running': 1, 'manual_required': 1})
        own = self.call('resource_list', actor='mcp', client_id='owner')
        self.assertEqual(own['total'], 1)
        self.assertEqual(self.call('resource_list', actor='mcp', client_id='owner', task_id=task['id'])['total'], 0)
        self.assertEqual(self.call('resource_list', offset=1)['total'], 2)
        self.assertEqual(len(self.call('resource_list', offset=1)['items']), 1)
        with self.assertRaises(ValueError):
            self.call('resource_list', actor='mcp')

    def test_unknown_shared_user_and_non_temporary_do_not_become_pending(self):
        for index, fields in enumerate(({'ownership': 'shared'}, {'ownership': 'user_owned'}, {'temporary': False}, {})):
            payload = {**self.lease, 'resource_type': 'browser_tab',
                       'identity': {'browser_id': 'test', 'session_id': 's', 'tab_id': str(index)}, **fields}
            row = self.call('resource_register', **payload)
            self.assertEqual(row['state'], 'manual_required')
            self.assertFalse(row['cleanup_eligible'])
            with self.assertRaises(ValueError):
                self.cleanup(row)
        self.finish()
        self.assertEqual(self.call('resource_list')['counts'], {'manual_required': 4})

    def test_pid_reuse_requires_start_identity_and_cannot_close_original(self):
        old = {'pid': 4321, 'process_started_at': '2026-10-01T00:00:00+00:00'}
        newer = {**old, 'process_started_at': '2026-10-01T01:00:00+00:00'}
        row = self.register('service_process', old)
        second = self.register('service_process', newer)
        self.assertNotEqual(row['id'], second['id'])
        evidence = {'identity': second['identity'], 'observed_at': core._now(), 'outcome': 'absent'}
        with self.assertRaises(ValueError):
            self.cleanup(row, evidence=evidence)
        for identity in ({'pid': 4321}, {'pid': True, 'process_started_at': old['process_started_at']},
                         {'pid': 1, 'process_started_at': '2026-10-01T00:00:00'}):
            with self.assertRaises(ValueError):
                self.register('service_process', identity)

    def test_listener_identity_is_bound_to_process_and_loopback_port(self):
        valid = {'pid': 500, 'process_started_at': '2026-10-01T00:00:00Z', 'host': '127.0.0.1', 'port': 12345}
        row = self.register('listener', valid)
        self.assertEqual(row['identity']['pid'], 500)
        for invalid in ({**valid, 'host': '0.0.0.0'}, {**valid, 'port': True}, {**valid, 'port': 70000}):
            with self.assertRaises(ValueError):
                self.register('listener', invalid)

    def test_directory_bounds_and_output_retention_preserve_actual_files(self):
        temporary = Path(self.task['paths']['temp']) / 'synthetic-cache'
        temporary.mkdir()
        child = temporary / 'keep.txt'
        child.write_text('preserved', encoding='utf-8')
        row = self.register('temp_directory', {'path': str(temporary)})
        self.cleanup(row)
        self.assertEqual(child.read_text(encoding='utf-8'), 'preserved')
        output = Path(self.task['paths']['outputs']) / 'final.txt'
        output.write_text('delivery', encoding='utf-8')
        retained = self.register('output', {'path': str(output)}, temporary=False)
        self.assertFalse(retained['cleanup_eligible'])
        self.assertEqual(retained['state'], 'manual_required')
        with self.assertRaises(ValueError):
            self.register('output', {'path': str(output)})
        other, _ = self.new_task()
        for path in (self.task['paths']['temp'], other['paths']['temp'], str(temporary / '..' / 'elsewhere'), str(self.base)):
            with self.assertRaises(ValueError):
                self.register('temp_directory', {'path': path})

    def test_directory_reparse_and_hardlinked_output_are_rejected(self):
        directory = Path(self.task['paths']['temp']) / 'candidate'
        directory.mkdir()
        info = directory.lstat()
        original = config._is_reparse
        def synthetic_reparse(st):
            return (st.st_ino == info.st_ino and st.st_dev == info.st_dev) or original(st)
        with patch.object(config, '_is_reparse', side_effect=synthetic_reparse):
            with self.assertRaises(ValueError):
                self.register('temp_directory', {'path': str(directory)})
        output = Path(self.task['paths']['outputs']) / 'one.txt'
        output.write_text('retained', encoding='utf-8')
        os.link(output, output.with_name('two.txt'))
        with self.assertRaises(ValueError):
            self.register('output', {'path': str(output)}, temporary=False)

    def test_closed_needs_matching_absence_evidence_and_retry_is_idempotent(self):
        row = self.register()
        for outcome in ('present', 'not_checked'):
            with self.assertRaises(ValueError):
                self.cleanup(row, outcome=outcome)
        with self.assertRaises(ValueError):
            self.cleanup(row, evidence={})
        closed = self.cleanup(row)
        again = self.cleanup(row, evidence=closed['evidence'])
        self.assertTrue(again['deduplicated'])
        self.assertEqual(self.call('resource_list')['counts'], {'closed': 1})
        with self.assertRaises(ValueError):
            self.cleanup(row, state='cleanup_failed', outcome='present')

    def test_failure_manual_and_stale_evidence_preserve_status(self):
        row = self.register()
        failed = self.cleanup(row, state='cleanup_failed', outcome='present')
        with self.assertRaises(ValueError):
            self.cleanup(row, state='cleanup_pending', evidence=failed['evidence'])
        older = dict(failed['evidence'], observed_at='2000-01-01T00:00:00Z')
        with self.assertRaises(ValueError):
            self.cleanup(row, state='manual_required', evidence=older)
        manual = self.cleanup(row, state='manual_required', outcome='not_checked')
        self.assertEqual(manual['state'], 'manual_required')
        self.assertEqual(manual['last_evidence_at'], manual['evidence']['observed_at'])

    def test_sampling_is_bounded_and_timestamped_without_process_control(self):
        sample = {'sampled_at': '2026-10-01T00:00:00Z', 'ram_available_bytes': 1024,
                  'vram_available_bytes': 2048}
        row = self.register(sample=sample)
        self.assertEqual(row['sample']['ram_available_bytes'], 1024)
        for index, invalid in enumerate(({'ram_available_bytes': 10}, {**sample, 'ram_available_bytes': -1},
                                         {**sample, 'ram_available_bytes': float('nan')}, {**sample, 'ram_available_bytes': True},
                                         {**sample, 'ram_available_bytes': 10 ** 500}, {**sample, 'command': 'never execute'})):
            with self.assertRaises(ValueError):
                self.register(identity={'browser_id': 't', 'session_id': 's', 'tab_id': str(index)}, sample=invalid)

    def test_resource_schema_backup_includes_committed_wal_and_no_credentials(self):
        path = self.data / 'collaboration.sqlite3'
        with closing(sqlite3.connect(path)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute("INSERT INTO audit(root,action,object_id,actor,created_at,detail) VALUES(?,?,?,?,?,?)",
                           (config._key(self.root), 'wal_fixture', 'fixture', 'synthetic', core._now(), 'committed WAL marker'))
            writer.commit()
            self.register()
            backups = list((self.data / 'collaboration-schema-backups').glob('resources-*.sqlite3'))
            self.assertEqual(len(backups), 1)
            with closing(sqlite3.connect(backups[0])) as backup:
                self.assertEqual(backup.execute("SELECT detail FROM audit WHERE action='wal_fixture'").fetchone()[0], 'committed WAL marker')
                self.assertFalse(backup.execute("SELECT 1 FROM sqlite_master WHERE name='task_resources'").fetchone())
        with core.store(self.cfg) as (con, _):
            text = json.dumps(dict(con.execute('SELECT * FROM task_resources').fetchone()))
            self.assertNotIn(self.lease['lease_token'], text)

    def test_unknown_schema_fails_closed_and_is_not_recreated(self):
        with core.store(self.cfg) as (con, _):
            con.execute('CREATE TABLE task_resources(id TEXT PRIMARY KEY, legacy_data TEXT)')
            con.execute("INSERT INTO task_resources VALUES('legacy','preserve')")
        with self.assertRaisesRegex(ValueError, '版本不兼容'):
            self.register()
        with core.store(self.cfg) as (con, _):
            self.assertEqual(con.execute('SELECT legacy_data FROM task_resources').fetchone()[0], 'preserve')


if __name__ == '__main__':
    unittest.main()
