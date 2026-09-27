"""Exercise the work/report boundary and capability dispatch through real HTTP."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import server
from aihub import config, harnesses, collaboration_maintenance as maintenance
from tools.aihub_mcp import Bridge, BridgeError


class WorkcenterHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='yaohe-workcenter-http-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'Workspace'
        self.root.mkdir()
        self.data = self.base / 'App/data'
        self.data.mkdir(parents=True)
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True,
                    'catalog_dir': str(self.root / '00_Management/Catalogs')}
        for obj, name, value in [(config, 'DATA_DIR', str(self.data)),
                                  (config, 'REPORTS_DIR', str(self.data / 'reports')),
                                  (server, 'CFG', self.cfg), (server, 'DB_OBJ', None)]:
            context = patch.object(obj, name, value)
            context.start()
            self.addCleanup(context.stop)
        harnesses.save(self.cfg, {'id': 'codex', 'revision': 0, 'connection_mode': 'mcp_stdio'})
        class Quiet(server.Handler):
            def log_message(self, *_args):
                pass
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        self.thread = threading.Thread(target=self.http.serve_forever,
            kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)

    def request(self, path, body=None):
        req = urllib.request.Request('http://127.0.0.1:%s%s' % (self.http.server_port, path),
            data=None if body is None else json.dumps(body).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_external_report_search_classification_preview_and_reveal_are_id_scoped(self):
        source = self.base / 'Codex'
        folder = source / '2026-09-25' / 'demo' / 'outputs'
        folder.mkdir(parents=True)
        original = folder / '验收报告.md'
        original.write_text('# 验收\n原文件保留', encoding='utf-8')
        record = maintenance.add_source(self.cfg, {'path': str(source), 'label': 'Codex', 'tool': 'codex'})
        maintenance.scan_source(self.cfg, {'source_id': record['id']})
        code, listing = self.request('/api/workcenter/documents?tool=codex&query=%E9%AA%8C%E6%94%B6')
        self.assertEqual(code, 200, listing)
        self.assertEqual(listing['total'], 1)
        document = listing['items'][0]
        self.assertEqual(document['tool'], 'codex')
        self.assertEqual(listing['workspace_root'], str(self.root))
        code, preview = self.request('/api/workcenter/document?id=' + document['id'])
        self.assertEqual(code, 200, preview)
        self.assertIn('原文件保留', preview['content'])
        body = {'document_id': document['id'], 'category': 'plan', '_workspace_root': str(self.root)}
        self.assertEqual(self.request('/api/workcenter/classify', {**body, '_workspace_root': 'other'})[0], 409)
        self.assertEqual(self.request('/api/workcenter/classify', body)[0], 200)
        self.assertEqual(original.read_text(encoding='utf-8'), '# 验收\n原文件保留')
        with patch('subprocess.Popen') as launch:
            code, result = self.request('/api/workcenter/reveal', {'document_id': document['id'], '_workspace_root': str(self.root)})
            self.assertEqual(code, 200, result)
            self.assertEqual(launch.call_args.args[0], ['explorer.exe', '/select,', str(original)])
            launch.reset_mock()
            self.assertNotEqual(self.request('/api/workcenter/reveal', {'document_id': str(self.base / 'secret'), '_workspace_root': str(self.root)})[0], 200)
            launch.assert_not_called()
        self.assertEqual(self.request('/api/workcenter/document?id=unknown')[0], 400)
        code, projects = self.request('/api/workcenter/projects')
        self.assertEqual(code, 200, projects)
        self.assertTrue(any(p['id'] == document['project_id'] for p in projects['items']))

    def test_capability_discovery_dispatch_uses_existing_task_ownership_and_report_contract(self):
        def call(action, body):
            code, result = self.request('/api/collaboration/mcp/' + action, body)
            self.assertEqual(code, 200, result)
            return result
        call('client_heartbeat', {'client_id': 'worker-codex', 'tool': 'codex', 'name': 'Test worker', 'protocol_version': 1})
        entry = {'key': 'image-job', 'name': '图片任务处理', 'kind': 'mcp_tool',
                 'provider': 'fixture', 'server': 'fixture', 'domains': ['image'],
                 'description': '处理图片任务，结果写入分配目录', 'tags': ['图片'],
                 'inputs': {'type': 'object', 'properties': {'prompt': {'type': 'string'}},
                            'required': ['prompt'], 'additionalProperties': False},
                 'outputs': ['image'], 'constraints': ['仅测试，不调用外部服务']}
        call('capability_publish', {'client_id': 'worker-codex', 'capabilities_json': json.dumps([entry], ensure_ascii=False)})
        code, listed = self.request('/api/capabilities/list')
        self.assertEqual(code, 200, listed)
        cap = listed['items'][0]
        self.assertEqual(cap['target_tool'], 'codex')
        code, suggested = self.request('/api/capabilities/recommend', {'query': '图片任务', 'domain': 'image'})
        self.assertEqual(code, 200, suggested)
        self.assertTrue(suggested['items'])
        body = {'capability_id': cap['id'], 'project': '演示', 'title': '图片任务',
                'input_json': json.dumps({'prompt': 'test'}), '_workspace_root': str(self.root)}
        self.assertEqual(self.request('/api/capabilities/dispatch', {**body, '_workspace_root': 'other'})[0], 409)
        code, dispatched = self.request('/api/capabilities/dispatch', body)
        self.assertEqual(code, 200, dispatched)
        self.assertEqual(dispatched['status'], 'queued')
        self.assertTrue(dispatched['worker_required'])
        task = dispatched['task']
        self.assertEqual(task['target_tool'], 'codex')
        claim = call('task_claim', {'task_id': task['id'], 'client_id': 'worker-codex'})
        owned = {'task_id': task['id'], 'client_id': 'worker-codex', 'lease_token': claim['lease_token']}
        artifact = call('artifact_write', {**owned, 'kind': 'report', 'title': '调度结果',
                        'filename': '结果.md', 'content': '已完成测试任务，未调用外部模型。'})
        self.assertTrue(Path(artifact['path']).is_relative_to(Path(task['paths']['reports'])))
        call('task_finish', {**owned, 'summary': '测试闭环完成'})

    def test_mcp_session_remains_bound_when_workbench_switches_root(self):
        bridge = Bridge(self.http.server_port, 'root-binding-test', 'codex')
        bridge.heartbeat()
        self.assertEqual(bridge.workspace_root, str(self.root))
        first = bridge.request('task_create', {'project': 'First', 'title': 'Bound task'})
        self.assertTrue(Path(first['paths']['reports']).is_relative_to(self.root))
        other = self.base / 'OtherWorkspace'
        other.mkdir()
        self.cfg['ai_root'] = str(other)
        with self.assertRaises(BridgeError):
            bridge.request('task_create', {'project': 'Wrong', 'title': 'Must not be created'})
        with self.assertRaises(BridgeError):
            bridge.request('client_heartbeat', {})
        self.assertFalse((other / '40_Projects').exists())
        self.assertEqual(bridge.workspace_root, str(self.root))
        new_bridge = Bridge(self.http.server_port, 'root-binding-test', 'codex')
        with self.assertRaises(BridgeError):
            new_bridge.heartbeat()
        harnesses.save(self.cfg, {'id': 'codex', 'revision': 0, 'connection_mode': 'mcp_stdio'})
        new_bridge.heartbeat()
        self.assertEqual(new_bridge.workspace_root, str(other))


if __name__ == '__main__':
    unittest.main()
