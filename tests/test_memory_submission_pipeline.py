"""Exercise the actual stdio -> HTTP -> SQLite memory candidate boundary."""
import json
import os
import datetime as dt
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

import server
from aihub import api, collaboration, collaboration_api, config, harnesses
from tools.aihub_mcp import BY_NAME, RPCError, validate


class MemorySubmissionPipeline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihub-memory-pipeline-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / '工作区'
        self.root.mkdir()
        self.app = self.base / 'App'
        self.data = self.app / 'data'
        self.data.mkdir(parents=True)
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True, 'server': {'port': 8765}}
        for target, name, value in [(config, 'APP_DIR', str(self.app)),
                (config, 'DATA_DIR', str(self.data)), (config, 'CONFIG_PATH', str(self.data / 'config.json')),
                (server, 'CFG', self.cfg), (server, 'DB_OBJ', None), (api, 'APP_CFG', self.cfg)]:
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        harnesses.save(self.cfg, {'id': 'codex', 'revision': 0, 'connection_mode': 'mcp_stdio'})
        collaboration.execute(self.cfg, 'client_heartbeat', {'client_id': 'pipeline-client', 'tool': 'codex',
                              'name': 'pipeline-client', 'protocol_version': 1})
        task = collaboration.execute(self.cfg, 'task_create', {'project': '合成项目', 'title': '记忆链路'})
        claim = collaboration.execute(self.cfg, 'task_claim', {'task_id': task['id'], 'client_id': 'pipeline-client'})
        self.owner = {'task_id': task['id'], 'lease_token': claim['lease_token']}
        class Quiet(server.Handler):
            def log_message(self, *_args):
                pass
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        self.cfg['server']['port'] = self.http.server_address[1]
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def run_bridge(self, args, tool='aihub_report_submit'):
        messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'Synthetic', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                'name': tool, 'arguments': args}}]
        script = Path(__file__).resolve().parents[1] / 'tools/aihub_mcp.py'
        result = subprocess.run([sys.executable, '-B', str(script), '--port', str(self.http.server_address[1]),
            '--tool', 'codex', '--client-id', 'pipeline-client'], input=''.join(json.dumps(m, ensure_ascii=False)+'\n' for m in messages),
            text=True, encoding='utf-8', capture_output=True, timeout=20,
            env=dict(os.environ, APPDATA=str(self.base / 'AppData'), XDG_DATA_HOME=str(self.base / 'AppData')))
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = [json.loads(line) for line in result.stdout.splitlines()][-1]
        self.assertNotIn('error', reply)
        self.assertFalse(reply['result']['isError'])
        return json.loads(reply['result']['content'][0]['text'])

    def request(self, path, body=None):
        request = urllib.request.Request('http://127.0.0.1:%s%s' % (self.http.server_address[1], path),
            data=None if body is None else json.dumps(body).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_report_and_candidate_reach_database_but_not_approved_search(self):
        result = self.run_bridge({**self.owner, 'category': 'report', 'title': '验收报告',
            'filename': '验收.md', 'content': '# 合成报告', 'memory_candidates': [
                {'title': '可复用约定', 'content': '合成项目稳定约定', 'scope': 'project'}]})
        self.assertEqual(result['memory_candidate_count'], 1)
        self.assertTrue(result['memory_declared'])
        status = collaboration_api.status(self.cfg)
        self.assertEqual(status['memory_pipeline']['reports'], 1)
        self.assertEqual(status['memory_pipeline']['candidates'], 1)
        self.assertEqual(status['memory_pipeline']['reports_evaluated'], 1)
        self.assertEqual(status['memory_pipeline']['reports_unevaluated'], 0)
        self.assertEqual(status['memory_pipeline']['reports_evaluated_without_candidates'], 0)
        self.assertEqual(collaboration.execute(self.cfg, 'memory_search', {})['items'], [])
        candidate = status['memories'][0]
        self.assertEqual(candidate['source_artifact_id'], result['id'])
        self.assertEqual(candidate['project'], '合成项目')
        collaboration.execute(self.cfg, 'memory_review', {'memory_id': candidate['id'], 'status': 'approved'})
        approved = collaboration.execute(self.cfg, 'memory_search', {})['items']
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]['source_artifact_id'], result['id'])

    def test_explicit_empty_candidates_and_malformed_nested_declarations(self):
        schema = BY_NAME['aihub_report_submit']['inputSchema']
        args = {**self.owner, 'category': 'report', 'title': '无长期结论',
                'filename': '无记忆.md', 'content': '合成日常报告', 'memory_candidates': []}
        result = self.run_bridge(args)
        self.assertEqual(result['memory_candidate_count'], 0)
        self.assertTrue(result['memory_declared'])
        pipeline = collaboration_api.status(self.cfg)['memory_pipeline']
        self.assertEqual(pipeline['candidates'], 0)
        self.assertEqual(pipeline['reports_evaluated'], 1)
        self.assertEqual(pipeline['reports_unevaluated'], 0)
        self.assertEqual(pipeline['reports_evaluated_without_candidates'], 1)
        for candidates in ['wrong', [1], [{}], [{'title': 'x', 'content': 'y', 'scope': 'project', 'client_id': 'spoof'}],
                           [{'title': 'x', 'content': 'y', 'scope': 'project'}] * 6]:
            with self.subTest(candidates=candidates), self.assertRaises(RPCError):
                validate(schema, dict(args, memory_candidates=candidates))
        missing = dict(args)
        del missing['memory_candidates']
        with self.assertRaises(RPCError):
            validate(schema, missing)

    def test_compatibility_submission_without_memory_decision_remains_unevaluated(self):
        result = collaboration.execute(self.cfg, 'artifact_write', {**self.owner,
            'client_id': 'pipeline-client', 'kind': 'report', 'category': 'report',
            'title': '旧客户端报告', 'filename': '旧提交.md', 'content': '未声明'})
        self.assertIs(result['memory_declared'], False)
        pipeline = collaboration_api.status(self.cfg)['memory_pipeline']
        self.assertEqual(pipeline['reports_evaluated'], 0)
        self.assertEqual(pipeline['reports_unevaluated'], 1)
        self.assertEqual(pipeline['reports_evaluated_without_candidates'], 0)

    def test_workspace_detach_http_rejects_busy_and_wrong_root_then_preserves_files(self):
        root = {'_workspace_root': str(self.root)}
        self.assertEqual(self.request('/api/workspace/detach_preview', root)[0], 400)
        self.run_bridge({**self.owner, 'category': 'report', 'title': '解绑前验收',
            'filename': 'detach.md', 'content': '合成任务完成', 'memory_candidates': []})
        collaboration.execute(self.cfg, 'task_finish', {**self.owner, 'client_id': 'pipeline-client', 'summary': '合成完成'})
        marker = self.root/'保留.md'
        marker.write_text('preserve', encoding='utf-8')
        with mock.patch.object(api.organization, 'busy', return_value=True):
            self.assertEqual(self.request('/api/workspace/detach_preview', root)[0], 409)
        code, preview = self.request('/api/workspace/detach_preview', root)
        self.assertEqual(code, 200)
        self.assertEqual(self.request('/api/workspace/detach_apply', {'token': preview['token'], '_workspace_root': 'wrong'})[0], 409)
        code, result = self.request('/api/workspace/detach_apply', {**root, 'token': preview['token']})
        self.assertEqual(code, 200)
        self.assertTrue(result['applied'])
        self.assertEqual(self.cfg['ai_root'], '')
        self.assertEqual(marker.read_text(), 'preserve')

    def test_report_removal_http_hides_all_federated_views_and_can_restore(self):
        report = self.run_bridge({**self.owner, 'category': 'report', 'title': '移除验收',
            'filename': '列表.md', 'content': 'synthetic', 'memory_candidates': []})
        code, listing = self.request('/api/workcenter/documents')
        self.assertEqual(code, 200)
        document = next(item for item in listing['items'] if item.get('artifact_id') == report['id'])
        body = {'document_id': document['id'], '_workspace_root': str(self.root)}
        code, preview = self.request('/api/workcenter/removal-preview', body)
        self.assertEqual(code, 200)
        code, result = self.request('/api/workcenter/removal-apply', {'preview_token': preview['preview_token'], '_workspace_root': str(self.root)})
        self.assertEqual(code, 200)
        self.assertEqual(self.request('/api/workcenter/documents')[1]['total'], 0)
        self.assertEqual(Path(report['path']).read_text(), 'synthetic')
        self.assertEqual(self.request('/api/workcenter/removals')[1]['total'], 1)
        self.assertEqual(self.request('/api/workcenter/removal-restore', body)[0], 200)
        self.assertEqual(self.request('/api/workcenter/documents')[1]['total'], 1)

    def test_task_resource_stdio_completion_and_client_cleanup_evidence(self):
        resource = self.run_bridge({**self.owner, 'resource_type': 'browser_tab',
            'identity': {'browser_id': 'synthetic', 'session_id': 'test-session', 'tab_id': 'test-tab'},
            'ownership': 'task_exclusive', 'temporary': True, 'label': '合成测试页'}, 'aihub_resource_register')
        self.assertEqual(resource['state'], 'running')
        self.assertNotIn('lease_hash', resource)
        self.assertFalse(resource['host_verified'])
        self.run_bridge({**self.owner, 'category': 'report', 'title': '资源任务验收',
            'filename': 'resources.md', 'content': '合成资源登记完成，清理证据待客户端上报', 'memory_candidates': []})
        finished = self.run_bridge({**self.owner, 'summary': '合成完成'}, 'aihub_task_finish')
        self.assertEqual(finished['resource_cleanup']['counts']['cleanup_pending'], 1)
        self.assertEqual(self.request('/api/collaboration/status')[1]['resources']['total'], 1)
        closed = self.run_bridge({**self.owner, 'resource_id': resource['id'], 'state': 'closed',
            'evidence': {'identity': resource['identity'], 'observed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                'outcome': 'absent', 'detail': '合成客户端已检查'}}, 'aihub_resource_cleanup_report')
        self.assertEqual(closed['state'], 'closed')
        self.assertEqual(closed['evidence_source'], 'client_report')
        self.assertFalse(closed['automatic_control'])
        self.assertFalse(closed['host_verified'])

    def test_nested_resource_protocol_rejects_identity_spoofing_bad_boolean_and_unbounded_sample(self):
        schema = BY_NAME['aihub_resource_register']['inputSchema']
        args = {**self.owner, 'resource_type': 'service_process', 'identity': {'pid': 123,
            'process_started_at': '2026-10-01T00:00:00Z'}, 'ownership': 'task_exclusive', 'temporary': True}
        validate(schema, args)
        for body in [dict(args, identity={**args['identity'], 'client_id': 'spoof'}),
                     dict(args, temporary=1), dict(args, identity={**args['identity'], 'pid': True}),
                     dict(args, sample={'sampled_at': '2026-10-01T00:00:00Z', 'ram_available_bytes': float('inf')}),
                     dict(args, sample={'sampled_at': '2026-10-01T00:00:00Z', 'ram_available_bytes': 10 ** 500})]:
            with self.subTest(body=body), self.assertRaises(RPCError):
                validate(schema, body)


if __name__ == '__main__':
    unittest.main()
