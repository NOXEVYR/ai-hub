"""Read-only route integration; temporary index and synthetic files only."""
import unittest
import test_organization_http as fixtures


class GovernanceHTTP(unittest.TestCase):
    setUp = fixtures.OrganizationHTTP.setUp
    tearDown = fixtures.OrganizationHTTP.tearDown
    patch = fixtures.OrganizationHTTP.patch
    request = fixtures.OrganizationHTTP.request
    ok = fixtures.OrganizationHTTP.ok

    def test_get_diagnostic_and_static_module_without_index_mutation(self):
        model = self.root / 'synthetic.gguf'
        model.write_bytes(b'fixture')
        self.cfg.update(ai_root=str(self.root), scan_roots=[str(self.root)], workspace_managed=True)
        self.db.upsert_model({'path': str(model), 'filename': model.name, 'size': 7})
        before = self.db.conn.total_changes
        report = self.ok('GET', '/api/models/governance?limit=1')
        self.assertEqual(report['summary']['identified_file_bytes'], 7)
        self.assertTrue(report['read_only'])
        self.assertFalse(report['safe_to_delete'])
        self.assertEqual(self.db.conn.total_changes, before)
        self.assertIn('model-governance.js', self.ok('GET', '/'))
        self.assertIn('AIHubModelGovernance', self.ok('GET', '/model-governance.js'))

    def test_write_method_and_oversized_batch_are_rejected(self):
        self.assertEqual(self.request('POST', '/api/models/governance', {})[0], 404)
        self.assertEqual(self.request('GET', '/api/models/governance?limit=5001')[0], 400)
