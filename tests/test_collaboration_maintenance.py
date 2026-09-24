"""Synthetic sources only. Never recycle real workspaces or native memories."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from aihub import config, collaboration_maintenance as maintenance


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihub-maintenance-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        self.data = self.base / 'application/data'
        self.data.mkdir(parents=True)
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True}
        for name, value in [('DATA_DIR', str(self.data)), ('APP_DIR', str(self.data.parent))]:
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(maintenance, '_root', side_effect=lambda cfg: cfg['ai_root'])
        patch.start()
        self.addCleanup(patch.stop)

    def source(self, root=None):
        return maintenance.add_source(self.cfg, {'path': str(root or self.root), 'label': '合成来源', 'tool': 'codex'})

    def test_read_only_inventory_excludes_native_state_secrets_links_and_weights(self):
        (self.root / '验收报告.md').write_text('keep original', encoding='utf-8')
        (self.root / 'secret-token.txt').write_text('never read')
        (self.root / 'model.safetensors').write_bytes(b'not a model')
        (self.root / '.codex').mkdir()
        (self.root / '.codex/native-memory.md').write_text('native memory')
        (self.root / 'Temp').mkdir()
        (self.root / 'Temp/scratch.txt').write_text('keep too')
        hard = self.root / 'linked-report.md'
        os.link(self.root / '验收报告.md', hard)
        source = self.source()
        result = maintenance.scan_source(self.cfg, {'source_id': source['id']})
        self.assertEqual([r['title'] for r in result['items']], ['scratch.txt'])
        self.assertEqual(result['items'][0]['category'], 'temp_candidate')
        self.assertFalse(result['retention_authority'])
        self.assertEqual((self.root / '.codex/native-memory.md').read_text(), 'native memory')
        self.assertTrue(hard.exists())

    def test_root_partition_partial_scan_preserves_prior_rows(self):
        for number in range(3):
            (self.root / ('report%d.md' % number)).write_text(str(number))
        source = self.source()
        first = maintenance.scan_source(self.cfg, {'source_id': source['id']})
        self.assertEqual(len(first['items']), 3)
        with mock.patch.object(maintenance, 'MAX_ITEMS', 1):
            second = maintenance.scan_source(self.cfg, {'source_id': source['id']})
        self.assertTrue(second['truncated'])
        self.assertEqual(maintenance.inventory(self.cfg)['total'], 3)
        other = {**self.cfg, 'ai_root': str(self.base / 'other')}
        self.assertEqual(maintenance.list_sources(other)['items'], [])
        self.assertEqual(maintenance.inventory(other)['total'], 0)
        with self.assertRaises(ValueError):
            maintenance.scan_source(other, {'source_id': source['id']})

    def test_settings_validation_and_mcp_cannot_expand_authority(self):
        self.assertTrue(maintenance.policy(self.cfg)['enabled'])
        result = maintenance.save_policy(self.cfg, {'enabled': False, 'days': 30})
        self.assertFalse(result['enabled'])
        self.assertEqual(result['days'], 30)
        self.assertEqual(result['days_apply_to'], 'new_artifacts')
        for body in ({'enabled': 'yes', 'days': 7}, {'enabled': True, 'days': True}, {'enabled': True, 'days': 0}):
            with self.assertRaises(ValueError):
                maintenance.save_policy(self.cfg, body)
        for action in ('source_add', 'source_scan', 'retention_policy', 'retention_run'):
            with self.assertRaises(PermissionError):
                maintenance.execute(self.cfg, action, {}, actor='mcp')

    def test_source_internal_directories_rejected(self):
        state = self.root / '.workbuddy'
        state.mkdir()
        with self.assertRaises(ValueError):
            self.source(state)
        self.assertEqual(maintenance.list_sources(self.cfg)['items'], [])

    def test_scheduled_run_is_leased_rate_limited_and_failure_preserves(self):
        core = mock.Mock()
        core.retention_count.return_value = 1
        core.retention_candidates.return_value = {'items': [{'id': 'x'}]}
        core.recycle_candidate.return_value = {'id': 'x', 'status': 'error', 'error': 'recycle denied'}
        with mock.patch.object(maintenance, '_core', return_value=core):
            first = maintenance.run_retention(self.cfg, scheduled=True)
            second = maintenance.run_retention(self.cfg, scheduled=True)
            self.assertEqual(first['recycled'], 0)
            self.assertEqual(first['errors'], ['recycle denied'])
            self.assertTrue(second['skipped'])
            self.assertEqual(core.recycle_candidate.call_count, 1)
            self.assertIs(core.recycle_candidate.call_args.args[2], maintenance.recycle.recycle_file)
            self.assertEqual(maintenance.policy(self.cfg)['last_run']['errors'], ['recycle denied'])
            maintenance.save_policy(self.cfg, {'enabled': False, 'days': 7})
            self.assertTrue(maintenance.run_retention(self.cfg, scheduled=True)['skipped'])

    def test_protected_first_batch_cannot_starve_later_candidates(self):
        core = mock.Mock()
        core.retention_count.return_value = 101
        core.retention_candidates.side_effect = lambda cfg, **kw: [{'id': str(n)} for n in range(kw['offset'], min(101, kw['offset'] + kw['limit']))]
        core.recycle_candidate.side_effect = lambda cfg, ident, recycler, **kw: {'id': ident, 'status': 'recycled' if ident == '100' else 'protected'}
        with mock.patch.object(maintenance, '_core', return_value=core):
            first = maintenance.run_retention(self.cfg)
            second = maintenance.run_retention(self.cfg)
        self.assertEqual(first['checked'], 100)
        self.assertEqual(first['next_offset'], 100)
        self.assertEqual(second['checked'], 1)
        self.assertEqual(second['recycled'], 1)
        self.assertEqual(second['next_offset'], 0)

    def test_scheduler_stops_without_processing_unmanaged_workspace(self):
        with mock.patch.object(maintenance, 'run_retention') as run:
            stop, thread = maintenance.start_scheduler({'workspace_managed': False})
            stop.set()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
