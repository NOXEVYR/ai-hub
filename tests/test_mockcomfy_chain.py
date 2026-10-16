"""Real Hub TCP + synthetic worker + MockComfy HTTP. No other product or GPU."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest
import urllib.request
import uuid

import test_execution_http as fixture


class MockComfyChain(unittest.TestCase):
    def setUp(self):
        self.hub = fixture.ExecutionHTTPTests()
        self.addCleanup(self.hub.doCleanups)
        self.hub.setUp()

    def test_lost_reply_restart_single_prompt_report_and_review(self):
        hub = self.hub
        hub.seed()
        prompts = []
        output = b'MockComfy synthetic output; no model inference\n'

        class MockComfy(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                if self.path != '/prompt':
                    self.send_error(404)
                    return
                prompts.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                body = json.dumps({'prompt_id': 'mock-prompt-1'}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == '/history/mock-prompt-1':
                    body = json.dumps({'mock-prompt-1': {'outputs': {'1': {'files': ['mock.txt']}},
                                                       'status': {'completed': True}}}).encode()
                elif self.path == '/view?filename=mock.txt':
                    body = output
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        provider = ThreadingHTTPServer(('127.0.0.1', 0), MockComfy)
        thread = threading.Thread(target=provider.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        def stop_provider():
            provider.shutdown()
            provider.server_close()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop_provider)
        base = 'http://127.0.0.1:%s' % provider.server_port
        def fetch(path, payload=None):
            req = urllib.request.Request(base + path, data=None if payload is None else json.dumps(payload).encode(),
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.read()

        intent = hub.intent()
        hub.drop_accept = True
        import http.client
        with self.assertRaises(http.client.RemoteDisconnected):
            hub.request('/api/execution/accept', intent, bearer=hub.source['token'])
        receipt = hub.accept(intent)
        claim_id = str(uuid.uuid4())
        claim = hub.execute('claim', {'execution_id': receipt['execution_id'], 'claim_request_id': claim_id}, hub.worker['token'])
        owner = dict(_workspace_root=str(hub.root), task_id=receipt['queue_task_id'],
                     client_id='worker-one', lease_token=claim['lease_token'])
        resource = hub.success('/api/collaboration/mcp/resource_register', dict(owner,
            resource_type='browser_tab', ownership='task_exclusive', temporary=True,
            identity={'browser_id': 'mock', 'session_id': 'mock', 'tab_id': 'mock'}))
        # Resume the original claim after a real TCP server restart; no second dispatch.
        hub.stop()
        hub.start()
        claim = hub.execute('claim', {'execution_id': receipt['execution_id'], 'claim_request_id': claim_id}, hub.worker['token'])
        owner['lease_token'] = claim['lease_token']
        observation = dict(execution_id=receipt['execution_id'], lease_token=claim['lease_token'],
                           provider_request_id='mock-prompt-1', results=[])
        hub.execute('observe', dict(observation, observation_id=str(uuid.uuid4()), provider_state='submitting'), hub.worker['token'])
        prompt = json.loads(fetch('/prompt', {'prompt': {'1': {'class_type': 'SyntheticText'}}}))
        self.assertEqual(prompt['prompt_id'], 'mock-prompt-1')
        hub.execute('observe', dict(observation, observation_id=str(uuid.uuid4()), provider_state='running'), hub.worker['token'])
        history = json.loads(fetch('/history/' + prompt['prompt_id']))
        self.assertTrue(history[prompt['prompt_id']]['status']['completed'])
        raw = fetch('/view?filename=mock.txt')
        self.assertEqual(raw, output)
        result = dict(result_id='mock-output', kind='document', media_type='text/plain',
                      bytes=len(raw), locator='mock/results/mock.txt', sha256=hashlib.sha256(raw).hexdigest())
        terminal = dict(observation, observation_id=str(uuid.uuid4()), provider_state='succeeded', results=[result])
        hub.execute('observe', terminal, hub.worker['token'])
        hub.execute('observe', terminal, hub.worker['token'])
        code, _ = hub.request('/api/collaboration/mcp/task_finish', dict(owner, summary='too early'))
        self.assertEqual(code, 400)
        submission = dict(owner, submission_id=str(uuid.uuid4()), category='report', title='MockComfy CI',
            filename='mock-ci.md', content='# MockComfy\n' + result['sha256'],
            memory_candidates=[{'title': '模拟链路', 'content': '仅合成模拟结论', 'scope': 'project'}])
        report = hub.success('/api/collaboration/mcp/report_submit', submission)
        self.assertEqual(hub.success('/api/collaboration/mcp/report_submit', submission)['id'], report['id'])
        self.assertIn(result['sha256'], Path(report['path']).read_text(encoding='utf-8'))
        self.assertEqual(report['memory_candidate_count'], 1)
        self.assertEqual(hub.success('/api/collaboration/mcp/memory_search', {'_workspace_root': str(hub.root), 'client_id': 'worker-one'})['items'], [])
        review = dict(_workspace_root=str(hub.root), client_id='worker-one', memory_id=report['memory_candidate_ids'][0], status='approved')
        self.assertEqual(hub.request('/api/collaboration/mcp/memory_review', review)[0], 403)
        hub.success('/api/collaboration/memory_review', review)
        self.assertEqual(len(hub.success('/api/collaboration/mcp/memory_search', {'_workspace_root': str(hub.root), 'client_id': 'worker-one'})['items']), 1)
        closed = hub.success('/api/collaboration/mcp/resource_cleanup_report', dict(owner, resource_id=resource['id'], state='closed',
            evidence={'identity': {'browser_id': 'mock', 'session_id': 'mock', 'tab_id': 'mock'},
                      'observed_at': fixture.collaboration._now(), 'outcome': 'absent'}))
        self.assertEqual(closed['state'], 'closed')
        hub.success('/api/collaboration/mcp/task_finish', dict(owner, summary='MockComfy only'))
        hub.stop()
        hub.start()
        final = hub.execute('status', {'request_id': intent['request_id']}, hub.source['token'])
        self.assertEqual(final['dispatch_state'], 'completed')
        self.assertEqual(final['results'], [result])
        self.assertFalse(final['native_execution_verified_by_hub'])
        self.assertEqual(hub.counts(), (1, 1))
        self.assertEqual(len(prompts), 1)
