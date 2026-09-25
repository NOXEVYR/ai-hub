from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aihub import management


class ManagedReportsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        self.legacy = self.base / 'data' / 'reports'
        self.legacy.mkdir(parents=True)
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True}

    def tearDown(self):
        self.tmp.cleanup()

    def test_managed_destination_does_not_create_directories(self):
        destination = management.generated_report_dir(self.cfg, self.legacy)
        self.assertEqual(destination, self.root / '00_Management' / 'Reports' / 'AIHub')
        self.assertFalse(destination.exists())
        self.assertFalse((self.root / '00_Management').exists())

    def test_legacy_and_managed_reports_coexist_without_moving(self):
        legacy = self.legacy / 'old.md'
        legacy.write_text('legacy report', encoding='utf-8')
        destination = management.generated_report_dir(self.cfg, self.legacy)
        destination.mkdir(parents=True)
        current = destination / 'new.md'
        current.write_text('managed report', encoding='utf-8')
        # Isolate the report-library behavior from unrelated project discovery.
        with patch.object(management, 'projects', return_value={'items': []}), patch.object(management, '_registry', return_value={'knowledge': []}):
            records = management.reports(self.cfg, self.legacy)
        self.assertEqual({row['path'] for row in records}, {str(legacy), str(current)})
        self.assertEqual(legacy.read_text(), 'legacy report')
        self.assertEqual(current.read_text(), 'managed report')

    def test_unmanaged_keeps_legacy_and_deduplicates(self):
        cfg = dict(self.cfg, workspace_managed=False)
        self.assertEqual(management.generated_report_dir(cfg, self.legacy), self.legacy)
        legacy = self.legacy / 'old.md'
        legacy.write_text('old')
        with patch.object(management, 'projects', return_value={'items': []}), patch.object(management, '_registry', return_value={'knowledge': []}):
            records = management.reports(cfg, self.legacy)
        self.assertEqual([row['path'] for row in records], [str(legacy)])

    def test_invalid_managed_root_never_falls_back(self):
        for root in ['', 'relative', str(self.base / 'missing')]:
            cfg = dict(self.cfg, ai_root=root)
            with self.subTest(root=root), self.assertRaises((ValueError, OSError)):
                management.generated_report_dir(cfg, self.legacy)
            with self.subTest(reports_root=root), self.assertRaises((ValueError, OSError)):
                management.reports(cfg, self.legacy)
        self.assertEqual(list(self.legacy.iterdir()), [])

    def test_destination_occupied_by_file_is_rejected(self):
        destination = self.root / '00_Management' / 'Reports' / 'AIHub'
        destination.parent.mkdir(parents=True)
        destination.write_text('do not overwrite')
        with self.assertRaises(ValueError):
            management.generated_report_dir(self.cfg, self.legacy)
        self.assertEqual(destination.read_text(), 'do not overwrite')

    def test_link_validation_is_not_swallowed(self):
        from aihub import config
        original = config._check_ancestors
        destination = self.root / '00_Management' / 'Reports' / 'AIHub'
        def reject_destination(path):
            if Path(path) == destination:
                raise ValueError('reparse point')
            return original(path)
        with patch.object(config, '_check_ancestors', side_effect=reject_destination), self.assertRaisesRegex(ValueError, 'reparse'):
            management.generated_report_dir(self.cfg, self.legacy)

    def test_actual_symlink_destination_is_rejected(self):
        parent = self.root / '00_Management' / 'Reports'
        parent.mkdir(parents=True)
        destination = parent / 'AIHub'
        try:
            destination.symlink_to(self.legacy, target_is_directory=True)
        except OSError:
            self.skipTest('OS does not permit symbolic links')
        with self.assertRaises(ValueError):
            management.generated_report_dir(self.cfg, self.legacy)


if __name__ == '__main__':
    unittest.main()
