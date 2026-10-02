"""Explicit native exit preserves unfinished collaboration in temporary services."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import server


# Only the copied test service gets synthetic blocking operations. Production
# cmd_serve, HTTP admission, jobs and authenticated shutdown remain in use.
BOOTSTRAP = '''
import sys, time
from pathlib import Path
import server
from aihub import collaboration, collaboration_api, config, harnesses, jobs
cfg = config.load_config()
state = sys.argv[1]
harnesses.save(cfg, {'id': 'codex', 'revision': 0, 'connection_mode': 'mcp_stdio'})
collaboration_api.execute(cfg, 'client_heartbeat', {
    'client_id': 'fixture-owner', 'tool': 'codex', 'name': 'Synthetic owner', 'protocol_version': 1})
task = collaboration.execute(cfg, 'task_create', {'project': 'Synthetic', 'title': 'Unfinished fixture'})
if state != 'queued':
    collaboration.execute(cfg, 'task_claim', {'task_id': task['id'], 'client_id': 'fixture-owner'})
    if state == 'active-stale':
        with collaboration.store(cfg) as (con, root):
            con.execute("UPDATE clients SET last_seen=? WHERE root=? AND id=?",
                        ('2000-01-01T00:00:00+00:00', root, 'fixture-owner'))
dispatch = server.api.dispatch
def block(kind):
    Path(kind + '.started').write_text('synthetic')
    deadline = time.monotonic() + 15
    while not Path(kind + '.release').exists():
        if time.monotonic() > deadline:
            raise RuntimeError('fixture release timed out')
        time.sleep(.01)
    Path(kind + '.finished').write_text('synthetic')
def fixture_dispatch(db, cfg, method, path, params, body):
    if path == '/api/fixture/block-http':
        block('http')
        return 200, {'Content-Type': 'application/json'}, b'{}'
    if path == '/api/fixture/start-job':
        jobs.start('shutdown-fixture', block, 'job')
        return 200, {'Content-Type': 'application/json'}, b'{}'
    if path == '/api/fixture/job-status':
        return 200, {'Content-Type': 'application/json'}, server.json.dumps(jobs.get_jobs()).encode()
    return dispatch(db, cfg, method, path, params, body)
server.api.dispatch = fixture_dispatch
sys.argv = ['server.py', 'serve', '--no-initial-scan']
server.main()
'''


class PersistentShutdownTests(unittest.TestCase):
    @contextmanager
    def service(self, state):
        source = Path(server.__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix='aihub-persistent-exit-') as temporary:
            base = Path(temporary)
            root = base / 'app'
            root.mkdir()
            workspace = base / 'workspace'
            workspace.mkdir()
            shutil.copyfile(source / 'server.py', root / 'server.py')
            shutil.copytree(source / 'aihub', root / 'aihub', ignore=shutil.ignore_patterns('__pycache__'))
            data = root / 'data'
            data.mkdir()
            (data / 'config.json').write_text(json.dumps({
                'server': {'port': 0}, 'ai_root': str(workspace),
                'workspace_managed': True, 'scan_roots': [], 'output_roots': []}), encoding='utf-8')
            (workspace / 'keep.txt').write_text('user fixture must survive', encoding='utf-8')
            with (root / 'fixture.log').open('wb') as log:
                process = subprocess.Popen([sys.executable, '-B', '-c', BOOTSTRAP, state],
                    cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try:
                    control_path = data / 'desktop' / 'server-control.json'
                    self.wait_for(lambda: control_path.exists() or process.poll() is not None)
                    self.assertTrue(control_path.exists(), (root / 'fixture.log').read_text(encoding='utf-8'))
                    control = json.loads(control_path.read_text(encoding='utf-8'))
                    self.assertEqual(control['pid'], process.pid)
                    self.assertEqual(Path(control['install_root']), root.resolve())
                    fixture = (root, workspace, process, control)
                    status, health = self.request(fixture, '/api/health', method='GET')
                    self.assertEqual(status, 200)
                    self.assertEqual(health['service_instance_id'], control['instance_id'])
                    yield fixture
                finally:
                    for kind in ('http', 'job'):
                        (root / (kind + '.release')).touch()
                    if process.poll() is None:
                        # This is exclusively the subprocess created above.
                        process.terminate()
                        process.wait(timeout=5)

    def wait_for(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(predicate(), 'isolated fixture did not reach the expected state')

    def request(self, fixture, path='/api/desktop/shutdown', *, method='POST', body=None, token=None):
        control = fixture[3]
        if body is None:
            body = {'instance_id': control['instance_id'], 'install_root': control['install_root']}
        request = urllib.request.Request('http://127.0.0.1:%d%s' % (control['port'], path),
            data=json.dumps(body).encode() if method == 'POST' else None, method=method,
            headers={'Content-Type': 'application/json',
                     'X-AIHub-Control-Token': control['token'] if token is None else token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def snapshot(self, fixture):
        root, workspace, _, _ = fixture
        con = sqlite3.connect(root / 'data' / 'collaboration.sqlite3')
        con.row_factory = sqlite3.Row
        try:
            task = dict(con.execute('SELECT * FROM tasks').fetchone())
            client = dict(con.execute('SELECT * FROM clients').fetchone())
            dump = list(con.iterdump())
        finally:
            con.close()
        files = {str(path.relative_to(workspace)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in workspace.rglob('*') if path.is_file()}
        return task, client, dump, files, (root / 'data' / 'config.json').read_bytes()

    def test_queued_and_active_fresh_or_stale_owner_survive_authenticated_exit(self):
        for state in ('queued', 'active-fresh', 'active-stale'):
            with self.subTest(state=state), self.service(state) as fixture:
                before = self.snapshot(fixture)
                task, client = before[:2]
                self.assertEqual(task['status'], 'queued' if state == 'queued' else 'active')
                if state == 'queued':
                    self.assertIsNone(task['owner'])
                    self.assertIsNone(task['lease_hash'])
                else:
                    self.assertEqual(task['owner'], 'fixture-owner')
                    self.assertEqual(len(task['lease_hash']), 64)
                    self.assertEqual(client['last_seen'].startswith('2000-'), state == 'active-stale')
                # Native control authority stays instance-bound even with unfinished tasks.
                for token in ('', 'wrong-fixture-credential'):
                    self.assertEqual(self.request(fixture, token=token)[0], 403)
                body = {'instance_id': 'other-fixture-instance', 'install_root': fixture[3]['install_root']}
                self.assertEqual(self.request(fixture, body=body)[0], 403)
                self.assertIsNone(fixture[2].poll())
                self.assertEqual(self.snapshot(fixture), before)
                status, value = self.request(fixture)
                self.assertEqual(status, 202)
                self.assertEqual(value['instance_id'], fixture[3]['instance_id'])
                self.assertEqual(fixture[2].wait(timeout=5), 0)
                self.assertFalse((fixture[0] / 'data' / 'desktop' / 'server-control.json').exists())
                self.assertEqual(self.snapshot(fixture), before)

    def test_real_http_and_job_busy_reject_exit_then_idle_preserves_active_task(self):
        with self.service('active-fresh') as fixture:
            root = fixture[0]
            before = self.snapshot(fixture)
            responses = []
            writer = threading.Thread(target=lambda: responses.append(
                self.request(fixture, '/api/fixture/block-http')))
            writer.start()
            try:
                self.wait_for(lambda: (root / 'http.started').exists())
                self.assert_busy(fixture)
            finally:
                (root / 'http.release').touch()
                writer.join(6)
            self.assertFalse(writer.is_alive())
            self.assertEqual(responses[0][0], 200)
            self.assertEqual(self.request(fixture, '/api/fixture/start-job')[0], 200)
            self.wait_for(lambda: (root / 'job.started').exists())
            self.assert_busy(fixture)
            (root / 'job.release').touch()
            self.wait_for(lambda: self.request(fixture, '/api/fixture/job-status', method='GET')[1][0]['finished'] is not None)
            self.assertEqual(self.request(fixture)[0], 202)
            self.assertEqual(fixture[2].wait(timeout=5), 0)
            self.assertEqual(self.snapshot(fixture), before)

    def assert_busy(self, fixture):
        status, value = self.request(fixture)
        self.assertEqual(status, 409)
        self.assertEqual(value['code'], 'service_busy')
        self.assertEqual(value['error'], '本机仍有扫描、更新或数据操作正在执行，请稍后重试退出。')
        self.assertIsNone(fixture[2].poll())


if __name__ == '__main__':
    unittest.main()
