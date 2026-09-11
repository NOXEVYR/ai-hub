"""Workspace setup tests touch only generated synthetic directories/configuration."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from aihub import config, workspace


class WorkspaceSetup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihub-workspace-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.app = self.base / 'Application' / 'Hub'
        self.data = self.app / 'data'
        self.data.mkdir(parents=True)
        for key, value in [('APP_DIR', self.app), ('DATA_DIR', self.data),
                           ('CONFIG_PATH', self.data / 'config.json')]:
            patcher = mock.patch.object(config, key, str(value))
            patcher.start()
            self.addCleanup(patcher.stop)
        workspace._previews.clear()
        workspace._discovery_cache.clear()
        self.root = self.base / 'AI'
        self.cfg = {'ai_root': '', 'scan_roots': [], 'output_roots': [],
                    'ignore_dirs': sorted(config.DEFAULT_IGNORE_DIRS),
                    'network': {'synthetic_token': 'preserve'}, 'custom': {'keep': 7}}
        config.save_config(self.cfg)

    def preview(self, **fields):
        return workspace.preview(self.cfg, {'mode': 'create', 'root': str(self.root), **fields})

    def _junction(self, link, target):
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                    capture_output=True, text=True)
            if result.returncode:
                self.skipTest('Temporary junction unavailable')
        else:
            link.symlink_to(target, target_is_directory=True)
        def cleanup():
            if os.path.lexists(link):
                self.assertTrue(link.absolute().is_relative_to(self.base.absolute()))
                self.assertTrue(config._is_reparse(link.lstat()))
                os.rmdir(link) if os.name == 'nt' else link.unlink()
        self.addCleanup(cleanup)

    def test_preview_is_read_only_and_create_is_explicit(self):
        before = Path(config.CONFIG_PATH).read_bytes()
        result = self.preview()
        self.assertTrue(result['can_apply'], result['errors'])
        self.assertFalse(self.root.exists())
        self.assertEqual(Path(config.CONFIG_PATH).read_bytes(), before)
        self.assertEqual(result['sources']['scan_roots'], [str(self.root)])
        applied = workspace.apply(self.cfg, result['token'])
        self.assertTrue(applied['applied'])
        self.assertEqual(Path(applied['config_backup']).read_bytes(), before)
        self.assertEqual(self.cfg['network']['synthetic_token'], 'preserve')
        self.assertEqual(self.cfg['custom']['keep'], 7)
        for name in config.STANDARD_DIRS:
            self.assertTrue((self.root / name).is_dir())
        for name in ('WORKSPACE.md', 'workspace.json', 'Templates/PROJECT_TASK.md'):
            self.assertTrue((self.root / workspace.MANAGED / name).is_file())
        self.assertTrue((self.root / 'AGENTS.md').is_file())
        self.assertEqual(applied['status']['enforcement']['mode'], 'soft')
        self.assertTrue(applied['scan_required'])
        self.assertTrue(self.cfg['workspace_managed'])
        self.assertTrue(applied['status']['managed'])
        self.assertEqual(applied['status']['discovery']['examined'], 0)
        with self.assertRaises(ValueError):
            workspace.apply(self.cfg, result['token'])

    def test_connect_preserves_existing_user_rules_and_structure(self):
        self.root.mkdir()
        (self.root / 'AGENTS.md').write_bytes(b'User rules must remain byte-identical\n')
        original = self.root / 'legacy' / 'custom.txt'
        original.parent.mkdir()
        original.write_bytes(b'Existing asset')
        plan = self.preview(mode='connect')
        self.assertTrue(plan['can_apply'], plan['errors'])
        self.assertEqual(next(r for r in plan['files'] if r['kind'] == 'agents')['action'], 'keep')
        workspace.apply(self.cfg, plan['token'])
        self.assertEqual((self.root / 'AGENTS.md').read_bytes(), b'User rules must remain byte-identical\n')
        self.assertEqual(original.read_bytes(), b'Existing asset')
        repeat = self.preview(mode='connect')
        self.assertTrue(repeat['can_apply'], repeat['errors'])
        self.assertTrue(all(r['action'] == 'keep' for r in repeat['files']))

    def test_existing_manifest_conflict_is_not_overwritten(self):
        folder = self.root / workspace.MANAGED
        folder.mkdir(parents=True)
        existing = folder / 'workspace.json'
        existing.write_bytes(b'{"owner":"SomeoneElse"}')
        plan = self.preview(mode='connect')
        self.assertFalse(plan['can_apply'])
        self.assertIsNone(plan['token'])
        self.assertEqual(existing.read_bytes(), b'{"owner":"SomeoneElse"}')
        self.assertFalse((folder / 'WORKSPACE.md').exists())

    def test_existing_root_requires_connect(self):
        self.root.mkdir()
        self.assertFalse(self.preview()['can_apply'])
        self.assertTrue(self.preview(mode='connect')['can_apply'])

    def test_missing_connect_root_is_rejected_without_creation(self):
        self.assertFalse(self.preview(mode='connect')['can_apply'])
        self.assertFalse(self.root.exists())

    def test_token_expiry_and_config_edit_are_rejected(self):
        result = self.preview()
        workspace._previews[result['token']]['expires_at'] = 0
        with self.assertRaises(ValueError):
            workspace.apply(self.cfg, result['token'])
        result = self.preview()
        self.cfg['custom']['keep'] = 8
        with self.assertRaises(ValueError):
            workspace.apply(self.cfg, result['token'])
        self.assertFalse(self.root.exists())

    def test_external_config_change_and_concurrent_previews(self):
        plan = self.preview()
        other = copy.deepcopy(self.cfg)
        other['custom'] = {'keep': 10}
        config.save_config(other)
        with self.assertRaises(ValueError):
            workspace.apply(self.cfg, plan['token'])
        self.assertFalse(self.root.exists())
        self.cfg.update(other)
        one, two = self.preview(), self.preview()
        workspace.apply(self.cfg, one['token'])
        with self.assertRaises(ValueError):
            workspace.apply(self.cfg, two['token'])

    def test_new_file_appearing_after_preview_is_preserved(self):
        plan = self.preview()
        self.root.mkdir()
        (self.root / 'AGENTS.md').write_bytes(b'Concurrent user rules')
        with self.assertRaises(ValueError):
            workspace.apply(self.cfg, plan['token'])
        self.assertEqual((self.root / 'AGENTS.md').read_bytes(), b'Concurrent user rules')
        self.assertFalse((self.root / '20_Models').exists())

    def test_mutated_existing_agents_invalidates_token(self):
        self.root.mkdir()
        agents = self.root / 'AGENTS.md'
        agents.write_text('Before', encoding='utf-8')
        plan = self.preview(mode='connect')
        agents.write_text('After', encoding='utf-8')
        with self.assertRaises(ValueError):
            workspace.apply(self.cfg, plan['token'])
        self.assertEqual(agents.read_text(encoding='utf-8'), 'After')

    def test_external_sources_rejected_and_missing_preserved_in_health(self):
        self.root.mkdir()
        external = self.base / 'Outside'
        external.mkdir()
        plan = self.preview(mode='connect', scan_roots=[str(external)])
        self.assertFalse(plan['can_apply'])
        self.assertEqual(plan['source_health'][0]['status'], 'outside')
        missing = str(self.root / 'missing')
        self.cfg.update(ai_root=str(self.root), scan_roots=[missing], output_roots=[])
        status = workspace.status(self.cfg)
        self.assertEqual(status['sources']['scan_roots'], [missing])
        self.assertEqual(status['source_health'][0]['status'], 'missing')
        self.assertEqual(self.cfg['scan_roots'], [missing])

    def test_junction_suggests_canonical_but_requires_new_explicit_preview(self):
        canonical = self.root / '70_Output'
        canonical.mkdir(parents=True)
        old = self.root / 'AI_Output'
        self._junction(old, canonical)
        plan = self.preview(mode='connect', output_roots=[str(old)])
        self.assertFalse(plan['can_apply'])
        row = next(r for r in plan['source_health'] if r['kind'] == 'output')
        self.assertEqual(row['status'], 'junction')
        self.assertEqual(config._key(row['canonical_path']), config._key(canonical))
        self.assertTrue(row['canonical_supported'])
        valid = self.preview(mode='connect', output_roots=[str(canonical)])
        self.assertTrue(valid['can_apply'], valid['errors'])

    def test_junction_to_outside_is_not_supported(self):
        self.root.mkdir()
        external = self.base / 'ExternalOutput'
        external.mkdir()
        link = self.root / 'old_output'
        self._junction(link, external)
        plan = self.preview(mode='connect', output_roots=[str(link)])
        row = next(r for r in plan['source_health'] if r['kind'] == 'output')
        self.assertFalse(row['canonical_supported'])
        self.assertFalse(plan['can_apply'])

    def test_standard_partition_junction_is_not_replaced(self):
        self.root.mkdir()
        external = self.base / 'OtherModels'
        external.mkdir()
        link = self.root / '20_Models'
        self._junction(link, external)
        self.assertFalse(self.preview(mode='connect')['can_apply'])
        self.assertTrue(config._is_reparse(link.lstat()))

    def test_barbara_style_deep_verification_discovery_and_dataset_exclusion(self):
        found = self.root / '50_Training' / 'Projects' / 'Barbara' / 'Runs' / 'v1' / 'bakeoff' / 'variant' / 'verify_out'
        samples = self.root / '50_Training' / 'Projects' / 'Barbara' / 'Runs' / 'v2' / 'samples'
        bad = [self.root / '50_Training' / name / 'samples' for name in ('Datasets', 'DATASET', 'cache', 'CaChEs')]
        for folder in [found, samples, *bad]:
            folder.mkdir(parents=True)
        with mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('discovery must not read contents')):
            result = config.discover_output_roots([str(self.root)])
        self.assertEqual({row['path'] for row in result['items']}, {str(found), str(samples)})
        self.assertFalse(result['truncated'])
        # Discovery alone neither changes configured sources nor creates an index.
        self.assertEqual(self.cfg['output_roots'], [])

    def test_discovery_entry_budget_is_bounded(self):
        self.root.mkdir()
        for number in range(30):
            (self.root / str(number)).write_text('Synthetic', encoding='utf-8')
        result = config.discover_output_roots([str(self.root)], max_entries=10)
        self.assertTrue(result['truncated'])
        self.assertLessEqual(result['examined'], 10)

    def test_explicit_custom_gallery_allowed_but_dataset_and_cache_rejected(self):
        custom = self.root / '30_Assets' / 'ReviewPictures'
        custom.mkdir(parents=True)
        plan = self.preview(mode='connect', output_roots=[str(custom)])
        self.assertTrue(plan['can_apply'], plan['errors'])
        for name in ('Dataset', 'Datasets', 'cache', 'CACHE'):
            path = self.root / '50_Training' / name
            path.mkdir(parents=True, exist_ok=True)
            self.assertFalse(self.preview(mode='connect', output_roots=[str(path)])['can_apply'])

    def test_explicit_empty_sources_remain_empty(self):
        plan = self.preview(scan_roots=[], output_roots=[])
        self.assertTrue(plan['can_apply'], plan['errors'])
        workspace.apply(self.cfg, plan['token'])
        self.assertEqual(self.cfg['scan_roots'], [])
        self.assertEqual(self.cfg['output_roots'], [])

    def test_apply_failure_does_not_leave_generated_rules_or_touch_old_assets(self):
        self.root.mkdir()
        asset = self.root / 'existing.txt'
        asset.write_bytes(b'Keep')
        before = Path(config.CONFIG_PATH).read_bytes()
        plan = self.preview(mode='connect')
        with mock.patch.object(config, 'save_config', side_effect=OSError('Synthetic write failure')):
            with self.assertRaises(OSError):
                workspace.apply(self.cfg, plan['token'])
        self.assertEqual(asset.read_bytes(), b'Keep')
        self.assertEqual(Path(config.CONFIG_PATH).read_bytes(), before)
        self.assertFalse((self.root / 'AGENTS.md').exists())
        self.assertFalse((self.root / '20_Models').exists())
        self.assertEqual(len(list((self.data / 'workspace-backups').glob('*.json'))), 1)

    def test_existing_cross_process_lock_prevents_write(self):
        plan = self.preview()
        lock = self.data / '.workspace-apply.lock'
        lock.write_bytes(b'Other operation')
        with self.assertRaises(ValueError):
            workspace.apply(self.cfg, plan['token'])
        self.assertEqual(lock.read_bytes(), b'Other operation')
        self.assertFalse(self.root.exists())

    def test_rollback_preserves_empty_directory_replaced_during_apply(self):
        plan = self.preview()
        target = self.root / '20_Models'
        retained = self.base / 'retained-created-models'
        identities = []

        def replace_and_fail(_updated):
            identities.append(workspace._root_state(target)['identity'])
            target.rename(retained)
            target.mkdir()
            identities.append(workspace._root_state(target)['identity'])
            raise OSError('Original apply failure')

        with mock.patch.object(config, 'save_config', side_effect=replace_and_fail):
            with self.assertRaisesRegex(OSError, 'Original apply failure'):
                workspace.apply(self.cfg, plan['token'])
        self.assertNotEqual(*identities)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])
        self.assertTrue(retained.is_dir())
        self.assertFalse((self.data / '.workspace-apply.lock').exists())

    def test_rollback_rejects_replaced_ancestor_without_masking_error(self):
        plan = self.preview()
        moved = self.base / 'retained-workspace'

        def replace_and_fail(_updated):
            self.root.rename(moved)
            self._junction(self.root, moved)
            raise OSError('Original ancestor failure')

        with mock.patch.object(config, 'save_config', side_effect=replace_and_fail):
            with self.assertRaisesRegex(OSError, 'Original ancestor failure'):
                workspace.apply(self.cfg, plan['token'])
        self.assertTrue((moved / '20_Models').is_dir())
        self.assertTrue((moved / 'AGENTS.md').is_file())
        self.assertTrue(config._is_reparse(self.root.lstat()))

    def test_replaced_lock_is_preserved_even_when_empty(self):
        plan = self.preview()
        lock = self.data / '.workspace-apply.lock'
        retained = self.data / 'retained-lock'
        identities = []

        def replace_and_fail(_updated):
            identities.append(workspace._file_state(lock)['identity'])
            lock.rename(retained)
            lock.touch()
            identities.append(workspace._file_state(lock)['identity'])
            raise OSError('Original lock replacement failure')

        with mock.patch.object(config, 'save_config', side_effect=replace_and_fail):
            with self.assertRaisesRegex(OSError, 'Original lock replacement failure'):
                workspace.apply(self.cfg, plan['token'])
        self.assertNotEqual(*identities)
        self.assertTrue(lock.is_file())
        self.assertEqual(lock.read_bytes(), b'')
        self.assertTrue(retained.is_file())

    def test_lock_state_cleanup_error_preserves_original_exception(self):
        for cleanup_error in (OSError('Synthetic stat failure'), ValueError('Synthetic unsafe ancestor')):
            with self.subTest(error=type(cleanup_error).__name__):
                plan = self.preview()
                lock = self.data / '.workspace-apply.lock'
                original_state = workspace._file_state

                def state(path):
                    if Path(path) == lock:
                        raise cleanup_error
                    return original_state(path)

                with mock.patch.object(workspace, '_file_state', side_effect=state), \
                        mock.patch.object(config, 'save_config', side_effect=OSError('Primary write failure')):
                    with self.assertRaisesRegex(OSError, 'Primary write failure'):
                        workspace.apply(self.cfg, plan['token'])
                self.assertTrue(lock.is_file())
                lock.unlink()  # This known synthetic fixture belongs to the test.

    def test_custom_source_replaced_after_preview_invalidates_identity(self):
        source = self.root / 'custom_gallery'
        source.mkdir(parents=True)
        plan = self.preview(mode='connect', output_roots=[str(source)])
        source.rename(self.root / 'retained_original_gallery')
        source.mkdir()
        with self.assertRaises(ValueError):
            workspace.apply(self.cfg, plan['token'])
        self.assertFalse((self.root / 'AGENTS.md').exists())

    def test_dataset_selected_as_discovery_root_is_not_traversed(self):
        dataset = self.root / 'Datasets'
        (dataset / 'samples').mkdir(parents=True)
        self.assertEqual(config.discover_output_roots([str(dataset)])['items'], [])


if __name__ == '__main__':
    unittest.main()
