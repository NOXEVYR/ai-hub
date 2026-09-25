"""Real HTTP + stdio bridge acceptance against entirely synthetic workspaces."""
import datetime
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

import server
from aihub import config, collaboration as core, collaboration_maintenance as maintenance
from aihub import collaboration_api


class CollaborationHTTP(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihub-collaboration-http-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'Workspace'
        self.root.mkdir()
        self.data = self.base / 'App/data'
        self.data.mkdir(parents=True)
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True, 'server': {'port': 8765}}
        for target, name, value in [(config, 'DATA_DIR', str(self.data)), (server, 'CFG', self.cfg), (server, 'DB_OBJ', None)]:
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        class QuietHandler(server.Handler):
            def log_message(self, *_args):
                pass
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.cfg['server']['port'] = self.http.server_address[1]
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def request(self, path, body=None):
        req = urllib.request.Request('http://127.0.0.1:%s%s' % (self.http.server_address[1], path),
            data=None if body is None else json.dumps(body).encode('utf-8'), headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def call(self, action, body=None, mcp=False):
        code, result = self.request('/api/collaboration/' + ('mcp/' if mcp else '') + action, body or {})
        self.assertEqual(code, 200, result)
        return result

    def test_real_stdio_chinese_report_memory_review_and_cross_harness_handoff(self):
        code, registered = self.request('/api/harnesses/save', {
            'id': 'studio-cli', 'name': 'Synthetic Studio', 'revision': 0,
            '_workspace_root': str(self.root), 'connection_mode': 'mcp_stdio'})
        self.assertEqual(code, 200, registered)
        self.assertFalse(registered['invocation_verified'])
        stderr = tempfile.TemporaryFile()
        self.addCleanup(stderr.close)
        script = Path(__file__).resolve().parents[1] / 'tools/aihub_mcp.py'
        proc = subprocess.Popen([sys.executable, '-B', str(script), '--port', str(self.http.server_address[1]),
                                 '--client-id', 'studio-integration', '--tool', 'studio-cli'],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr)
        def end_bridge():
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            proc.stdin.close()
            proc.stdout.close()
        self.addCleanup(end_bridge)
        messages = queue.Queue()
        def read():
            for line in iter(proc.stdout.readline, b''):
                messages.put(json.loads(line))
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        serial = 0
        def rpc(method, params, notification=False):
            nonlocal serial
            serial += 1
            message = {'jsonrpc': '2.0', 'method': method, 'params': params}
            if not notification:
                message['id'] = serial
            proc.stdin.write(json.dumps(message, ensure_ascii=False).encode('utf-8') + b'\n')
            proc.stdin.flush()
            if notification:
                return
            reply = messages.get(timeout=15)
            self.assertEqual(reply['id'], serial)
            self.assertNotIn('error', reply, reply)
            return reply['result']
        def tool(name, arguments):
            result = rpc('tools/call', {'name': 'aihub_' + name, 'arguments': arguments})
            self.assertFalse(result.get('isError'), result)
            return json.loads(result['content'][0]['text'])
        init = rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'synthetic-codex', 'version': '1'}})
        self.assertEqual(init['protocolVersion'], '2025-11-25')
        rpc('notifications/initialized', {}, True)
        tools = rpc('tools/list', {})['tools']
        self.assertNotIn('aihub_retention_run', [t['name'] for t in tools])
        task = tool('task_create', {'project': '中文协作', 'title': '报告与交接验收'})
        claim = tool('task_claim', {'task_id': task['id']})
        owner = {'task_id': task['id'], 'lease_token': claim['lease_token']}
        report = tool('artifact_write', {**owner, 'kind': 'report', 'title': '正式验收', 'filename': '验收.md', 'content': '# 中文报告\n可追溯结论。'})
        self.assertEqual(Path(report['path']).read_text(encoding='utf-8'), '# 中文报告\n可追溯结论。')
        candidate = tool('memory_propose', {'title': '共享约定', 'content': '确认后的稳定结论', 'scope': 'workspace', 'source_artifact_id': report['id']})
        self.assertEqual(tool('memory_search', {'query': '稳定'})['items'], [])
        code, denied = self.request('/api/collaboration/mcp/memory_review', {'memory_id': candidate['id'], 'status': 'approved'})
        self.assertEqual(code, 403)
        self.call('memory_review', {'memory_id': candidate['id'], 'status': 'approved'})
        self.assertEqual(len(tool('memory_search', {'query': '稳定'})['items']), 1)
        tool('task_handoff', {**owner, 'target_tool': 'workbuddy', 'summary': '请复核已登记报告'})
        self.call('client_heartbeat', {'client_id': 'wb-integration', 'tool': 'workbuddy', 'name': 'synthetic WorkBuddy', 'protocol_version': 1}, True)
        wb = self.call('task_claim', {'task_id': task['id'], 'client_id': 'wb-integration'}, True)
        self.call('task_finish', {'task_id': task['id'], 'client_id': 'wb-integration', 'lease_token': wb['lease_token'], 'summary': '交接闭环通过'}, True)
        code, status = self.request('/api/collaboration/status')
        self.assertEqual(code, 200)
        self.assertEqual(status['tasks'][0]['status'], 'completed')
        self.assertNotIn(claim['lease_token'], json.dumps(status))
        self.assertTrue(status['policy']['enabled'])
        evidence = next(v for v in status['tools'] if v['id'] == 'studio-cli')
        self.assertTrue(evidence['recent_heartbeat'])
        self.assertTrue(evidence['invocation_verified'])
        self.assertEqual(evidence['verification_scope'], 'successful_aihub_protocol_call_only')
        self.assertEqual(status['mcp_config']['mcpServers']['aihub']['args'][2], str(self.http.server_address[1]))
        end_bridge()
        reader.join(3)
        stderr.seek(0)
        self.assertNotIn(claim['lease_token'].encode(), stderr.read())

    def test_automatic_recycling_rechecks_and_does_not_touch_reports(self):
        self.call('client_heartbeat', {'client_id': 'test', 'tool': 'dsh', 'name': 'test', 'protocol_version': 1})
        task = self.call('task_create', {'project': 'retention', 'title': 'cleanup'})
        claim = self.call('task_claim', {'task_id': task['id'], 'client_id': 'test'})
        owner = {'task_id': task['id'], 'client_id': 'test', 'lease_token': claim['lease_token']}
        temp = self.call('artifact_write', {**owner, 'kind': 'temp', 'title': '临时', 'filename': 'scratch.txt', 'content': 'scratch'})
        report = self.call('artifact_write', {**owner, 'kind': 'report', 'title': '报告', 'filename': 'report.md', 'content': 'keep'})
        self.call('task_finish', {**owner, 'summary': 'done'})
        with core.store(self.cfg) as (con, root):
            con.execute("UPDATE artifacts SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (temp['id'],))
        observed = []
        recycle_target = self.base / 'synthetic-recycle'
        recycle_target.mkdir()
        def fake_recycle(path):
            observed.append(path)
            Path(path).rename(recycle_target / Path(path).name)
        with mock.patch.object(maintenance.recycle, 'recycle_file', side_effect=fake_recycle):
            result = maintenance.run_retention(self.cfg, scheduled=True)
        self.assertEqual(result['recycled'], 1, result)
        self.assertEqual(observed, [temp['path']])
        self.assertEqual(Path(report['path']).read_text(), 'keep')
        self.assertTrue((recycle_target / 'scratch.txt').exists())
        self.assertEqual(self.call('artifact_list', {'kind': 'temp'})['items'][0]['status'], 'recycled')

    def test_database_errors_are_json_and_mcp_source_list_is_allowed(self):
        with mock.patch.object(collaboration_api, 'execute', side_effect=sqlite3.OperationalError('private-db-path')):
            code, result = self.request('/api/collaboration/task_create', {'project': 'test', 'title': 'test'})
        self.assertEqual(code, 503)
        self.assertNotIn('private-db-path', json.dumps(result))
        with mock.patch.object(collaboration_api, 'status', side_effect=sqlite3.DatabaseError('private-db-path')):
            code, result = self.request('/api/collaboration/status')
        self.assertEqual(code, 503)
        self.assertNotIn('private-db-path', json.dumps(result))
        code, _ = self.request('/api/collaboration/mcp/source_list', {})
        self.assertNotEqual(code, 200)
        self.call('client_heartbeat', {'client_id': 'source-reader', 'tool': 'codex',
            'name': 'Source reader', 'protocol_version': 1}, mcp=True)
        self.assertEqual(self.call('source_list', {'client_id': 'source-reader'}, mcp=True)['items'], [])

    def test_stale_workspace_and_snapshot_never_mix_roots(self):
        code, _ = self.request('/api/collaboration/task_create', {'project': 'demo', 'title': 'demo', '_workspace_root': str(self.base / 'OldWorkspace')})
        self.assertEqual(code, 400)
        self.assertFalse((self.root / '40_Projects').exists())
        old = dict(self.cfg)
        def concurrent_switch(snapshot):
            self.cfg['ai_root'] = str(self.base / 'OtherWorkspace')
            return {'available': True, 'root': snapshot['ai_root']}
        with mock.patch.object(core, 'status', side_effect=concurrent_switch):
            result = collaboration_api.status(self.cfg)
        self.assertEqual(result['root'], old['ai_root'])
        self.assertEqual(result['sources'], [])


if __name__ == '__main__':
    unittest.main()
