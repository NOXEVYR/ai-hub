"""Synthetic HTTP acceptance for dynamic work ends, never executes an installed tool."""
import json
from pathlib import Path
import unittest
from unittest import mock

import test_collaboration_http as fixture
from aihub import config, projects


class HarnessHTTP(unittest.TestCase):
    setUp = fixture.CollaborationHTTP.setUp
    stop = fixture.CollaborationHTTP.stop
    request = fixture.CollaborationHTTP.request
    call = fixture.CollaborationHTTP.call

    def register(self, identifier='studio-cli', **values):
        values.setdefault('connection_mode', 'mcp_stdio')
        code, result = self.request('/api/harnesses/save', dict(
            id=identifier, name='Synthetic Studio', revision=0,
            _workspace_root=str(self.root), **values))
        self.assertEqual(code, 200, result)
        return result

    def check(self, identifier='studio-cli'):
        code, result = self.request('/api/harnesses/check?id=' + identifier)
        self.assertEqual(code, 200, result)
        return result

    def heartbeat(self, identifier='studio-cli'):
        return self.call('client_heartbeat', {'client_id': identifier + '-client', 'tool': identifier,
            'name': 'Synthetic worker', 'protocol_version': 1}, True)

    def test_registration_config_and_evidence_are_separate(self):
        record = self.register()
        self.assertEqual(record['revision'], 1)
        self.assertFalse(record['recent_heartbeat'])
        self.assertFalse(record['invocation_verified'])
        self.heartbeat()
        self.assertTrue(self.check()['recent_heartbeat'])
        self.assertFalse(self.check()['invocation_verified'])
        self.call('task_list', {'client_id': 'studio-cli-client'}, True)
        self.assertTrue(self.check()['invocation_verified'])
        code, snippet = self.request('/api/harnesses/config?id=studio-cli&client_id=custom-session')
        self.assertEqual(code, 200, snippet)
        self.assertEqual(snippet['args'][-1], 'custom-session')
        self.assertIn('studio-cli', snippet['args'])
        code, updated = self.request('/api/harnesses/save', {'id': 'studio-cli', 'revision': 1,
            'name': 'Renamed', '_workspace_root': str(self.root)})
        self.assertEqual(code, 200, updated)
        self.assertFalse(updated['invocation_verified'])
        self.assertEqual(self.request('/api/harnesses/save', {'id': 'studio-cli', 'revision': 1,
            'name': 'Stale', '_workspace_root': str(self.root)})[0], 409)

    def test_active_owner_blocks_disable_and_cached_client_is_denied_after_finish(self):
        self.register()
        self.heartbeat()
        task = self.call('task_create', {'project': 'Project', 'title': 'Owned', 'target_tool': 'studio-cli'})
        claim = self.call('task_claim', {'task_id': task['id'], 'client_id': 'studio-cli-client'}, True)
        disable = {'id': 'studio-cli', 'revision': 1, 'enabled': False, '_workspace_root': str(self.root)}
        self.assertEqual(self.request('/api/harnesses/save', disable)[0], 409)
        self.call('task_finish', {'task_id': task['id'], 'client_id': 'studio-cli-client',
            'lease_token': claim['lease_token'], 'summary': 'done'}, True)
        code, disabled = self.request('/api/harnesses/save', disable)
        self.assertEqual(code, 200, disabled)
        self.assertFalse(disabled['recent_heartbeat'])
        self.assertFalse(disabled['invocation_verified'])
        for action in ('task_list', 'source_list', 'retention_preview', 'capability_list', 'capability_recommend'):
            with self.subTest(action=action):
                self.assertEqual(self.request('/api/collaboration/mcp/' + action,
                    {'client_id': 'studio-cli-client', 'query': 'video'})[0], 403)
        self.assertNotEqual(self.request('/api/collaboration/mcp/client_heartbeat',
            {'client_id': 'studio-cli-client', 'tool': 'studio-cli'})[0], 200)
        self.assertEqual(self.call('task_list', {'target_tool': 'studio-cli'})['items'][0]['status'], 'completed')

    def test_manual_handoff_works_in_projects_and_sources_but_cannot_claim_or_queue(self):
        self.register(connection_mode='manual')
        code, snippet = self.request('/api/harnesses/config?id=studio-cli')
        self.assertEqual(code, 200, snippet)
        self.assertIsNone(snippet['config'])
        self.assertNotEqual(self.request('/api/collaboration/client_heartbeat',
            {'client_id': 'studio-cli-client', 'tool': 'studio-cli'})[0], 200)
        self.assertNotEqual(self.request('/api/collaboration/task_create',
            {'project': 'Project', 'title': 'Cannot queue', 'target_tool': 'studio-cli'})[0], 200)
        project = self.root / '40_Projects/Manual'
        documents = projects._documents(project, ['studio-cli'], cfg=self.cfg)
        self.assertIn(str(project / 'AIHUB_HANDOFF_studio-cli.md'), documents)
        manifest = json.loads(documents[str(project / '.aihub-project.json')])
        self.assertEqual(manifest['handoff_files'], ['AIHUB_HANDOFF_studio-cli.md'])
        source = self.root / 'ManualReports'
        source.mkdir()
        result = self.call('source_add', {'tool': 'studio-cli', 'path': str(source), 'name': 'Reports'})
        self.assertEqual(result['tool'], 'studio-cli')

    def test_root_binding_unknown_ids_and_read_only_checks(self):
        before = set(self.data.iterdir())
        for path in ('/api/harnesses', '/api/harnesses/check?id=codex', '/api/harnesses/discover'):
            with mock.patch('subprocess.Popen', side_effect=AssertionError('must not launch')):
                self.assertEqual(self.request(path)[0], 200)
        self.assertEqual(set(self.data.iterdir()), before)
        self.assertEqual(self.request('/api/harnesses/save', {'id': 'studio-cli', 'name': 'Other',
            'revision': 0, '_workspace_root': str(self.base / 'Other')})[0], 409)
        self.assertNotEqual(self.request('/api/collaboration/client_heartbeat',
            {'client_id': 'unknown', 'tool': 'unregistered'})[0], 200)
        self.register()
        other = self.base / 'Other'
        other.mkdir()
        self.cfg['ai_root'] = str(other)
        code, result = self.request('/api/harnesses')
        self.assertEqual(code, 200, result)
        self.assertNotIn('studio-cli', [v['id'] for v in result['items']])

    def test_custom_capability_is_dispatchable_then_historical_only_when_disabled(self):
        self.register()
        self.heartbeat()
        item = {'key': 'write.report', 'name': 'Report writing', 'kind': 'mcp_tool',
            'provider': 'fixture', 'server': 'fixture', 'domains': ['document'],
            'description': 'Write a report', 'tags': ['report'],
            'inputs': {'type': 'object', 'properties': {}, 'additionalProperties': False},
            'outputs': ['report'], 'constraints': []}
        self.call('capability_publish', {'client_id': 'studio-cli-client',
            'capabilities_json': json.dumps([item])}, True)
        catalog = self.call('capability_list', {'tool': 'studio-cli'})
        entry = catalog['items'][0]
        queued = self.call('capability_dispatch', {'capability_id': entry['id'],
            'project': 'Reports', 'title': 'Draft', 'input_json': '{}'})
        self.assertEqual(queued['target_tool'], 'studio-cli')
        self.assertEqual(self.request('/api/harnesses/save', {'id': 'studio-cli', 'revision': 1,
            'enabled': False, '_workspace_root': str(self.root)})[0], 200)
        self.assertFalse(self.call('capability_list', {'tool': 'studio-cli'})['items'][0]['tool_enabled'])
        self.assertEqual(self.call('capability_recommend', {'query': 'report'})['items'], [])
        self.assertNotEqual(self.request('/api/collaboration/capability_dispatch',
            {'capability_id': entry['id'], 'project': 'Reports', 'title': 'Draft'})[0], 200)


if __name__ == '__main__':
    unittest.main()
