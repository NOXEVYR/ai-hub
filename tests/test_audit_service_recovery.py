"""AUD-YH-03/05/06/07/08: temporary data and real loopback HTTP only."""
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock
import zipfile
import zlib

import server
from aihub import app_update as updater, registry, service_control
import test_organization_http as fixtures
from test_app_update import package_bytes, feed_value, wait_state


class RegistryAndBoundaryHTTP(unittest.TestCase):
    setUp = fixtures.OrganizationHTTP.setUp
    tearDown = fixtures.OrganizationHTTP.tearDown
    patch = fixtures.OrganizationHTTP.patch
    request = fixtures.OrganizationHTTP.request

    def test_registry_expected_errors_have_no_trace_and_preserve_corruption(self):
        for path in ('/api/registry', '/api/registry/backups'):
            status, value = self.request('GET', path)
            self.assertEqual(status, 400, value)
            self.assertIn('根目录', value['error'])
            self.assertNotIn('trace', value)
        self.cfg['ai_root'] = str(self.root)
        main = self.data / 'registry.json'
        backup = self.data / 'registry.previous.json'
        main.write_bytes(b'{bad-main')
        backup.write_bytes(b'{bad-backup')
        for path in ('/api/registry', '/api/registry/backups'):
            status, value = self.request('GET', path)
            self.assertEqual(status, 400, value)
            self.assertIn('保留', value['error'])
            self.assertNotIn('trace', value)
        self.assertEqual(main.read_bytes(), b'{bad-main')
        self.assertEqual(backup.read_bytes(), b'{bad-backup')
        backup.write_text(json.dumps(registry._empty()), encoding='utf-8')
        status, value = self.request('GET', '/api/registry')
        self.assertEqual(status, 200, value)
        self.assertTrue(value['warnings'])
        self.assertEqual(main.read_bytes(), b'{bad-main')

    def test_2_4_mib_post_returns_413_json_repeatedly(self):
        for _ in range(5):
            status, value = self.request('POST', '/api/registry/preview', raw=b'x' * int(2.4 * 1024 * 1024))
            self.assertEqual(status, 413)
            self.assertEqual(value['code'], 'request_too_large')
        self.assertEqual(self.request('GET', '/api/health')[0], 200)

    def test_huge_incomplete_upload_has_bounded_drain_deadline(self):
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as connection:
            started = time.monotonic()
            connection.sendall(('POST /api/registry/preview HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                                'Content-Length: 999999999999\r\n\r\nx' % self.port).encode())
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 413)
            self.assertEqual(json.loads(response.read())['code'], 'request_too_large')
            self.assertLess(time.monotonic() - started, 4)


class UpdateRecovery(unittest.TestCase):
    def test_corrupt_deflate_manifest_and_payload_are_invalid_update(self):
        for member in ('AI-Hub/AI Hub.exe', 'AI-Hub/manifest.json'):
            with self.subTest(member=member), tempfile.TemporaryDirectory() as tmp:
                blob = bytearray(package_bytes({'AI Hub.exe': b'fixture executable' * 10}))
                with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                    item = archive.getinfo(member)
                    name_len, extra_len = struct.unpack_from('<HH', blob, item.header_offset + 26)
                    offset = item.header_offset + 30 + name_len + extra_len
                # Reserved DEFLATE block type: valid ZIP/feed digest, broken compressed stream.
                blob[offset] = (blob[offset] & 0xf8) | 7
                blob = bytes(blob)
                with zipfile.ZipFile(io.BytesIO(blob)) as archive, self.assertRaises(zlib.error):
                    archive.read(member)
                feed = feed_value(blob)
                manager = updater.UpdateManager(tmp, '2.11.3', fetcher=lambda url, maximum: feed if url == updater.FEED_URL else blob)
                try:
                    manager.check()
                    available = wait_state(manager, 'available')
                    manager.download(available['release_id'])
                    result = wait_state(manager, 'failed')
                    self.assertEqual(result['error_code'], 'invalid_update')
                    self.assertIn('更新包', result['message'])
                finally:
                    manager.close()

    def test_real_local_http_404_and_connection_failure_remain_network_error(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Missing(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_error(404)
            def log_message(self, *_):
                pass
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Missing)
        worker = threading.Thread(target=httpd.serve_forever)
        worker.start()
        url = 'http://127.0.0.1:%s/missing.zip' % httpd.server_address[1]
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        feed = feed_value(package_bytes({'AI Hub.exe': b'fixture'}))
        def fetch(request_url, maximum):
            if request_url == updater.FEED_URL:
                return feed
            with opener.open(url, timeout=1) as response:
                return response.read()
        try:
            for stopped in (False, True):
                if stopped:
                    httpd.shutdown()
                    httpd.server_close()
                    worker.join(2)
                with tempfile.TemporaryDirectory() as tmp:
                    manager = updater.UpdateManager(tmp, '2.11.3', fetcher=fetch)
                    try:
                        manager.check()
                        available = wait_state(manager, 'available')
                        manager.download(available['release_id'])
                        self.assertEqual(wait_state(manager, 'failed')['error_code'], 'network_error')
                    finally:
                        manager.close()
        finally:
            if worker.is_alive():
                httpd.shutdown()
                httpd.server_close()
                worker.join(2)

    def test_dangling_marker_is_preserved_with_recovery_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / 'data/app-updates/install-lock.json'
            marker.parent.mkdir(parents=True)
            raw = json.dumps({'schema': updater.LOCK_SCHEMA, 'transaction_id': 'a' * 32}).encode()
            marker.write_bytes(raw)
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, '不要直接删除锁标记') as raised:
                    updater.startup_guard(root)
                self.assertIn('journal.json', str(raised.exception))
                self.assertEqual(marker.read_bytes(), raw)
                self.assertFalse((marker.parent / 'transactions').exists())


class RuntimeIdentity(unittest.TestCase):
    def test_exception_log_retains_location_without_exception_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = server.configure_runtime_log(tmp)
            try:
                try:
                    raise RuntimeError('private-token-must-not-be-logged')
                except RuntimeError:
                    server.log_runtime_exception()
                log = (Path(tmp) / 'service-runtime.log').read_text(encoding='utf-8')
                self.assertIn('exception=RuntimeError', log)
                self.assertIn('test_exception_log_retains_location', log)
                self.assertNotIn('private-token', log)
            finally:
                server.RUNTIME_LOG.removeHandler(handler)
                handler.close()

    def test_pid_cleanup_cannot_remove_a_newer_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = service_control.ServiceControl(tmp, Path(tmp) / 'data', 1234)
            new = service_control.ServiceControl(tmp, Path(tmp) / 'data', 1235)
            server.publish_pid(old)
            server.publish_pid(new)
            server.remove_own_pid(old)
            path = Path(tmp) / 'data/server.pid.json'
            self.assertEqual(json.loads(path.read_text())['instance_id'], new.record['instance_id'])
            server.remove_own_pid(new)
            self.assertFalse(path.exists())

    def test_direct_launch_logs_identity_requests_and_clean_stop(self):
        self.check_launch('direct')

    @unittest.skipUnless(os.name == 'nt', 'Windows desktop launcher')
    def test_desktop_launch_uses_same_identity_and_runtime_log(self):
        self.check_launch('desktop')

    def check_launch(self, mode):
        source = Path(server.__file__).resolve().parent
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copyfile(source / 'server.py', root / 'server.py')
            shutil.copytree(source / 'aihub', root / 'aihub', ignore=shutil.ignore_patterns('__pycache__'))
            data = root / 'data'
            data.mkdir()
            (data / 'config.json').write_text(json.dumps({'server': {'port': 0}, 'ai_root': '', 'scan_roots': []}))
            (data / 'server.pid.json').write_text('{"pid":999999,"source":"stale"}')
            (data / 'server.log').write_bytes(b'launcher history\n')
            with (root / 'stderr.log').open('wb') as output:
                if mode == 'desktop':
                    from test_desktop_launcher import launcher
                    with socket.socket() as available:
                        available.bind(('127.0.0.1', 0))
                        port = available.getsockname()[1]
                    state = {}
                    with mock.patch.object(launcher, 'ROOT', root), mock.patch.object(launcher, 'DATA', data):
                        result = launcher.ensure_running(port, startup_state=state)
                    self.assertEqual(result['status'], 'started')
                    process = state['process']
                else:
                    process = subprocess.Popen([sys.executable, '-B', str(root / 'server.py'), 'serve', '--no-initial-scan'],
                        cwd=root, stdout=output, stderr=output, stdin=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try:
                    control_path = data / 'desktop/server-control.json'
                    deadline = time.monotonic() + 12
                    while not control_path.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(.03)
                    self.assertTrue(control_path.exists(), (root / 'stderr.log').read_text())
                    control = json.loads(control_path.read_text())
                    connection = http.client.HTTPConnection('127.0.0.1', control['port'], timeout=5)
                    connection.request('GET', '/api/health?token=must-not-be-logged')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    response.read()
                    connection.close()
                    pid = json.loads((data / 'server.pid.json').read_text())
                    self.assertEqual(pid['pid'], process.pid)
                    self.assertEqual(pid['instance_id'], control['instance_id'])
                    self.assertEqual(pid['source'], 'server.py')
                    connection = http.client.HTTPConnection('127.0.0.1', control['port'], timeout=5)
                    connection.request('POST', '/api/desktop/shutdown', json.dumps({
                        'instance_id': control['instance_id'], 'install_root': control['install_root']}),
                        {'X-AIHub-Control-Token': control['token']})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 202)
                    response.read()
                    connection.close()
                    self.assertEqual(process.wait(8), 0)
                    self.assertFalse((data / 'server.pid.json').exists())
                    self.assertFalse(control_path.exists())
                    log = (data / 'service-runtime.log').read_text(encoding='utf-8')
                    for marker in ('started pid=', 'http method=GET status=200', 'stopped pid='):
                        self.assertIn(marker, log)
                    self.assertNotIn('must-not-be-logged', log)
                    self.assertNotIn(control['token'], log)
                    launcher_log = (data / 'server.log').read_bytes()
                    self.assertTrue(launcher_log.startswith(b'launcher history\n'))
                    if mode == 'direct':
                        self.assertEqual(launcher_log, b'launcher history\n')
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(5)


if __name__ == '__main__':
    unittest.main()
