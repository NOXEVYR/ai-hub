"""Project creation uses synthetic roots only; never touch real user assets."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aihub import harnesses, config, projects, tool_adapters, workspace


class ProjectCreation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihub-project-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'AI'
        self.root.mkdir()
        self.data = self.base / 'Application' / 'data'
        self.data.mkdir(parents=True)
        for key, value in [('APP_DIR', self.data.parent), ('DATA_DIR', self.data),
                           ('CONFIG_PATH', self.data / 'config.json')]:
            patcher = mock.patch.object(config, key, str(value))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = {'workspace_managed': True, 'ai_root': str(self.root), 'output_roots': [],
                    'network': {'synthetic': 'keep'}, 'notes': 'preserve'}
        # This fixture explicitly opts into the four compatibility recipes.
        for identifier in harnesses.BUILTIN_IDS:
            harnesses.save(self.cfg, {'id': identifier, 'revision': 0, 'connection_mode': 'mcp_stdio'})
        config.save_config(self.cfg)
        projects._previews.clear()
        self.body = {'name': '示例项目', 'tools': ['codex', 'zcode', 'dsh', 'workbuddy']}

    def preview(self, **changes):
        return projects.preview(self.cfg, {**self.body, **changes})

    def apply(self):
        result = self.preview()
        self.assertTrue(result['can_apply'], result['errors'])
        return projects.apply(self.cfg, result['token'])

    def test_preview_read_only_and_create_outputs_with_soft_rules(self):
        before = Path(config.CONFIG_PATH).read_bytes()
        plan = self.preview()
        self.assertTrue(plan['can_apply'], plan['errors'])
        self.assertFalse((self.root / '40_Projects').exists())
        self.assertEqual(Path(config.CONFIG_PATH).read_bytes(), before)
        result = projects.apply(self.cfg, plan['token'])
        root = Path(result['root'])
        for name in ('Inputs', 'Work', 'Outputs', 'Deliverables'):
            self.assertTrue((root / name).is_dir())
        manifest = json.loads((root / '.aihub-project.json').read_text(encoding='utf-8'))
        self.assertEqual(set(manifest['tools']), set(self.body['tools']))
        self.assertTrue(all(v['mode'] == 'guidance_only' for v in manifest['tools'].values()))
        self.assertIn('Inputs 为只读', (root / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertIn('WorkBuddy', Path(result['prompt_path']).read_text(encoding='utf-8'))
        self.assertIn('--sandbox workspace-write', Path(result['prompt_path']).read_text(encoding='utf-8'))
        self.assertIn('TASK_BRIEF.md', (root / 'CODEBUDDY.md').read_text(encoding='utf-8'))
        self.assertFalse(list(root.glob('TOOL_HANDOFF*')))
        self.assertEqual(self.cfg['output_roots'], [str(root / 'Outputs')])
        self.assertEqual(self.cfg['network'], {'synthetic': 'keep'})
        self.assertEqual(Path(result['config_backup']).read_bytes(), before)
        self.assertEqual(json.loads(Path(config.CONFIG_PATH).read_text(encoding='utf-8')), self.cfg)
        self.assertTrue(result['output_root_added'])
        with self.assertRaises(ValueError):
            projects.apply(self.cfg, plan['token'])

    def test_invalid_names_rejected_without_write(self):
        for name in ['', '.', '..', '../escape', 'a/b', 'a\\b', 'a:b', 'CON', 'nul.txt',
                     'COM1', 'LPT9.jpg', 'COM¹', 'CONOUT$', 'a.', 'a ', ' a', 'a\n',
                     'a\x00b', 'a\u202eb', '*bad', '`bad', 'x' * 81, None, 4]:
            with self.subTest(name=name):
                result = self.preview(name=name)
                self.assertFalse(result['can_apply'])
                self.assertIsNone(result['token'])
        self.assertFalse((self.root / '40_Projects').exists())

    def test_invalid_tools_and_unmanaged_root(self):
        for selected in [[], ['other'], ['codex', 'codex'], 'codex', [None]]:
            self.assertFalse(self.preview(tools=selected)['can_apply'])
        self.cfg['workspace_managed'] = False
        self.assertFalse(self.preview()['can_apply'])

    def test_output_exclusions_do_not_create_an_invisible_project(self):
        for name in ['.private', 'Datasets', 'node_modules']:
            self.assertFalse(self.preview(name=name)['can_apply'])
        self.cfg['scan_exclude_paths'] = [str(self.root / '40_Projects')]
        self.assertFalse(self.preview()['can_apply'])

    def test_only_selected_tool_rules_are_created(self):
        plan = self.preview(tools=['codex'])
        result = projects.apply(self.cfg, plan['token'])
        self.assertFalse((Path(result['root']) / 'CODEBUDDY.md').exists())
        brief = Path(result['prompt_path']).read_text(encoding='utf-8')
        self.assertIn('Codex 项目接入', brief)
        self.assertNotIn('WorkBuddy 项目接入', brief)

    def test_adapter_path_and_conflict_fail_without_write(self):
        with mock.patch.object(tool_adapters, 'project_rules', return_value=[('../outside.md', 'bad')]):
            self.assertFalse(self.preview()['can_apply'])
        def conflict(key, root):
            return [('CODEBUDDY.md', key)]
        with mock.patch.object(tool_adapters, 'project_rules', side_effect=conflict):
            self.assertFalse(self.preview()['can_apply'])
        self.assertFalse((self.root / '40_Projects').exists())

    def test_rule_change_invalidates_preview(self):
        plan = self.preview()
        with mock.patch.object(tool_adapters, 'project_rules', return_value=[('CODEBUDDY.md', 'new rules')]):
            with self.assertRaises(ValueError):
                projects.apply(self.cfg, plan['token'])
        self.assertFalse(Path(plan['root']).exists())

    def test_existing_project_and_contents_preserved(self):
        project = self.root / '40_Projects' / self.body['name']
        project.mkdir(parents=True)
        original = project / 'AGENTS.md'
        original.write_bytes(b'Original instructions')
        self.assertFalse(self.preview()['can_apply'])
        self.assertEqual(original.read_bytes(), b'Original instructions')
        self.assertEqual(list(project.iterdir()), [original])

    def test_existing_source_list_preserved_without_duplicate(self):
        output = str(self.root / '40_Projects' / self.body['name'] / 'Outputs')
        self.cfg['output_roots'] = [str(self.root / '70_Output'), output]
        config.save_config(self.cfg)
        result = self.apply()
        self.assertFalse(result['output_root_added'])
        self.assertEqual(self.cfg['output_roots'], [str(self.root / '70_Output'), output])

    def test_atomic_config_replace_failure_preserves_original(self):
        before = Path(config.CONFIG_PATH).read_bytes()
        plan = self.preview()
        with mock.patch.object(config.os, 'replace', side_effect=PermissionError('synthetic sharing violation')):
            with self.assertRaises(PermissionError):
                projects.apply(self.cfg, plan['token'])
        self.assertEqual(Path(config.CONFIG_PATH).read_bytes(), before)
        self.assertEqual(self.cfg['output_roots'], [])
        self.assertFalse(Path(plan['root']).exists())
        self.assertEqual(list(self.data.glob('.config-*.tmp')), [])

    def test_partial_project_write_failure_rolls_back(self):
        plan = self.preview()
        original_fsync = os.fsync
        calls = []
        def fail_on_project(fd):
            calls.append(fd)
            if len(calls) == 2:  # First fsync writes the retained config backup.
                raise OSError('synthetic project write failure')
            return original_fsync(fd)
        with mock.patch.object(projects.os, 'fsync', side_effect=fail_on_project):
            with self.assertRaises(OSError):
                projects.apply(self.cfg, plan['token'])
        self.assertFalse(Path(plan['root']).exists())
        self.assertEqual(self.cfg['output_roots'], [])

    def test_config_in_memory_and_disk_revision_invalidate(self):
        plan = self.preview()
        self.cfg['new_setting'] = True
        with self.assertRaisesRegex(ValueError, '配置'):
            projects.apply(self.cfg, plan['token'])
        self.cfg.pop('new_setting')
        plan = self.preview()
        Path(config.CONFIG_PATH).write_bytes(Path(config.CONFIG_PATH).read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, '配置'):
            projects.apply(self.cfg, plan['token'])
        self.assertFalse((self.root / '40_Projects').exists())

    def test_root_identity_change_invalidates_preview(self):
        plan = self.preview()
        self.root.rename(self.base / 'Previous')
        self.root.mkdir()
        with self.assertRaisesRegex(ValueError, '变化'):
            projects.apply(self.cfg, plan['token'])
        self.assertFalse((self.root / '40_Projects').exists())

    def test_directory_created_after_preview_preserved(self):
        plan = self.preview()
        project = Path(plan['root'])
        project.mkdir(parents=True)
        (project / 'existing.txt').write_bytes(b'keep')
        with self.assertRaises(ValueError):
            projects.apply(self.cfg, plan['token'])
        self.assertEqual((project / 'existing.txt').read_bytes(), b'keep')

    def test_expired_token(self):
        plan = self.preview()
        with mock.patch.object(projects.time, 'time', return_value=plan['expires_at'] + 1):
            with self.assertRaisesRegex(ValueError, '过期'):
                projects.apply(self.cfg, plan['token'])

    def test_config_failure_rolls_back_only_created_objects(self):
        parent = self.root / '40_Projects'
        parent.mkdir()
        preserved = parent / 'existing.txt'
        preserved.write_bytes(b'keep')
        cfg_before = copy.deepcopy(self.cfg)
        disk_before = Path(config.CONFIG_PATH).read_bytes()
        plan = self.preview()
        with mock.patch.object(config, 'save_config', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                projects.apply(self.cfg, plan['token'])
        self.assertEqual(self.cfg, cfg_before)
        self.assertEqual(Path(config.CONFIG_PATH).read_bytes(), disk_before)
        self.assertFalse(Path(plan['root']).exists())
        self.assertEqual(preserved.read_bytes(), b'keep')
        self.assertFalse((self.data / '.workspace-apply.lock').exists())

    def test_config_failure_preserves_foreign_new_content(self):
        plan = self.preview()
        project = Path(plan['root'])
        def fail(_):
            (project / 'Work' / 'foreign.txt').write_bytes(b'other process')
            (project / 'AGENTS.md').write_bytes(b'concurrent edit')
            raise OSError('synthetic')
        with mock.patch.object(config, 'save_config', side_effect=fail):
            with self.assertRaises(OSError):
                projects.apply(self.cfg, plan['token'])
        self.assertEqual((project / 'Work' / 'foreign.txt').read_bytes(), b'other process')
        self.assertEqual((project / 'AGENTS.md').read_bytes(), b'concurrent edit')
        self.assertFalse((project / 'README.md').exists())

    def test_shared_lock_refuses_another_workspace_save(self):
        plan = self.preview()
        (self.data / '.workspace-apply.lock').write_bytes(b'other owner')
        with self.assertRaises(ValueError):
            projects.apply(self.cfg, plan['token'])
        self.assertEqual((self.data / '.workspace-apply.lock').read_bytes(), b'other owner')
        self.assertFalse(Path(plan['root']).exists())

    def test_lock_replaced_after_handle_close_is_preserved(self):
        plan = self.preview()
        lock = self.data / '.workspace-apply.lock'
        retained = self.data / 'original-lock'
        original_close = os.close
        replaced = []
        def replace_after_close(fd):
            original_close(fd)
            if not replaced and lock.exists():
                self.assertTrue(lock.absolute().is_relative_to(self.base.absolute()))
                self.assertTrue(retained.absolute().is_relative_to(self.base.absolute()))
                lock.rename(retained)
                lock.write_bytes(b'new process owns this lock')
                replaced.append(True)
        with mock.patch.object(projects.os, 'close', side_effect=replace_after_close):
            result = projects.apply(self.cfg, plan['token'])
        self.assertTrue(result['applied'])
        self.assertTrue(replaced)
        self.assertEqual(lock.read_bytes(), b'new process owns this lock')
        self.assertEqual(retained.read_bytes(), b'')

    def test_lock_cleanup_error_does_not_mask_apply_failure(self):
        original_file_state = workspace._file_state
        for cleanup_error in (OSError('cleanup denied'), ValueError('lock was replaced by a link')):
            with self.subTest(error=type(cleanup_error).__name__):
                plan = self.preview()
                lock = self.data / '.workspace-apply.lock'
                def fail_cleanup(path):
                    if Path(path) == lock:
                        raise cleanup_error
                    return original_file_state(path)
                with mock.patch.object(workspace, '_file_state', side_effect=fail_cleanup), \
                        mock.patch.object(config, 'save_config', side_effect=RuntimeError('original apply failure')):
                    with self.assertRaisesRegex(RuntimeError, 'original apply failure'):
                        projects.apply(self.cfg, plan['token'])
                self.assertFalse(Path(plan['root']).exists())
                self.assertEqual(lock.read_bytes(), b'')
                lock.unlink()

    def _junction(self, link, target):
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
        def cleanup():
            if os.path.lexists(link):
                self.assertTrue(link.absolute().is_relative_to(self.base.absolute()))
                self.assertTrue(config._is_reparse(link.lstat()))
                os.rmdir(link) if os.name == 'nt' else link.unlink()
        self.addCleanup(cleanup)

    def test_real_junction_parent_rejected_without_target_writes(self):
        target = self.base / 'Elsewhere'
        target.mkdir()
        self._junction(self.root / '40_Projects', target)
        self.assertFalse(self.preview()['can_apply'])
        self.assertEqual(list(target.iterdir()), [])

    def test_junction_swap_after_preview_rejected(self):
        plan = self.preview()
        target = self.base / 'Elsewhere'
        target.mkdir()
        self._junction(self.root / '40_Projects', target)
        with self.assertRaises(ValueError):
            projects.apply(self.cfg, plan['token'])
        self.assertEqual(list(target.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
