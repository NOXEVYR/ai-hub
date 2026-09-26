"""Synthetic loopback HTTP integration. No production config, assets or tool execution."""
import copy
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
import zlib

import server
from aihub import api, config, jobs, projects, tool_adapters, workspace
from aihub.db import DB


def _png():
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\0\x30\x50\x70')) + chunk(b'IEND', b'')


class WorkspaceHTTP(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihub-http-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'Workspace'
        self.data = self.base / 'Application' / 'data'
        self.data.mkdir(parents=True)
        replacements = {'APP_DIR': self.data.parent, 'DATA_DIR': self.data,
                        'CONFIG_PATH': self.data / 'config.json', 'DB_PATH': self.data / 'index.db',
                        'SOURCES_PATH': self.data / 'sources.json', 'REPORTS_DIR': self.data / 'reports'}
        for key, path in replacements.items():
            patch = mock.patch.object(config, key, str(path))
            patch.start()
            self.addCleanup(patch.stop)
        self.source_bytes = b'{"synthetic-source":"preserve"}\n'
        Path(config.SOURCES_PATH).write_bytes(self.source_bytes)
        self.cfg = config.default_config(use_environment=False)
        self.cfg.update(ai_root='', scan_roots=[], output_roots=[], custom={'preserve': 'http-test'})
        config.save_config(self.cfg)
        self.shared_cfg = copy.deepcopy(self.cfg)
        self.db = DB()
        self.addCleanup(self.db.conn.close)
        for target, attr, value in [(server, 'CFG', self.cfg), (server, 'DB_OBJ', self.db),
                                    (api, 'APP_CFG', self.shared_cfg), (api, 'APP_DB', self.db),
                                    (jobs, '_jobs', {}), (workspace, '_previews', {}),
                                    (workspace, '_discovery_cache', {}), (projects, '_previews', {})]:
            patch = mock.patch.object(target, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        # Exercise actual adapter metadata with a bounded synthetic candidate; never probe installed tools.
        fake = self.base / 'fake-codex.cmd'
        fake.write_text('fixture; never execute', encoding='utf-8')
        patch = mock.patch.object(tool_adapters, '_candidates', side_effect=lambda key, cfg: [fake] if key == 'codex' else [])
        patch.start()
        self.addCleanup(patch.stop)

        class QuietHandler(server.Handler):
            def log_message(self, *_args):
                pass

        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_service)
        self.checks = []

    def stop_service(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        print(json.dumps({'test': self._testMethodName, 'pid': os.getpid(), 'port': self.port,
                          'service_closed': True, 'checks': self.checks}, ensure_ascii=False))

    def request(self, path, body=None):
        request = urllib.request.Request('http://127.0.0.1:%s%s' % (self.port, path),
                                         data=None if body is None else json.dumps(body).encode('utf-8'),
                                         headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def ok(self, path, body=None):
        code, result = self.request(path, body)
        self.assertEqual(code, 200, result)
        return result

    def create_workspace(self):
        plan = self.ok('/api/workspace/preview', {'mode': 'create', 'root': str(self.root)})
        self.assertTrue(plan['can_apply'], plan['errors'])
        self.assertFalse(self.root.exists())
        result = self.ok('/api/workspace/apply', {'token': plan['token']})
        self.assertTrue(result['applied'])
        self.assertTrue(result['status']['managed'])
        self.assertEqual(self.shared_cfg, self.cfg)
        self.assertEqual(self.ok('/api/workspace/status')['tools'], [])
        # User-selected recipes are explicitly registered through the real HTTP API.
        for identifier in ['codex', 'zcode', 'dsh', 'workbuddy']:
            body = {'id': identifier, 'revision': 0, 'connection_mode': 'mcp_stdio', '_workspace_root': str(self.root)}
            if identifier == 'codex':
                body['executable'] = str(self.base / 'fake-codex.cmd')
            self.ok('/api/harnesses/save', body)
        return result

    def test_new_workspace_four_tool_project_gallery_and_shared_config(self):
        self.create_workspace()
        self.checks.append('new workspace preview/apply and shared cfg synchronized')
        before = self.ok('/api/workspace/status')
        self.assertEqual({t['id'] for t in before['tools']}, {'codex', 'zcode', 'dsh', 'workbuddy'})
        self.assertTrue(before['tools'][0]['detected'])
        self.assertFalse(before['tools'][1]['available'])
        stale = self.ok('/api/workspace/preview', {'mode': 'connect', 'root': str(self.root)})
        selected = ['codex', 'zcode', 'dsh', 'workbuddy']
        plan = self.ok('/api/workspace/project/preview', {'name': 'Film', 'tools': selected})
        self.assertTrue(plan['can_apply'], plan['errors'])
        self.assertFalse(Path(plan['root']).exists())
        made = self.ok('/api/workspace/project/apply', {'token': plan['token']})
        self.assertTrue(made['applied'])
        self.assertEqual(made['tools'], selected)
        self.assertIs(made['output_root_added'], True)
        project = Path(made['root'])
        self.assertTrue(Path(made['prompt_path']).is_file())
        for name in ('AGENTS.md', 'CODEBUDDY.md', 'TASK_BRIEF.md'):
            self.assertTrue((project / name).is_file(), name)
        brief = Path(made['prompt_path']).read_text(encoding='utf-8')
        for tool_name in ('Codex', 'ZCode', 'DeepSeek Harness', 'WorkBuddy'):
            self.assertIn(tool_name, brief)
        self.assertFalse(list(project.glob('TOOL_HANDOFF_*.md')))
        self.checks.append('four tool native/handoff files and response types match frontend')
        code, rejected = self.request('/api/workspace/apply', {'token': stale['token']})
        self.assertNotEqual(code, 200)
        self.assertIn('配置', rejected['error'])
        current = self.ok('/api/workspace/status')
        output = str(project / 'Outputs')
        self.assertIn(output, current['sources']['output_roots'])
        self.assertEqual(self.shared_cfg, self.cfg)
        self.assertEqual(json.loads(Path(config.CONFIG_PATH).read_text(encoding='utf-8')), self.cfg)
        self.checks.append('project apply invalidates old workspace token and preserves shared cfg')

        # A training verification source is explicitly added; neither output fixture is a real image asset.
        training = self.root / '50_Training/Projects/Synthetic/Runs/v1/verify_out'
        training.mkdir(parents=True)
        (training / 'training.png').write_bytes(_png())
        (project / 'Outputs' / 'project.png').write_bytes(_png())
        merged_outputs = current['sources']['output_roots'] + [str(training)]
        plan = self.ok('/api/workspace/preview', {'mode': 'connect', 'root': str(self.root),
                       'scan_roots': current['sources']['scan_roots'], 'output_roots': merged_outputs})
        self.assertTrue(plan['can_apply'], plan['errors'])
        self.ok('/api/workspace/apply', {'token': plan['token']})
        self.assertEqual(self.cfg['output_roots'], merged_outputs)
        self.assertEqual(self.cfg['custom'], {'preserve': 'http-test'})
        self.assertEqual(Path(config.SOURCES_PATH).read_bytes(), self.source_bytes)
        self.ok('/api/scan/start', {})
        deadline = time.monotonic() + 10
        scan = None
        while time.monotonic() < deadline:
            scan = next((j for j in self.ok('/api/jobs')['jobs'] if j['name'] == 'scan'), None)
            if scan and scan['status'] != 'running':
                break
            time.sleep(.03)
        self.assertIsNotNone(scan)
        self.assertEqual(scan['status'], 'done', scan)
        gallery = self.ok('/api/images')
        self.assertEqual(gallery['total'], 2, gallery)
        self.assertEqual({item['name'] for item in gallery['items']}, {'project.png', 'training.png'})
        self.checks.append('explicit project and training sources scan into gallery; private settings preserved')

    def test_project_contract_rejections_do_not_write(self):
        self.create_workspace()
        empty = self.ok('/api/workspace/project/preview', {'name': 'NoTools', 'tools': []})
        self.assertFalse(empty['can_apply'])
        self.assertIsNone(empty['token'])
        valid = self.ok('/api/workspace/project/preview', {'name': 'Unique', 'tools': ['codex']})
        self.assertTrue(valid['can_apply'], valid['errors'])
        self.cfg['custom']['changed'] = True
        config.save_config(self.cfg)
        code, error = self.request('/api/workspace/project/apply', {'token': valid['token']})
        self.assertNotEqual(code, 200)
        self.assertIn('配置', error['error'])
        self.assertFalse(Path(valid['root']).exists())
        self.checks.append('empty tools and stale project token are rejected without creating project')


if __name__ == '__main__':
    unittest.main()
