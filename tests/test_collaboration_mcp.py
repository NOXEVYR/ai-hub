"""Exercise the real stdio subprocess against a disposable local HTTP server."""
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from tools.aihub_mcp import Bridge, MAX_LINE, serve

SCRIPT = Path(__file__).resolve().parents[1] / 'tools' / 'aihub_mcp.py'


def request(identifier, method, params=None):
    return {'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params or {}}


def initialization(version='2025-11-25'):
    return [request(1, 'initialize', {'protocolVersion': version, 'capabilities': {},
                                    'clientInfo': {'name': 'Test', 'version': '1'}}),
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'}]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append((self.path, data))
        if self.path.endswith('task_claim'):
            result = {'lease_token': 'secret-lease', 'task': {'id': data['task_id'], 'title': '中文协作'}}
        elif self.path.endswith('memory_search'):
            result = {'items': [{'status': 'approved', 'content': '用户审核的长期记忆'}]}
        else:
            result = {'ok': True, 'received': data}
        self.send_response(self.server.reply_status)
        if self.server.reply_status == 302:
            self.send_header('Location', 'http://example.invalid/leak')
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))


class MCPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.reply_status = 200
        cls.server.calls = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def setUp(self):
        self.server.calls.clear()
        self.server.reply_status = 200

    def run_stdio(self, messages, extra=()):
        wire = '\n'.join(json.dumps(item, ensure_ascii=False) if not isinstance(item, str)
                         else item for item in messages) + '\n'
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), '--port',
                                 str(self.server.server_port), '--client-id', 'test-codex',
                                 '--tool', 'codex', *extra], input=wire.encode('utf-8'),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
        return [json.loads(line) for line in result.stdout.splitlines()], result.stderr.decode('utf-8')

    def test_protocol_negotiation_and_ready_gate(self):
        out, err = self.run_stdio([request(0, 'tools/list'), *initialization('2099-01-01'),
                                  request(2, 'tools/list'), request(3, 'ping')])
        self.assertEqual(out[0]['error']['code'], -32002)
        self.assertEqual(out[1]['result']['protocolVersion'], '2025-11-25')
        self.assertIn('tools', out[2]['result'])
        self.assertEqual(out[3]['result'], {})
        self.assertEqual(err, '')
        for version in ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'):
            out, _ = self.run_stdio(initialization(version))
            self.assertEqual(out[0]['result']['protocolVersion'], version)

    def test_chinese_roundtrip_claim_and_identity(self):
        out, err = self.run_stdio([*initialization(),
            request(2, 'tools/call', {'name': 'aihub_task_claim', 'arguments': {'task_id': '中文任务'}}),
            request(3, 'tools/call', {'name': 'aihub_artifact_write', 'arguments': {
                'task_id': '中文任务', 'lease_token': 'secret-lease', 'kind': 'report',
                'title': '验收报告', 'filename': '报告.md', 'content': '第一行\n第二行'}})])
        self.assertFalse(out[1]['result']['isError'])
        self.assertEqual(json.loads(out[1]['result']['content'][0]['text'])['lease_token'], 'secret-lease')
        self.assertEqual(len(self.server.calls), 3)
        self.assertEqual(self.server.calls[0][0], '/api/collaboration/mcp/client_heartbeat')
        self.assertEqual(self.server.calls[0][1]['tool'], 'codex')
        self.assertEqual(self.server.calls[2][1]['content'], '第一行\n第二行')
        self.assertTrue(all(data['client_id'] == 'test-codex' for _, data in self.server.calls))
        self.assertNotIn('secret-lease', err)
        self.assertNotIn('secret-lease', json.dumps(out[0]))

    def test_forbidden_and_impersonating_arguments_never_reach_http(self):
        calls = [request(i + 2, 'tools/call', {'name': 'aihub_' + action, 'arguments': {}})
                 for i, action in enumerate(('memory_review', 'artifact_pin', 'retention_policy',
                                             'retention_run', 'source_add', 'source_scan', 'memory_list'))]
        calls.append(request(20, 'tools/call', {'name': 'aihub_task_claim',
                     'arguments': {'task_id': '1', 'client_id': 'other-client'}}))
        out, _ = self.run_stdio([*initialization(), *calls])
        self.assertTrue(all(item['error']['code'] == -32602 for item in out[1:]))
        self.assertEqual(self.server.calls, [])

    def test_resource_allowlist_and_approved_memory(self):
        out, _ = self.run_stdio([*initialization(), request(2, 'resources/list'),
            request(3, 'resources/read', {'uri': 'aihub://collaboration/guide'}),
            request(4, 'resources/read', {'uri': 'aihub://memory/approved'}),
            request(5, 'resources/read', {'uri': 'file:///C:/secret'}),
            request(6, 'resources/read', {'uri': 'http://example.invalid'})])
        self.assertEqual(len(out[1]['result']['resources']), 2)
        self.assertIn('pull queue', out[2]['result']['contents'][0]['text'])
        self.assertIn('用户审核', out[3]['result']['contents'][0]['text'])
        self.assertEqual(out[4]['error']['code'], -32002)
        self.assertEqual(out[5]['error']['code'], -32002)
        self.assertEqual([path for path, _ in self.server.calls],
                         ['/api/collaboration/mcp/client_heartbeat', '/api/collaboration/mcp/memory_search'])

    def test_http_failure_diagnostics_and_no_redirect(self):
        for status in (302, 403, 500):
            self.server.reply_status = status
            out, err = self.run_stdio([*initialization(), request(2, 'tools/call',
                                       {'name': 'aihub_task_list'})])
            self.assertTrue(out[1]['result']['isError'])
            self.assertIn('HTTP %s' % status, out[1]['result']['content'][0]['text'])
            self.assertNotIn('secret-lease', err)
            self.assertNotIn('example.invalid', str(out))

    def test_offline_tools_list_available_but_call_fails(self):
        bridge = Bridge(self.server.server_port, 'test', 'dsh')
        for message in initialization():
            bridge.handle(message)
        with patch('tools.aihub_mcp.http.client.HTTPConnection') as factory:
            factory.return_value.request.side_effect = OSError('private failure')
            reply = bridge.handle(request(2, 'tools/call', {'name': 'aihub_task_list'}))
        self.assertTrue(reply['result']['isError'])
        self.assertIn('start AI Hub', reply['result']['content'][0]['text'])
        self.assertNotIn('private failure', str(reply))
        self.assertIsNone(bridge.last_heartbeat)

    def test_jsonrpc_errors_notifications_and_invalid_arguments(self):
        out, _ = self.run_stdio(['{bad', [], request(None, 'ping'), *initialization(),
            {'jsonrpc': '2.0', 'method': 'notifications/anything'},
            {'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': 'aihub_task_create'}},
            request(3, 'not-a-method'), request(4, 'tools/call', {'name': 'aihub_task_claim'}),
            request(5, 'tools/call', {'name': 'aihub_task_create', 'arguments': {
                'project': 'demo', 'title': 3}}), request(6, 'tools/list', {'cursor': 'anything'})])
        self.assertEqual([item['error']['code'] for item in out if 'error' in item],
                         [-32700, -32600, -32600, -32601, -32602, -32602, -32602])
        self.assertEqual(self.server.calls, [])

    def test_size_limit_closes_without_parsing_or_stdout_noise(self):
        output = io.BytesIO()
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            code = serve(Bridge(8765, 'test', 'zcode'), io.BytesIO(b'x' * (MAX_LINE + 1)), output)
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), b'')
        self.assertIn('limit', stderr.getvalue())

    def test_port_client_tool_validation_and_no_url_option(self):
        for values in ((0, 'test', 'codex'), (65536, 'test', 'codex'),
                       (8765, '../other', 'codex'), (8765, 'test', 'other')):
            with self.assertRaises(ValueError):
                Bridge(*values)
        result = subprocess.run([sys.executable, str(SCRIPT), '--client-id', 'test', '--tool', 'codex',
                                 '--url', 'http://example.invalid'], capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')


if __name__ == '__main__':
    unittest.main()
