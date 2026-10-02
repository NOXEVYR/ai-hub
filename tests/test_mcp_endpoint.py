"""Real loopback tests for an explicitly selected installation, never port scans."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from tools.aihub_mcp import Bridge, BridgeError
from tools.mcp_endpoint import EndpointError, resolve_endpoint


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.server.health_count += 1
        body = self.server.health
        if self.server.on_health:
            self.server.on_health()
        self.send_response(self.server.health_status)
        if self.server.health_status == 302:
            self.send_header('Location', 'http://example.invalid/never-follow')
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.posts.append((self.path, body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({'workspace_root': str(self.server.root / 'workspace'),
                                    'items': []}).encode())


class InstallationEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mcp-installation-fixture-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'app'
        (self.root / 'data').mkdir(parents=True)
        self.path = self.root / 'data/config.json'
        self.servers = []
        self.first, self.second = self.server(), self.server()
        self.config(self.first.server_port)

    def server(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.root, server.posts = self.root, []
        server.health_count, server.health_status, server.on_health = 0, 200, None
        server.health = {'app': 'ai-hub', 'control_protocol': 'ai-hub-local-control-v1',
                         'mcp_endpoint_binding': 'aihub-mcp-endpoint/1',
                         'service_instance_id': 'synthetic-instance', 'install_root': str(self.root)}
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        self.servers.append(server)
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.addCleanup(stop)
        return server

    def config(self, port):
        self.path.write_text(json.dumps({'server': {'port': port}, 'private_api_key': 'SYNTHETIC_NEVER_TRANSMIT'}), encoding='utf-8')

    def test_bridge_tracks_config_port_but_never_sends_private_config(self):
        bridge = Bridge(8765, 'fixture-client', 'codex', install_root=str(self.root))
        bridge.request('client_heartbeat', {})
        self.config(self.second.server_port)
        bridge.request('task_list', {})
        self.assertEqual(len(self.first.posts), 1)
        self.assertEqual(len(self.second.posts), 1)
        self.assertEqual(bridge.port, self.second.server_port)
        self.assertEqual(self.second.posts[0][1]['_workspace_root'], str(self.root / 'workspace'))
        self.assertNotIn('SYNTHETIC_NEVER_TRANSMIT', json.dumps(self.first.posts + self.second.posts))

    def test_wrong_installation_redirect_invalid_health_send_no_operations(self):
        bridge = Bridge(8765, 'fixture-client', 'codex', install_root=str(self.root))
        original = dict(self.first.health)
        for wrong in ('different-app', r'\\untrusted.invalid\share\app'):
            self.first.health = dict(original, install_root=wrong)
            with self.assertRaises(BridgeError) as caught:
                bridge.request('task_list', {})
            self.assertEqual(caught.exception.error_code, 'endpoint_identity_invalid')
        self.first.health = original
        for code in (302, 401, 503):
            self.first.health_status = code
            with self.assertRaises(EndpointError):
                resolve_endpoint(self.root)
        self.first.health_status = 200
        for raw in (b'{bad-json', b' ' * 65537):
            self.first.health = raw
            with self.assertRaises(EndpointError):
                resolve_endpoint(self.root)
        self.assertEqual(self.first.posts, [])
        self.assertEqual(self.second.health_count, 0)

    def test_config_changed_during_health_does_not_dispatch_to_old_port(self):
        self.first.on_health = lambda: self.config(self.second.server_port)
        bridge = Bridge(8765, 'fixture-client', 'codex', install_root=str(self.root))
        with self.assertRaises(BridgeError) as caught:
            bridge.request('task_create', {'project': 'Fixture', 'title': 'synthetic'})
        self.assertEqual(caught.exception.error_code, 'endpoint_config_changed')
        self.assertEqual(self.first.posts + self.second.posts, [])

    def test_invalid_config_has_no_fallback_or_private_error_output(self):
        for value in (True, 0, 65536, '8765', None):
            self.config(value)
            with self.assertRaises(EndpointError) as caught:
                resolve_endpoint(self.root)
            self.assertEqual(caught.exception.code, 'endpoint_config_invalid')
            self.assertNotIn('SYNTHETIC_NEVER_TRANSMIT', str(caught.exception))
        for raw in ('{"server":{"port":1234,"port":4321}}', '{broken', '{"server":null}'):
            self.path.write_text(raw, encoding='utf-8')
            with self.assertRaises(EndpointError):
                resolve_endpoint(self.root)
        self.assertTrue(all(server.health_count == 0 for server in self.servers))

    def test_hardlinked_configuration_is_refused(self):
        alias = self.path.with_name('alias.json')
        os.link(self.path, alias)
        with self.assertRaises(EndpointError):
            resolve_endpoint(self.root)
        self.assertEqual(self.first.health_count, 0)

    def test_bound_bridge_refuses_old_backend_before_business_post(self):
        self.first.health.pop('mcp_endpoint_binding')
        bridge = Bridge(8765, 'fixture-client', 'codex', install_root=str(self.root))
        with self.assertRaises(BridgeError) as caught:
            bridge.request('task_finish', {'lease_token': 'synthetic-lease'})
        self.assertEqual(caught.exception.error_code, 'endpoint_identity_invalid')
        self.assertEqual(self.first.posts, [])
        self.assertEqual(resolve_endpoint(self.root), self.first.server_port)

    def test_actual_stdio_cli_uses_selected_installation(self):
        script = Path(__file__).resolve().parents[1] / 'tools/aihub_mcp.py'
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25',
                'capabilities': {}, 'clientInfo': {'name': 'Fixture', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'aihub_task_list', 'arguments': {}}}]
        wire = '\n'.join(json.dumps(item) for item in messages) + '\n'
        result = subprocess.run([sys.executable, '-B', str(script), '--install-root', str(self.root),
            '--client-id', 'fixture-cli', '--tool', 'codex'], input=wire.encode(), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertFalse(replies[-1]['result'].get('isError', False))
        self.assertEqual([path for path, _ in self.first.posts],
                         ['/api/collaboration/mcp/client_heartbeat', '/api/collaboration/mcp/task_list'])
        self.assertNotIn(b'SYNTHETIC_NEVER_TRANSMIT', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
