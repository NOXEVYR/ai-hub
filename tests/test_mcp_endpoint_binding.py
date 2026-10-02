"""Real TCP admission checks on disposable installations, without real stores."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

import server
from tools.aihub_mcp import Bridge, BridgeError
from tools.mcp_endpoint import PROTOCOL, installation_identity


INSTALL = 'X-AIHub-Expected-Install'
INSTANCE = 'X-AIHub-Expected-Instance'


class PublicControl:
    """Only public synthetic metadata; this fixture has no control credential."""
    def __init__(self, root):
        self.identity = {'app': 'ai-hub', 'control_protocol': 'ai-hub-local-control-v1',
                         'install_root': str(root), 'service_instance_id': 'fixture-' + uuid.uuid4().hex}

    def public_identity(self):
        return dict(self.identity)


class BodyReads:
    def __init__(self, stream, fixture):
        self.stream, self.fixture = stream, fixture

    def read(self, *args):
        self.fixture.body_reads += 1
        return self.stream.read(*args)

    def read1(self, *args):
        self.fixture.body_reads += 1
        return self.stream.read1(*args)

    def __getattr__(self, name):
        return getattr(self.stream, name)


class LoopbackFixture:
    def __init__(self, root, port=0):
        self.root, self.posts, self.body_reads = root, [], 0
        fixture = self

        class Handler(server.Handler):
            def log_message(self, *_args):
                pass

            def setup(self):
                super().setup()
                self.rfile = BodyReads(self.rfile, fixture)

            def _dispatch(self, method, parsed, body=None):
                # Real Handler.do_POST still performs identity admission and JSON
                # reading. Accepted dispatch never touches application state.
                if method == 'GET' and parsed.path == '/api/health':
                    self._json(200, {**fixture.control.public_identity(),
                                     'mcp_endpoint_binding': PROTOCOL})
                    return
                fixture.posts.append((parsed.path, body, dict(self.headers)))
                self._json(200, {'ok': True, 'workspace_root': str(root.parent / 'shared-workspace')})

        self.http = server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.port = self.http.server_address[1]
        self.control = PublicControl(root)
        self.http.service_control = self.control
        self.thread = threading.Thread(target=self.http.serve_forever,
                                       kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def stop(self):
        if self.http is not None:
            self.http.shutdown()
            self.http.server_close()
            self.thread.join(3)
            self.http = None


class EndpointBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mcp-binding-owned-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / '中文 安装'
        (self.root / 'data').mkdir(parents=True)
        self.fixture = self.start(self.root)
        (self.root / 'data/config.json').write_text(json.dumps({'server': {'port': self.fixture.port}}), encoding='utf-8')

    def start(self, root, port=0):
        fixture = LoopbackFixture(root, port)
        self.addCleanup(fixture.stop)
        return fixture

    def bindings(self):
        return [(INSTALL, installation_identity(self.root)),
                (INSTANCE, self.fixture.control.identity['service_instance_id'])]

    def post(self, headers=(), send_body=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.fixture.port, timeout=3)
        payload = b'{"client_id":"fixture","lease_token":"SYNTHETIC_LEASE_ONLY"}'
        try:
            connection.putrequest('POST', '/api/collaboration/mcp/task_finish')
            connection.putheader('Content-Type', 'application/json')
            connection.putheader('Content-Length', str(len(payload) if send_body else 1024 * 1024))
            for key, value in headers:
                connection.putheader(key, value)
            # The incomplete-body case proves the server replies without waiting
            # for or draining business data, rather than merely skipping dispatch.
            connection.endheaders(payload if send_body else None)
            response = connection.getresponse()
            return response.status, response.getheader('X-AIHub-Endpoint-Rejected'), json.loads(response.read())
        finally:
            connection.close()

    def test_selected_unicode_installation_sends_only_ascii_public_binding(self):
        result = Bridge(8765, 'fixture-client', 'codex', install_root=str(self.root)).request('task_list', {})
        self.assertTrue(result['ok'])
        self.assertEqual(len(self.fixture.posts), 1)
        headers = self.fixture.posts[0][2]
        self.assertEqual(headers[INSTALL], installation_identity(self.root))
        self.assertEqual(headers[INSTANCE], self.fixture.control.identity['service_instance_id'])
        self.assertTrue(headers[INSTALL].isascii())
        self.assertNotIn('X-AIHub-Control-Token', headers)
        self.assertNotIn('Authorization', headers)

    def test_other_installation_reusing_validated_port_is_rejected_before_body_read(self):
        self.port_takeover(self.base / 'other-installation')

    def test_same_installation_restarted_between_health_and_post_is_rejected(self):
        self.port_takeover(self.root)

    def port_takeover(self, replacement_root):
        original = http.client.HTTPConnection
        first, replacements, calls = self.fixture, [], 0

        def connection(host, port, timeout):
            nonlocal calls
            calls += 1
            if calls == 2:
                # Deterministically replace only this fixture's listener after its
                # completed health check and before the business connection opens.
                first.stop()
                replacement_root.mkdir(exist_ok=True)
                replacements.append(self.start(replacement_root, port))
            return original(host, port, timeout=timeout)

        bridge = Bridge(8765, 'fixture-client', 'codex', install_root=str(self.root))
        with patch('http.client.HTTPConnection', side_effect=connection):
            with self.assertRaises(BridgeError) as caught:
                bridge.request('task_finish', {'task_id': 'synthetic-task', 'lease_token': 'SYNTHETIC_LEASE_ONLY'})
        self.assertEqual(caught.exception.error_code, 'endpoint_identity_invalid')
        self.assertEqual(len(replacements), 1)
        self.assertEqual(replacements[0].posts, [])
        self.assertEqual(replacements[0].body_reads, 0)
        self.assertEqual(first.posts, [])
        self.assertEqual(first.body_reads, 0)

    def test_partial_duplicate_and_mismatched_binding_do_not_read_business_body(self):
        install, instance = self.bindings()
        cases = [(install,), (instance,), (install, install, instance),
                 (install, instance, instance), ((INSTALL, '0' * 64), instance),
                 (install, (INSTANCE, 'wrong-instance'))]
        for headers in cases:
            with self.subTest(kind=tuple(key for key, _ in headers)):
                status, marker, body = self.post(headers)
                self.assertEqual((status, marker), (409, '1'))
                self.assertEqual(body['code'], 'endpoint_identity_invalid')
                self.assertEqual(self.fixture.posts, [])
                self.assertEqual(self.fixture.body_reads, 0)

    def test_mismatched_binding_replies_without_receiving_declared_body(self):
        status, marker, _ = self.post([(INSTALL, '0' * 64), self.bindings()[1]], send_body=False)
        self.assertEqual((status, marker), (409, '1'))
        self.assertEqual(self.fixture.body_reads, 0)
        self.assertEqual(self.fixture.posts, [])

    def test_explicit_port_bridge_remains_legacy_without_binding_headers(self):
        result = Bridge(self.fixture.port, 'fixture-client', 'codex').request('task_list', {})
        self.assertTrue(result['ok'])
        self.assertEqual(len(self.fixture.posts), 1)
        headers = self.fixture.posts[0][2]
        self.assertNotIn(INSTALL, headers)
        self.assertNotIn(INSTANCE, headers)


if __name__ == '__main__':
    unittest.main()
