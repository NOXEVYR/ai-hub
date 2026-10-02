"""Budget and recovery diagnostics on disposable project/report directories."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aihub import config, management


class ManagementCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihub-coverage-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        self.data = self.base / 'application' / 'data'
        self.data.mkdir(parents=True)
        self.cfg = {'ai_root': str(self.root)}
        for name, path in [('DATA_DIR', self.data), ('APP_DIR', self.data.parent)]:
            patch = mock.patch.object(config, name, str(path))
            patch.start()
            self.addCleanup(patch.stop)

    def test_project_budget_is_exact_and_records_truncation(self):
        folder = self.root / '40_Projects'
        folder.mkdir()
        for number in range(500):
            (folder / ('p-%03d' % number)).mkdir()
        complete = management.projects(self.cfg)
        self.assertEqual(len(complete['items']), 500)
        self.assertFalse(complete['discovery']['partial'])
        (folder / 'p-500').mkdir()
        partial = management.projects(self.cfg)
        self.assertEqual(len(partial['items']), 500)
        self.assertTrue(partial['discovery']['partial'])
        self.assertEqual(partial['discovery']['partial_locations'], [
            {'path': str(folder), 'reason': 'project_limit', 'limit': 500}])

    def test_report_budget_is_explicit_and_legacy_list_contract_is_retained(self):
        folder = self.data / 'reports'
        folder.mkdir()
        for number in range(1000):
            (folder / ('report-%04d.md' % number)).write_text('synthetic', encoding='utf-8')
        complete = {}
        self.assertEqual(len(management.reports(self.cfg, folder, diagnostics=complete)), 1000)
        self.assertFalse(complete.get('partial_locations'))
        (folder / 'report-1000.md').write_text('synthetic', encoding='utf-8')
        partial = {}
        self.assertEqual(len(management.reports(self.cfg, folder, diagnostics=partial)), 1000)
        self.assertIn({'path': str(folder), 'reason': 'report_limit', 'limit': 1000}, partial['partial_locations'])
        self.assertIsInstance(management.reports(self.cfg, folder), list)

    def test_damaged_registry_does_not_masquerade_as_clean_empty_inventory(self):
        (self.data / 'registry.json').write_text('{invalid', encoding='utf-8')
        projects = management.projects(self.cfg)
        self.assertEqual(projects['items'], [])
        self.assertTrue(projects['warnings'])
        diagnostics = {}
        self.assertEqual(management.reports(self.cfg, self.data / 'reports', diagnostics=diagnostics), [])
        self.assertTrue(diagnostics['warnings'])
        self.assertEqual((self.data / 'registry.json').read_text(encoding='utf-8'), '{invalid')

    def test_empty_workspace_has_no_false_budget_or_failure_warnings(self):
        projects = management.projects(self.cfg)
        self.assertFalse(projects['discovery']['partial'])
        diagnostics = {}
        self.assertEqual(management.reports(self.cfg, self.data / 'reports', diagnostics=diagnostics), [])
        self.assertFalse(diagnostics.get('warnings'))
        self.assertFalse(diagnostics.get('partial_locations'))
        self.assertFalse(diagnostics.get('unavailable_locations'))

    def test_explicit_template_is_discoverable_without_exposing_private_directories(self):
        folder = self.root / '40_Projects'
        for name in ('_TEMPLATE_PROJ-YYYY-NNN_项目名', '_private', '.hidden', '_templatecredentials'):
            (folder / name).mkdir(parents=True)
        result = management.projects(self.cfg)
        self.assertEqual([item['name'] for item in result['items']], ['_TEMPLATE_PROJ-YYYY-NNN_项目名'])
        self.assertFalse(result['items'][0]['registered'])


if __name__ == '__main__':
    unittest.main()
