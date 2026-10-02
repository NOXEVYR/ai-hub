import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aihub import collaboration, config, workspace


class WorkspaceDetach(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihub-detach-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.data = self.base / 'App/data'
        self.data.mkdir(parents=True)
        self.root = self.base / '工作区'
        self.root.mkdir()
        for name, value in [('APP_DIR', str(self.data.parent)), ('DATA_DIR', str(self.data)),
                ('CONFIG_PATH', str(self.data/'config.json'))]:
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True, 'scan_roots': [str(self.root)],
                    'output_roots': [], 'custom': {'keep': 7}, 'organizer': {'enabled': False}}
        config.save_config(self.cfg)
        workspace._detach_previews.clear()

    def preview(self):
        return workspace.detach_preview(self.cfg, {'_workspace_root': str(self.root)})

    def test_preview_writes_nothing_apply_preserves_files_and_config_backup(self):
        original = self.root/'原报告.md'
        original.write_text('synthetic', encoding='utf-8')
        before = Path(config.CONFIG_PATH).read_bytes()
        plan = self.preview()
        self.assertEqual(Path(config.CONFIG_PATH).read_bytes(), before)
        self.assertFalse((self.data/'workspace-backups').exists())
        result = workspace.detach_apply(self.cfg, plan['token'])
        self.assertTrue(result['applied'])
        self.assertEqual(Path(result['backup_path']).read_bytes(), before)
        self.assertEqual(original.read_text(), 'synthetic')
        self.assertEqual(self.cfg['ai_root'], '')
        self.assertEqual(self.cfg['scan_roots'], [])
        self.assertEqual(self.cfg['custom'], {'keep': 7})
        with self.assertRaises(ValueError):
            workspace.detach_apply(self.cfg, plan['token'])

    def test_changed_configuration_expired_preview_and_queue_reject_without_write(self):
        plan = self.preview()
        self.cfg['custom']['keep'] = 8
        before = Path(config.CONFIG_PATH).read_bytes()
        with self.assertRaises(ValueError):
            workspace.detach_apply(self.cfg, plan['token'])
        self.assertEqual(Path(config.CONFIG_PATH).read_bytes(), before)
        plan = self.preview()
        workspace._detach_previews[plan['token']]['expires_at'] = 0
        with self.assertRaises(ValueError):
            workspace.detach_apply(self.cfg, plan['token'])
        plan = self.preview()
        collaboration.execute(self.cfg, 'task_create', {'project': 'Project', 'title': '待办'})
        with self.assertRaises(ValueError):
            workspace.detach_apply(self.cfg, plan['token'])
        with self.assertRaises(ValueError):
            self.preview()
        self.assertEqual(Path(config.CONFIG_PATH).read_bytes(), before)

    def test_lock_save_failure_and_wrong_workspace_do_not_clear_live_config(self):
        for root in ['other', 1, None]:
            with self.subTest(root=root), self.assertRaises(ValueError):
                workspace.detach_preview(self.cfg, {'_workspace_root': root})
        plan = self.preview()
        before = copy.deepcopy(self.cfg)
        (self.data/'.workspace-apply.lock').write_text('other owner')
        with self.assertRaises(ValueError):
            workspace.detach_apply(self.cfg, plan['token'])
        self.assertEqual((self.data/'.workspace-apply.lock').read_text(), 'other owner')
        (self.data/'.workspace-apply.lock').unlink()
        with mock.patch.object(config, 'save_config', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                workspace.detach_apply(self.cfg, plan['token'])
        self.assertEqual(self.cfg, before)
        self.assertFalse((self.data/'.workspace-apply.lock').exists())
