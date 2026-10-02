"""Interop MCP reads against synthetic HTTP, without client/store initialization."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from tools.aihub_mcp import Bridge, BY_NAME, RPCError, validate

REVISION = 'a' * 64
CAPABILITY = 'b' * 32
FINGERPRINT = 'c' * 64


def message(identifier, name, arguments=None):
    return {'jsonrpc': '2.0', 'id': identifier, 'method': 'tools/call',
            'params': {'name': 'aihub_' + name, 'arguments': arguments or {}}}


class InteropMCPTests(unittest.TestCase):
    def setUp(self):
        owner = self
        self.calls = []
        self.root = 'C:/synthetic/Workspace'
        self.revision = REVISION
        self.override, self.status = None, 200

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                arguments = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                action = self.path.rsplit('/', 1)[-1]
                owner.calls.append((action, arguments))
                result = {'protocol': 'aihub-interop/1', 'workspace_root': owner.root,
                          'connection_revision': owner.revision}
                if action == 'interop_capability_snapshot':
                    result['declaration'] = {'origin': 'stored_normalized_declaration', 'sha256': FINGERPRINT}
                elif action == 'task_list':
                    result = {'items': [], 'workspace_root': owner.root}
                elif action == 'client_heartbeat':
                    result = {'workspace_root': owner.root}
                if owner.override is not None:
                    result = owner.override
                self.send_response(owner.status)
                self.end_headers()
                self.wfile.write(json.dumps(result).encode('utf-8'))

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.bridge = Bridge(self.http.server_port, 'interop-client', 'codex')
        self.bridge.handle({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'Synthetic', 'version': '1'}}})
        self.bridge.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def stop(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def read_result(self, reply):
        self.assertNotIn('error', reply)
        self.assertFalse(reply['result']['isError'], reply)
        return json.loads(reply['result']['content'][0]['text'])

    def test_read_annotations_and_no_heartbeat_or_outbox_side_effects(self):
        for name in ('interop_describe', 'interop_capability_snapshot'):
            self.assertTrue(BY_NAME['aihub_' + name]['annotations']['readOnlyHint'])
        with patch.object(self.bridge, 'heartbeat', side_effect=AssertionError('heartbeat called')), \
                patch('tools.aihub_mcp.ReportOutbox', side_effect=AssertionError('outbox created')):
            description = self.read_result(self.bridge.handle(message(1, 'interop_describe')))
            snapshot = self.read_result(self.bridge.handle(message(2, 'interop_capability_snapshot', {
                'capability_id': CAPABILITY, 'connection_revision': REVISION,
                'expected_declaration_sha256': FINGERPRINT})))
        self.assertEqual(description['protocol'], 'aihub-interop/1')
        self.assertEqual(snapshot['declaration']['sha256'], FINGERPRINT)
        self.assertEqual([action for action, _ in self.calls], ['interop_describe', 'interop_capability_snapshot'])
        self.assertEqual(self.calls[1][1], {'capability_id': CAPABILITY, 'connection_revision': REVISION,
            'expected_declaration_sha256': FINGERPRINT, 'client_id': 'interop-client', '_workspace_root': self.root})
        self.assertIsNone(self.bridge.last_heartbeat)
        self.assertIsNone(self.bridge.outbox)
        self.assertEqual(self.bridge.leases, {})

    def test_first_snapshot_reads_description_to_bind_without_registration(self):
        result = self.read_result(self.bridge.handle(message(1, 'interop_capability_snapshot', {
            'capability_id': CAPABILITY, 'connection_revision': REVISION})))
        self.assertEqual(result['workspace_root'], self.root)
        self.assertEqual(self.bridge.workspace_root, self.root)
        self.assertEqual([action for action, _ in self.calls], ['interop_describe', 'interop_capability_snapshot'])
        self.assertNotIn('_workspace_root', self.calls[0][1])
        self.assertEqual(self.calls[1][1]['_workspace_root'], self.root)
        self.assertIsNone(self.bridge.last_heartbeat)

    def test_empty_workspace_description_is_valid_but_snapshot_is_not_dispatchable(self):
        self.root, self.revision = '', None
        result = self.read_result(self.bridge.handle(message(1, 'interop_describe')))
        self.assertEqual(result['workspace_root'], '')
        self.assertIsNone(result['connection_revision'])
        self.assertIsNone(self.bridge.workspace_root)
        reply = self.bridge.handle(message(2, 'interop_capability_snapshot', {
            'capability_id': CAPABILITY, 'connection_revision': REVISION}))
        self.assertTrue(reply['result']['isError'])
        self.assertEqual([action for action, _ in self.calls], ['interop_describe', 'interop_describe'])
        self.assertIsNone(self.bridge.last_heartbeat)

    def test_workspace_switch_cannot_rebind_existing_session(self):
        self.read_result(self.bridge.handle(message(1, 'interop_describe')))
        previous = self.bridge.workspace_root
        self.root = 'D:/synthetic/OtherWorkspace'
        for tool, args in [('interop_describe', {}), ('interop_capability_snapshot', {
                'capability_id': CAPABILITY, 'connection_revision': REVISION})]:
            response = self.bridge.handle(message(2, tool, args))
            self.assertTrue(response['result']['isError'])
            self.assertIn('workspace changed', response['result']['content'][0]['text'])
            self.assertEqual(self.calls[-1][1]['_workspace_root'], previous)
            self.assertEqual(self.bridge.workspace_root, previous)

    def test_invalid_protocol_and_revision_are_safe_without_private_response_echo(self):
        for fields in ({'protocol': 'private-invalid-protocol'}, {'workspace_root': ['private-invalid-root']},
                       {'connection_revision': 'private-invalid-revision'}, {'protocol': None}):
            self.override = {'protocol': 'aihub-interop/1', 'workspace_root': self.root,
                             'connection_revision': REVISION, 'private_detail': 'private-fixture-history', **fields}
            reply = self.bridge.handle(message(1, 'interop_describe'))
            self.assertTrue(reply['result']['isError'])
            self.assertNotIn('private-', str(reply))
            self.assertIsNone(self.bridge.workspace_root)
        self.override = None
        self.override = {'protocol': 'aihub-interop/1', 'workspace_root': self.root}
        reply = self.bridge.handle(message(1, 'interop_describe'))
        self.assertTrue(reply['result']['isError'])
        self.assertIsNone(self.bridge.workspace_root)
        self.override = None
        self.read_result(self.bridge.handle(message(2, 'interop_describe')))
        self.revision = 'd' * 64
        reply = self.bridge.handle(message(3, 'interop_capability_snapshot', {
            'capability_id': CAPABILITY, 'connection_revision': REVISION}))
        self.assertTrue(reply['result']['isError'])
        self.assertIn('connection revision changed', reply['result']['content'][0]['text'])

    def test_caller_identity_override_and_invalid_fingerprints_never_reach_http(self):
        arguments = {'capability_id': CAPABILITY, 'connection_revision': REVISION}
        for fields in ({'service_instance_id': 'spoof'}, {'install_root': 'C:/spoof'},
                       {'client_id': 'other'}, {'_workspace_root': 'C:/spoof'},
                       {'expected_declaration_sha256': '../bad'}, {'connection_revision': ''},
                       {'capability_id': 'not-an-id'}):
            reply = self.bridge.handle(message(1, 'interop_capability_snapshot', dict(arguments, **fields)))
            self.assertEqual(reply['error']['code'], -32602)
        with self.assertRaises(RPCError):
            validate(BY_NAME['aihub_interop_describe']['inputSchema'], {'service_instance_id': 'spoof'})
        self.assertEqual(self.calls, [])

    def test_legacy_tools_keep_default_heartbeat_and_report_schema(self):
        self.read_result(self.bridge.handle(message(1, 'interop_describe')))
        self.read_result(self.bridge.handle(message(2, 'task_list')))
        self.assertEqual([action for action, _ in self.calls], ['interop_describe', 'client_heartbeat', 'task_list'])
        self.assertIsNotNone(self.bridge.last_heartbeat)
        self.assertIn('lease_token', BY_NAME['aihub_report_submit']['inputSchema']['required'])
        self.assertIn('memory_candidates', BY_NAME['aihub_report_submit']['inputSchema']['required'])
        self.assertTrue(BY_NAME['aihub_submission_receipt']['annotations']['readOnlyHint'])


if __name__ == '__main__':
    unittest.main()
