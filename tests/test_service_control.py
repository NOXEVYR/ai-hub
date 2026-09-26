"""Shutdown acceptance uses temporary installations and ephemeral loopback ports only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

import server
from aihub import api, collaboration_maintenance, jobs, service_control


class GateTests(unittest.TestCase):
    def test_admission_and_shutdown_are_atomic(self):
        for _ in range(30):
            gate = service_control.ActivityGate()
            ready = threading.Barrier(3)
            results = {}
            def enter():
                ready.wait()
                results['enter'] = gate.enter()
            def stop():
                ready.wait()
                results['stop'] = gate.request_stop()
            threads = [threading.Thread(target=enter), threading.Thread(target=stop)]
            for worker in threads:
                worker.start()
            ready.wait()
            for worker in threads:
                worker.join(2)
                self.assertFalse(worker.is_alive())
            self.assertNotEqual(results['enter'], results['stop'])
            if results['enter']:
                gate.leave()
                self.assertTrue(gate.request_stop())
            self.assertFalse(gate.enter())

    def test_background_job_holds_admission_until_finished(self):
        gate = service_control.ActivityGate()
        released, started = threading.Event(), threading.Event()
        def target():
            started.set()
            released.wait(5)
        with mock.patch.object(service_control, 'GATE', gate), mock.patch.object(jobs, '_jobs', {}):
            self.addCleanup(released.set)
            job = jobs.start('fixture', target)
            self.assertTrue(started.wait(2))
            self.assertIsNone(jobs.start('fixture', target))
            self.assertFalse(gate.request_stop())
            released.set()
            deadline = time.monotonic() + 2
            while job['status'] == 'running' and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(job['status'], 'done')
            self.assertTrue(gate.request_stop())
            self.assertIsNone(jobs.start('refused', target))

    def test_scheduler_holds_gate_and_never_starts_after_stop(self):
        gate = service_control.ActivityGate()
        released, started = threading.Event(), threading.Event()
        def retention(*_args, **_kwargs):
            started.set()
            released.wait(5)
        with mock.patch.object(service_control, 'GATE', gate), mock.patch.object(collaboration_maintenance, 'run_retention', side_effect=retention) as run:
            worker = collaboration_maintenance.start_scheduler({'workspace_managed': True})
            try:
                self.assertTrue(started.wait(2))
                self.assertFalse(gate.request_stop())
            finally:
                worker[0].set()
                released.set()
                worker[1].join(2)
            self.assertFalse(worker[1].is_alive())
            self.assertTrue(gate.request_stop())
            blocked = collaboration_maintenance.start_scheduler({'workspace_managed': True})
            blocked[1].join(2)
            self.assertFalse(blocked[1].is_alive())
            self.assertEqual(run.call_count, 1)

    def test_failed_job_thread_start_releases_gate(self):
        gate = service_control.ActivityGate()
        with mock.patch.object(service_control, 'GATE', gate), mock.patch.object(jobs, '_jobs', {}), mock.patch.object(threading.Thread, 'start', side_effect=RuntimeError('fixture thread unavailable')):
            with self.assertRaises(RuntimeError):
                jobs.start('fixture', lambda: None)
            self.assertTrue(gate.request_stop())
            self.assertEqual(jobs.get_jobs()[0]['status'], 'error')


class ControlFileTests(unittest.TestCase):
    def test_private_record_identity_and_old_instance_cleanup(self):
        with tempfile.TemporaryDirectory(prefix='aihub-control-') as temporary:
            first = service_control.ServiceControl(temporary, Path(temporary) / 'data', 12345)
            first.publish()
            value = json.loads(first.path.read_text(encoding='utf-8'))
            self.assertEqual(value['schema'], service_control.PROTOCOL)
            self.assertGreaterEqual(len(value['token']), 40)
            self.assertNotIn('token', first.public_identity())
            if os.name != 'nt':
                self.assertEqual(first.path.stat().st_mode & 0o777, 0o600)
            second = service_control.ServiceControl(temporary, Path(temporary) / 'data', 12346)
            second.publish()
            self.assertNotEqual(second.record['token'], first.record['token'])
            first.close()
            self.assertEqual(json.loads(second.path.read_text())['instance_id'], second.record['instance_id'])
            second.close()
            self.assertFalse(second.path.exists())

    def test_control_file_cannot_be_hard_link(self):
        with tempfile.TemporaryDirectory(prefix='aihub-control-') as temporary:
            control = service_control.ServiceControl(temporary, Path(temporary) / 'data', 12345)
            control.path.parent.mkdir(parents=True)
            original = Path(temporary) / 'keep.json'
            original.write_text('preserve', encoding='utf-8')
            os.link(original, control.path)
            with self.assertRaises(ValueError):
                control.publish()
            self.assertEqual(original.read_text(), 'preserve')


class ControlHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='aihub-control-http-')
        self.addCleanup(self.temporary.cleanup)
        self.gate = service_control.ActivityGate()
        patch = mock.patch.object(service_control, 'GATE', self.gate)
        patch.start()
        self.addCleanup(patch.stop)
        class QuietHandler(server.Handler):
            def log_message(self, *_args):
                pass
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.port = self.httpd.server_address[1]
        self.control = service_control.ServiceControl(self.temporary.name, Path(self.temporary.name) / 'data', self.port, self.gate)
        self.control.publish()
        self.httpd.service_control = self.control
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)
        self.control.close()
        self.assertFalse(self.thread.is_alive())

    def request(self, path='/api/desktop/shutdown', body=None, headers=None, method='POST'):
        if body is None:
            body = {'instance_id': self.control.record['instance_id'], 'install_root': self.temporary.name}
        headers = {'Content-Type': 'application/json', 'X-AIHub-Control-Token': self.control.record['token'], **(headers or {})}
        request = urllib.request.Request('http://127.0.0.1:%s%s' % (self.port, path),
                                         data=json.dumps(body).encode() if method == 'POST' else None,
                                         method=method, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_wrong_credentials_instance_install_origin_host_and_method_are_rejected(self):
        for headers in ({'X-AIHub-Control-Token': ''}, {'X-AIHub-Control-Token': 'wrong'},
                        {'X-AIHub-Control-Token': '\u00e9'}, {'Origin': 'http://evil.invalid'},
                        {'Origin': 'http://127.0.0.1:%s' % self.port}, {'Sec-Fetch-Site': 'none'},
                        {'Host': 'evil.invalid'}):
            with self.subTest(headers=list(headers)):
                self.assertEqual(self.request(headers=headers)[0], 403)
        for body in ({}, {'instance_id': 'wrong', 'install_root': self.temporary.name},
                     {'instance_id': self.control.record['instance_id'], 'install_root': 'relative'},
                     {'instance_id': self.control.record['instance_id'], 'install_root': str(Path(self.temporary.name) / 'other')}, []):
            self.assertEqual(self.request(body=body)[0], 403)
        self.assertEqual(self.request(method='GET')[0], 404)
        self.assertFalse(self.gate.stopping)

    def test_health_is_instance_bound_without_credential(self):
        status, value = self.request('/api/health', method='GET')
        self.assertEqual(status, 200)
        self.assertEqual(value['service_instance_id'], self.control.record['instance_id'])
        self.assertEqual(value['control_protocol'], service_control.PROTOCOL)
        self.assertEqual(value['desktop_shell_version'], '2.11.2')
        self.assertNotIn('token', value)

    def test_accepted_stop_refuses_new_http_work(self):
        self.assertTrue(self.gate.request_stop())
        with mock.patch.object(api, 'dispatch') as dispatch:
            status, value = self.request('/api/fixture/write')
            self.assertEqual(status, 503)
            self.assertEqual(value['code'], 'service_stopping')
            dispatch.assert_not_called()

    def test_busy_request_rejects_exit_and_idle_exit_stops_listener(self):
        entered, released = threading.Event(), threading.Event()
        def dispatch(*_args):
            entered.set()
            released.wait(5)
            return 200, {'Content-Type': 'application/json'}, b'{}'
        response = []
        with mock.patch.object(api, 'dispatch', side_effect=dispatch):
            writer = threading.Thread(target=lambda: response.append(self.request('/api/fixture/write')))
            writer.start()
            try:
                self.assertTrue(entered.wait(2))
                status, value = self.request()
                self.assertEqual(status, 409)
                self.assertEqual(value['code'], 'service_busy')
                self.assertFalse(self.gate.stopping)
            finally:
                released.set()
                writer.join(2)
        self.assertEqual(response[0][0], 200)
        self.assertEqual(self.request()[0], 202)
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(self.gate.enter())


class RealProcessTests(unittest.TestCase):
    def test_service_exception_cleans_only_its_control_record(self):
        source = Path(server.__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix='aihub-tray-exception-') as temporary:
            root = Path(temporary)
            shutil.copyfile(source / 'server.py', root / 'server.py')
            shutil.copytree(source / 'aihub', root / 'aihub', ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'data').mkdir()
            config_path = root / 'data' / 'config.json'
            config_path.write_text(json.dumps({'server': {'port': 0}, 'ai_root': '', 'scan_roots': []}), encoding='utf-8')
            code = ('import server; from pathlib import Path\n'
                    'def fail(self):\n'
                    ' assert Path("data/desktop/server-control.json").is_file()\n'
                    ' Path("published.marker").write_text("published")\n'
                    ' raise RuntimeError("synthetic serve failure")\n'
                    'server.ThreadingHTTPServer.serve_forever=fail\n'
                    'server.main()\n')
            process = subprocess.run([sys.executable, '-B', '-c', code, 'serve', '--no-initial-scan'],
                                     cwd=root, capture_output=True, timeout=12,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b'synthetic serve failure', process.stderr)
            self.assertTrue((root / 'published.marker').exists())
            self.assertFalse((root / 'data' / 'desktop' / 'server-control.json').exists())
            self.assertTrue(config_path.exists())

    def test_fresh_install_launch_and_controlled_exit_preserves_data(self):
        source = Path(server.__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix='aihub-tray-process-') as temporary:
            root = Path(temporary)
            shutil.copyfile(source / 'server.py', root / 'server.py')
            shutil.copytree(source / 'aihub', root / 'aihub', ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'data').mkdir()
            sentinel = root / 'data' / 'user-keep.txt'
            sentinel.write_text('must survive shutdown', encoding='utf-8')
            # Port zero is test-only configuration; production launcher enforces a fixed valid port.
            (root / 'data' / 'config.json').write_text(json.dumps({'server': {'port': 0}, 'ai_root': '', 'scan_roots': []}), encoding='utf-8')
            with (root / 'service-output.log').open('wb') as log:
                process = subprocess.Popen([sys.executable, '-B', str(root / 'server.py'), 'serve', '--no-initial-scan'],
                                           cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try:
                    control_path = root / 'data' / 'desktop' / 'server-control.json'
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline and not control_path.is_file() and process.poll() is None:
                        time.sleep(.03)
                    self.assertTrue(control_path.is_file(), (root / 'service-output.log').read_text(encoding='utf-8'))
                    value = json.loads(control_path.read_text(encoding='utf-8'))
                    self.assertEqual(value['pid'], process.pid)
                    self.assertEqual(os.path.normcase(value['install_root']), os.path.normcase(str(root.resolve())))
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    base = 'http://127.0.0.1:%d' % value['port']
                    with opener.open(base + '/api/health', timeout=3) as response:
                        health = json.load(response)
                    self.assertEqual(health['service_instance_id'], value['instance_id'])
                    request = urllib.request.Request(base + '/api/desktop/shutdown',
                        data=json.dumps({'instance_id': value['instance_id'], 'install_root': value['install_root']}).encode('utf-8'),
                        headers={'Content-Type': 'application/json', 'X-AIHub-Control-Token': value['token']})
                    with opener.open(request, timeout=3) as response:
                        self.assertEqual(response.status, 202)
                    self.assertEqual(process.wait(timeout=5), 0)
                    self.assertFalse(control_path.exists())
                    self.assertEqual(sentinel.read_text(), 'must survive shutdown')
                    self.assertTrue((root / 'data' / 'aihub.db').exists())
                finally:
                    if process.poll() is None:
                        # Only this test's directly-created fixture process, never a discovered PID.
                        process.terminate()
                        process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
