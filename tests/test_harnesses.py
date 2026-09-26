"""Isolated registry and bounded discovery tests; never inspect native client data."""
import copy
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from aihub import config, collaboration, harnesses, tool_adapters


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='aihub-harnesses-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / 'Workspace'
        self.root.mkdir()
        self.other_root = self.base / 'OtherWorkspace'
        self.other_root.mkdir()
        self.data = self.base / 'Application' / 'data'
        self.cfg = {'workspace_managed': True, 'ai_root': str(self.root)}
        self.other = {'workspace_managed': True, 'ai_root': str(self.other_root)}
        for target, attribute, value in ((config, 'DATA_DIR', str(self.data)),
                                        (config, 'APP_DIR', str(self.data.parent))):
            patch = mock.patch.object(target, attribute, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(tool_adapters, '_candidates', return_value=[])
        patch.start()
        self.addCleanup(patch.stop)

    def body(self, identifier='custom-ai', **updates):
        return dict({'id': identifier, 'name': 'Custom AI', 'revision': 0,
                     'enabled': True, 'connection_mode': 'mcp_stdio', 'notes': ''}, **updates)

    def test_listing_unconfigured_and_managed_roots_never_creates_storage(self):
        for cfg in ({}, {'ai_root': str(self.root)}, self.cfg):
            rows = harnesses.list_tools(cfg)
            self.assertEqual(rows, [])
            self.assertEqual(harnesses.allowed_ids(cfg), set())
        self.assertFalse(self.data.exists())
        with self.assertRaises(ValueError):
            harnesses.save({}, self.body())
        self.assertFalse(self.data.exists())

    def seed_history(self, clients=(), targets=()):
        self.data.mkdir(parents=True, exist_ok=True)
        path = self.data / 'collaboration.sqlite3'
        with contextlib.closing(sqlite3.connect(path)) as connection:
            connection.execute('CREATE TABLE clients(root TEXT, tool TEXT)')
            connection.execute('CREATE TABLE tasks(root TEXT, target_tool TEXT)')
            connection.executemany('INSERT INTO clients VALUES(?,?)', clients)
            connection.executemany('INSERT INTO tasks VALUES(?,?)', targets)
            connection.commit()
        return path

    def test_templates_are_optional_pure_recipes_without_installation_claims(self):
        with mock.patch.object(tool_adapters, '_candidates', side_effect=AssertionError('no probing')):
            recipes = harnesses.templates(self.cfg)
        self.assertEqual([row['id'] for row in recipes], ['codex', 'zcode', 'dsh', 'workbuddy', 'qoder'])
        for row in recipes:
            self.assertTrue(row['template'])
            self.assertEqual(row['registration_origin'], 'template')
            self.assertFalse({'enabled', 'configured', 'registered', 'detected', 'available', 'online', 'executable'} & set(row))
        self.assertFalse(self.data.exists())

    def test_unregistered_template_heartbeat_is_rejected_then_explicit_registration_works(self):
        payload = {'client_id': 'fixture', 'tool': 'codex', 'name': 'fixture', 'protocol_version': 1}
        with self.assertRaises(ValueError):
            collaboration.execute(self.cfg, 'client_heartbeat', payload)
        self.assertEqual(harnesses.allowed_ids(self.cfg), set())
        harnesses.save(self.cfg, self.body('codex'))
        reply = collaboration.execute(self.cfg, 'client_heartbeat', payload)
        self.assertEqual(reply['tool'], 'codex')
        self.assertEqual(harnesses.get(self.cfg, 'codex')['registration_origin'], 'explicit')

    def test_legacy_usage_is_root_scoped_readonly_and_explicit_disable_wins(self):
        root, other = config._key(self.root), config._key(self.other_root)
        database = self.seed_history(clients=[(root, 'codex'), (other, 'workbuddy'), (root, 'custom-ai')],
                                     targets=[(root, 'dsh'), (root, 'any'), (other, 'zcode')])
        before = database.read_bytes()
        rows = harnesses.list_tools(self.cfg)
        self.assertEqual({row['id'] for row in rows}, {'codex', 'dsh'})
        self.assertTrue(all(row['registration_origin'] == 'legacy_usage' and row['revision'] == 0
                            and not row['configured'] and not row['template'] for row in rows))
        self.assertEqual(harnesses.allowed_ids(self.other), {'workbuddy', 'zcode'})
        self.assertFalse((self.data / 'harnesses.sqlite3').exists())
        self.assertEqual(database.read_bytes(), before)
        disabled = harnesses.save(self.cfg, {'id': 'codex', 'revision': 0, 'enabled': False})
        self.assertEqual(disabled['revision'], 1)
        self.assertEqual(disabled['registration_origin'], 'explicit')
        self.assertEqual(harnesses.allowed_ids(self.cfg), {'dsh'})
        self.assertEqual(database.read_bytes(), before)
        self.assertIn('codex', harnesses.allowed_ids(self.cfg, include_disabled=True))
        harnesses.save(self.cfg, {'id': 'codex', 'revision': 1, 'enabled': True})
        self.assertIn('codex', harnesses.allowed_ids(self.cfg))

    def test_legacy_native_bridge_client_can_resume_without_registry_migration(self):
        with collaboration.store(self.cfg) as (connection, root):
            connection.execute('INSERT INTO clients VALUES(?,?,?,?,?,?)',
                               ('old-client', root, 'codex', 'Old client', collaboration._now(), 1))
        self.assertFalse((self.data / 'harnesses.sqlite3').exists())
        reply = collaboration.execute(self.cfg, 'client_heartbeat', {
            'client_id': 'old-client', 'tool': 'codex', 'name': 'Old client', 'protocol_version': 1})
        self.assertEqual(reply['tool'], 'codex')
        self.assertEqual(harnesses.get(self.cfg, 'codex')['registration_origin'], 'legacy_usage')
        self.assertFalse((self.data / 'harnesses.sqlite3').exists())
        harnesses.save(self.cfg, {'id': 'codex', 'revision': 0, 'enabled': False})
        with self.assertRaises(ValueError):
            collaboration.execute(self.cfg, 'client_heartbeat', {
                'client_id': 'old-client', 'tool': 'codex', 'name': 'Old client', 'protocol_version': 1})

    def test_actual_legacy_entries_count_toward_limit_but_can_be_made_explicit(self):
        self.seed_history(clients=[(config._key(self.root), 'codex')])
        with harnesses._write_store(self.cfg) as (connection, root):
            for number in range(127):
                identifier = 'custom-%s' % number
                connection.execute('INSERT INTO harnesses VALUES(?,?,?,?)',
                                   (root, identifier, 1, json.dumps(harnesses._defaults(identifier, identifier))))
        with self.assertRaises(ValueError):
            harnesses.save(self.cfg, self.body('overflow'))
        explicit = harnesses.save(self.cfg, {'id': 'codex', 'revision': 0})
        self.assertEqual(explicit['revision'], 1)
        self.assertEqual(explicit['registration_origin'], 'explicit')
        self.assertEqual(len(harnesses.list_tools(self.cfg)), 128)

    def test_legacy_database_and_sidecar_links_are_rejected(self):
        database = self.seed_history()
        original = self.base / 'must-keep.txt'
        original.write_bytes(b'untouched')
        for suffix in ('-wal', '-shm', '-journal'):
            target = Path(str(database) + suffix)
            os.link(original, target)
            try:
                with self.assertRaises(ValueError):
                    harnesses.allowed_ids(self.cfg)
                self.assertEqual(original.read_bytes(), b'untouched')
            finally:
                target.unlink()

    def test_qoder_recipe_requires_registration_and_uses_generic_handoff(self):
        with self.assertRaises(ValueError):
            tool_adapters.project_rules('qoder', self.root, self.cfg)
        row = harnesses.save(self.cfg, {'id': 'qoder', 'revision': 0, 'connection_mode': 'mcp_stdio'})
        self.assertEqual(row['name'], 'Qoder')
        self.assertFalse(row['template'])
        proposal = tool_adapters.project_rules('qoder', self.root, self.cfg)
        self.assertEqual([item[0] for item in proposal], ['AIHUB_HANDOFF_qoder.md'])

    def test_discovery_wrapper_marks_only_real_registrations_and_keeps_source_evidence(self):
        from aihub import harness_discovery
        harnesses.save(self.cfg, self.body('dsh', enabled=False))
        result = {'items': [{'id': 'codex', 'suggested_id': 'codex'}, {'id': 'dsh', 'suggested_id': 'dsh'}],
                  'sources': {'path': {'checked': 2}}, 'truncated': False}
        with mock.patch.object(harness_discovery, 'discover', return_value=result) as discover:
            reply = harnesses.discover(self.cfg)
        discover.assert_called_once_with(self.cfg, max_checks=harnesses.MAX_DISCOVERY_CHECKS,
                                         max_path_dirs=harnesses.MAX_PATH_DIRS)
        self.assertEqual([item['registered'] for item in reply['items']], [False, True])
        self.assertEqual(reply['sources'], result['sources'])

    def test_registration_is_workspace_scoped_and_persistent_without_native_writes(self):
        executable = self.base / 'tool.exe'
        executable.write_bytes(b'fixture executable metadata only')
        config_path = self.base / 'native-private.json'
        config_path.write_bytes(b'unchanged private bytes')
        row = harnesses.save(self.cfg, self.body(executable=str(executable), work_dir=str(self.root),
                                                config_path=str(config_path), notes='Only metadata'))
        self.assertTrue(row['configured'])
        self.assertFalse(row['builtin'])
        self.assertTrue(row['detected'])
        self.assertTrue(row['available'])
        self.assertEqual(row['revision'], 1)
        self.assertEqual(row['user_notes'], 'Only metadata')
        self.assertEqual(row['path_status'], {'executable': 'file', 'work_dir': 'directory', 'config_path': 'file'})
        self.assertEqual(harnesses.get(self.cfg, row['id'])['evidence_key'], row['evidence_key'])
        self.assertNotIn(row['id'], harnesses.allowed_ids(self.other))
        self.assertNotIn(row['id'], harnesses.allowed_ids({}))
        other_row = harnesses.save(self.other, self.body(name='Different workspace'))
        self.assertEqual(other_row['revision'], 1)
        self.assertEqual(harnesses.get(self.cfg, row['id'])['name'], 'Custom AI')
        self.assertNotEqual(other_row['evidence_key'], row['evidence_key'])
        self.assertEqual(config_path.read_bytes(), b'unchanged private bytes')

    def test_disabled_history_and_reenable_require_current_revision(self):
        row = harnesses.save(self.cfg, self.body())
        with self.assertRaises(harnesses.RevisionConflict):
            harnesses.save(self.cfg, self.body(name='duplicate cannot overwrite'))
        disabled = harnesses.save(self.cfg, {'id': row['id'], 'revision': row['revision'], 'enabled': False})
        self.assertEqual(disabled['revision'], 2)
        self.assertFalse(disabled['enabled'])
        self.assertNotIn(row['id'], harnesses.allowed_ids(self.cfg))
        self.assertIn(row['id'], harnesses.allowed_ids(self.cfg, include_disabled=True))
        self.assertNotEqual(row['evidence_key'], disabled['evidence_key'])
        with self.assertRaises(harnesses.RevisionConflict):
            harnesses.save(self.cfg, {'id': row['id'], 'revision': 1, 'enabled': True})
        enabled = harnesses.save(self.cfg, {'id': row['id'], 'revision': 2, 'enabled': True})
        self.assertEqual(enabled['revision'], 3)
        self.assertIn(row['id'], harnesses.allowed_ids(self.cfg))

    def test_optimistic_revision_allows_only_one_concurrent_save(self):
        row = harnesses.save(self.cfg, self.body())
        ready = threading.Barrier(3)
        results = []
        def update(name):
            ready.wait()
            try:
                results.append(harnesses.save(self.cfg, {'id': row['id'], 'revision': 1, 'name': name}))
            except harnesses.RevisionConflict:
                results.append('conflict')
        threads = [threading.Thread(target=update, args=(name,)) for name in ('First', 'Second')]
        for worker in threads:
            worker.start()
        ready.wait()
        for worker in threads:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(results.count('conflict'), 1)
        self.assertEqual(harnesses.get(self.cfg, row['id'])['revision'], 2)

    def test_builtin_template_overrides_do_not_change_native_rule_support(self):
        row = harnesses.save(self.cfg, self.body('codex', name='My Codex', enabled=False))
        self.assertTrue(row['builtin'])
        self.assertTrue(row['configured'])
        self.assertEqual(row['rules_support'], 'AGENTS.md')
        self.assertNotIn('codex', harnesses.allowed_ids(self.cfg))
        self.assertNotIn('codex', harnesses.allowed_ids(self.other))
        with self.assertRaises(ValueError):
            tool_adapters.project_rules('codex', self.root, self.cfg)

    def test_rejects_invalid_ids_unknown_fields_types_and_secret_values(self):
        invalid_ids = ['any', '', 'New', '123', '../tool', 'my.tool', 'a' * 65, 'con', 'aux', 'my tool', 'tool\n']
        for identifier in invalid_ids:
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                harnesses.save(self.cfg, self.body(identifier))
        for extra in ({'env': {}}, {'token': 'secret'}, {'command': 'run me'}, {'args': []}, {'builtin': False}):
            with self.subTest(field=list(extra)), self.assertRaises(ValueError):
                harnesses.save(self.cfg, {**self.body(), **extra})
        for updates in ({'enabled': 1}, {'revision': True}, {'revision': -1}, {'connection_mode': 'shell'},
                        {'name': ''}, {'name': 'break\nline'}, {'notes': 'bad\ttab'},
                        {'notes': 'Bearer synthetic-credential-example-123456789'}, {'notes': {'nested': 'not text'}}):
            with self.subTest(fields=list(updates)), self.assertRaises(ValueError):
                harnesses.save(self.cfg, self.body(**updates))
        self.assertEqual(len(harnesses.list_tools(self.cfg)), 0)

    def test_missing_paths_are_metadata_and_invalid_paths_are_rejected(self):
        missing = self.base / 'not-installed' / 'tool.exe'
        row = harnesses.save(self.cfg, self.body(executable=str(missing), work_dir=str(missing.parent),
                                                config_path=str(missing.parent / 'config.json')))
        self.assertFalse(row['detected'])
        self.assertEqual(set(row['path_status'].values()), {'missing'})
        self.assertFalse(missing.parent.exists())
        for path in ('relative/tool.exe', 'https://example.invalid/run', str(self.root) + '\nsecond'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                harnesses.save(self.cfg, self.body('other-ai', executable=path))
        with self.assertRaises(ValueError):
            harnesses.save(self.cfg, self.body('other-ai', executable=str(self.root)))
        regular = self.base / 'ordinary.txt'
        regular.write_text('metadata')
        with self.assertRaises(ValueError):
            harnesses.save(self.cfg, self.body('other-ai', work_dir=str(regular)))

    def test_database_and_sidecar_hardlinks_are_rejected_without_touching_original(self):
        self.data.mkdir(parents=True)
        original = self.base / 'must-keep.txt'
        original.write_bytes(b'untouched')
        for suffix in ('', '-wal', '-shm', '-journal'):
            target = self.data / ('harnesses.sqlite3' + suffix)
            os.link(original, target)
            try:
                with self.assertRaises(ValueError):
                    harnesses.save(self.cfg, self.body())
                with self.assertRaises(ValueError):
                    harnesses.list_tools(self.cfg)
                self.assertEqual(original.read_bytes(), b'untouched')
            finally:
                target.unlink()

    def test_link_metadata_and_link_database_parent_are_rejected(self):
        original = self.base / 'actual'
        original.mkdir()
        link = self.base / 'redirect'
        try:
            link.symlink_to(original, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symbolic link privilege unavailable')
        with self.assertRaises(ValueError):
            harnesses.save(self.cfg, self.body(work_dir=str(link)))
        with mock.patch.object(config, 'DATA_DIR', str(link / 'private')):
            with self.assertRaises(ValueError):
                harnesses.save(self.cfg, self.body())
        self.assertFalse((original / 'private').exists())

    def test_root_and_entry_identity_changes_invalidate_evidence_without_editing_metadata(self):
        executable = self.base / 'tool.exe'
        executable.write_bytes(b'old')
        row = harnesses.save(self.cfg, self.body(executable=str(executable)))
        executable.write_bytes(b'changed size')
        changed = harnesses.get(self.cfg, row['id'])
        self.assertEqual(changed['revision'], row['revision'])
        self.assertNotEqual(changed['evidence_key'], row['evidence_key'])
        moved = self.base / 'PreviousWorkspace'
        self.root.rename(moved)
        self.root.mkdir()
        replaced = harnesses.get(self.cfg, row['id'])
        self.assertNotEqual(replaced['evidence_key'], changed['evidence_key'])

    def test_read_operations_preserve_registry_bytes_and_native_files(self):
        native = self.base / 'credentials.json'
        native.write_text('unread')
        row = harnesses.save(self.cfg, self.body(config_path=str(native)))
        database = self.data / 'harnesses.sqlite3'
        before = database.read_bytes()
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('must not read files')), \
                mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('must not read files')):
            result = harnesses.get(self.cfg, row['id'])
        self.assertEqual(result['id'], row['id'])
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(native.read_text(), 'unread')

    def test_limit_counts_actual_registrations_not_templates_and_preserves_history(self):
        with harnesses._write_store(self.cfg) as (connection, root):
            for number in range(128):
                identifier = 'custom-%s' % number
                connection.execute('INSERT INTO harnesses VALUES(?,?,?,?)',
                                   (root, identifier, 1, json.dumps(harnesses._defaults(identifier, identifier))))
        self.assertEqual(len(harnesses.list_tools(self.cfg)), 128)
        with self.assertRaises(ValueError):
            harnesses.save(self.cfg, self.body('overflow'))
        self.assertEqual(harnesses.save(self.cfg, {'id': 'custom-1', 'revision': 1, 'enabled': False})['revision'], 2)
        self.assertEqual(len(harnesses.list_tools(self.cfg)), 128)
        self.assertEqual(harnesses.save(self.other, self.body())['revision'], 1)

    def test_custom_project_rules_only_create_aihub_specific_proposal(self):
        harnesses.save(self.cfg, self.body(name='Another Client'))
        project = self.root / 'future-project'
        proposal = tool_adapters.project_rules('custom-ai', project, self.cfg)
        self.assertEqual([entry[0] for entry in proposal], ['AIHUB_HANDOFF_custom-ai.md'])
        self.assertIn('不代表工具原生支持', proposal[0][1])
        self.assertFalse(project.exists())
        with self.assertRaises(ValueError):
            tool_adapters.project_rules('unregistered', project, self.cfg)

    def test_discovery_is_bounded_metadata_only_and_never_registers(self):
        path = self.base / 'bin'
        path.mkdir()
        executable = path / ('claude.exe' if os.name == 'nt' else 'claude')
        executable.write_bytes(b'fixture only, never execute')
        unknown = path / ('unknown.exe' if os.name == 'nt' else 'unknown')
        unknown.write_bytes(b'unknown metadata')
        original = copy.deepcopy(self.cfg)
        with mock.patch.dict(os.environ, {'PATH': str(path), 'APPDATA': str(self.base / 'Roaming'),
                                         'LOCALAPPDATA': str(self.base / 'Local')}), \
                mock.patch.object(Path, 'home', return_value=self.base / 'SyntheticHome'), \
                mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('read')), \
                mock.patch.object(Path, 'read_text', side_effect=AssertionError('read')):
            result = harnesses.discover(self.cfg)
        candidate = next(item for item in result['items'] if item['suggested_id'] == 'claude')
        self.assertEqual(candidate['executable'], str(executable))
        self.assertFalse(candidate['registered'])
        self.assertFalse(candidate['verified'])
        self.assertNotIn('unknown', {item['id'] for item in result['items']})
        self.assertLessEqual(result['checks'], harnesses.MAX_DISCOVERY_CHECKS)
        self.assertFalse(result['scan_scope']['registers_automatically'])
        self.assertEqual(self.cfg, original)
        self.assertFalse(self.data.exists())

    def test_discovery_limit_and_custom_id_validation(self):
        with mock.patch.dict(os.environ, {'PATH': os.pathsep.join(str(self.base / ('path%d' % number)) for number in range(80))}), \
                mock.patch.object(harnesses, 'MAX_DISCOVERY_CHECKS', 20):
            result = harnesses.discover({})
        self.assertTrue(result['truncated'])
        self.assertEqual(result['checks'], 20)
        self.assertFalse(self.data.exists())
        self.assertEqual(harnesses.validate_tool_id('a' * 64), 'a' * 64)


if __name__ == '__main__':
    unittest.main()
