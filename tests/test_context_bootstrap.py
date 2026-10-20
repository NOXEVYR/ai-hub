import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from aihub import collaboration as c, collaboration_api as api, config, context_bootstrap as ctx, harnesses
from tools.aihub_mcp import Bridge, BY_NAME, validate, RPCError


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        self.data = self.base / 'data'
        p = patch.object(config, 'DATA_DIR', str(self.data))
        p.start(); self.addCleanup(p.stop)
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True}
        harnesses.save(self.cfg, {'id': 'codex', 'revision': 0, 'connection_mode': 'mcp_stdio'})
        for client in ('one', 'two'):
            c.execute(self.cfg, 'client_heartbeat', {'client_id': client, 'tool': 'codex', 'name': client, 'protocol_version': 1})
        self.task = c.execute(self.cfg, 'task_create', {'project': 'Project', 'title': 'Task', 'report_policy': 'optional'})
        (self.root / 'AGENTS.md').write_text('User rules', encoding='utf-8')
        self.identity = {'client_id': 'one', 'session_id': 'session-1', 'task_id': self.task['id']}

    def call(self, action, **args):
        return api.execute(self.cfg, action, {**self.identity, **args}, actor='mcp')

    def memory(self, project, scope='project', approved=True):
        task = c.execute(self.cfg, 'task_create', {'project': project, 'title': 'source', 'report_policy': 'optional'})
        claim = c.execute(self.cfg, 'task_claim', {'task_id': task['id'], 'client_id': 'one'})
        artifact = c.execute(self.cfg, 'artifact_write', {'task_id': task['id'], 'client_id': 'one',
            'lease_token': claim['lease_token'], 'title': 'source', 'kind': 'report', 'filename': 'source.md', 'content': 'source'})
        memory = c.execute(self.cfg, 'memory_propose', {'title': project + scope, 'content': 'approved only',
            'scope': scope, 'project': project, 'source_artifact_id': artifact['id']})
        if approved:
            c.execute(self.cfg, 'memory_review', {'memory_id': memory['id'], 'status': 'approved'})
        return memory['id']

    def ack(self, bundle, **overrides):
        return self.call('context_ack', **{'bundle_id': bundle['bundle_id'], 'bundle_sha256': bundle['bundle_sha256'], **overrides})

    def test_approved_scope_rules_hash_receipt_idempotency(self):
        shared = self.memory('Other', scope='workspace')
        own = self.memory('Project')
        self.memory('Other')
        self.memory('Project', approved=False)
        bundle = self.call('context_bootstrap')
        self.assertEqual({m['id'] for m in bundle['bundle']['approved_memories']}, {own, shared})
        self.assertEqual(bundle['bundle']['rules'][0]['content'], 'User rules')
        self.assertEqual(bundle['bundle_sha256'], hashlib.sha256(ctx._json(bundle['bundle'])).hexdigest())
        result = self.ack(bundle)
        self.assertEqual(result['state'], 'client_acknowledged')
        self.assertFalse(result['model_understanding_verified'])
        self.assertEqual(self.ack(bundle)['acknowledged_at'], result['acknowledged_at'])
        self.assertEqual(self.call('context_status')['state'], 'client_acknowledged')
        self.assertNotIn('lease_hash', json.dumps(bundle))
        self.assertNotIn('lease_token', json.dumps(bundle))

    def test_other_client_session_task_and_workspace_cannot_ack(self):
        bundle = self.call('context_bootstrap')
        for args in ({'client_id': 'two'}, {'session_id': 'new-session'}, {'bundle_sha256': 'bad'}, {'bundle_id': 'bad'}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.ack(bundle, **args)
        other = c.execute(self.cfg, 'task_create', {'project': 'Other', 'title': 'Other'})
        with self.assertRaises(ValueError):
            self.ack(bundle, task_id=other['id'])
        with self.assertRaises(ValueError):
            self.call('context_bootstrap', _workspace_root=str(self.base / 'wrong'))

    def test_rules_memory_handoff_and_expiry_invalidate(self):
        bundle = self.call('context_bootstrap')
        (self.root / 'AGENTS.md').write_text('Changed rules', encoding='utf-8')
        with self.assertRaises(ValueError): self.ack(bundle)
        self.assertEqual(self.call('context_status')['state'], 'bootstrap_required')
        bundle = self.call('context_bootstrap')
        self.memory('Project')
        with self.assertRaises(ValueError): self.ack(bundle)
        bundle = self.call('context_bootstrap')
        claim = c.execute(self.cfg, 'task_claim', {'task_id': self.task['id'], 'client_id': 'one'})
        c.execute(self.cfg, 'task_handoff', {'task_id': self.task['id'], 'client_id': 'one',
            'lease_token': claim['lease_token'], 'target_tool': 'codex', 'summary': 'Resume from this handoff'})
        with self.assertRaises(ValueError): self.ack(bundle)
        bundle = self.call('context_bootstrap')
        self.assertEqual(bundle['bundle']['task']['summary'], 'Resume from this handoff')
        with patch.object(ctx.time, 'time', return_value=bundle['expires_at'] + 1):
            with self.assertRaises(ValueError): self.ack(bundle)

    def test_budget_partial_explicit_and_known_revision(self):
        (self.root / 'AGENTS.md').write_text('规则' * 15000, encoding='utf-8')
        # 90 KB must fail before reading the oversized source, never silently acknowledge it.
        with self.assertRaises(ValueError): self.call('context_bootstrap')
        (self.root / 'AGENTS.md').write_text('规则' * 9000, encoding='utf-8')
        bundle = self.call('context_bootstrap', max_bytes=8192)
        self.assertLessEqual(len(ctx._json(bundle)), 8192)
        self.assertFalse(bundle['complete'])
        self.assertFalse(self.ack(bundle)['complete'])
        again = self.call('context_bootstrap', known_revision=bundle['revision'])
        self.assertTrue(again['unchanged'])
        with self.assertRaises(ValueError): self.call('context_bootstrap', max_bytes=True)
        with self.assertRaises(ValueError): self.call('context_bootstrap', native_config='/private')

    def test_symlinks_rejected_and_no_native_history_read(self):
        (self.root / 'AGENTS.md').unlink()
        secret = self.base / 'private'
        secret.write_text('private credential')
        (self.root / 'AGENTS.md').symlink_to(secret)
        with self.assertRaises(ValueError): self.call('context_bootstrap')
        self.assertFalse((self.root / 'native_history').exists())

    def test_migration_backup_before_receipt_schema_and_no_raw_context_persistence(self):
        path = self.data / 'collaboration.sqlite3'
        with sqlite3.connect(path) as db: db.execute('DROP TABLE IF EXISTS context_receipts')
        before = set((self.data / 'collaboration-schema-backups').glob('*'))
        self.call('context_bootstrap')
        backups = set((self.data / 'collaboration-schema-backups').glob('*')) - before
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(next(iter(backups))) as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='context_receipts'").fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
        with sqlite3.connect(path) as db:
            row = db.execute('SELECT * FROM context_receipts').fetchone()
            self.assertNotIn('User rules', str(row))

    def test_bridge_owns_session_not_user_input(self):
        first, second = Bridge(12345, 'one', 'codex'), Bridge(12345, 'one', 'codex')
        self.assertNotEqual(first.context_session_id, second.context_session_id)
        with self.assertRaises(RPCError):
            validate(BY_NAME['aihub_context_bootstrap']['inputSchema'], {'task_id': self.task['id'], 'session_id': 'spoof'})
        validate(BY_NAME['aihub_context_ack']['inputSchema'], {'task_id': self.task['id'], 'bundle_id': 'a', 'bundle_sha256': 'b'})

    def test_real_stdio_http_bootstrap_ack_and_restart_invalidates(self):
        import os
        import subprocess
        import sys
        import threading
        import server
        from aihub import api as http_api
        (self.data / 'config.json').write_text('{}')
        for obj, name, value in [(server, 'CFG', self.cfg), (server, 'DB_OBJ', None), (http_api, 'APP_CFG', self.cfg)]:
            p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)
        class Quiet(server.Handler):
            def log_message(self, *_args): pass
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        self.cfg['server'] = {'port': http.server_port}
        thread = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        def stop_http():
            http.shutdown(); http.server_close(); thread.join(3)
        self.addCleanup(stop_http)
        script = Path(__file__).resolve().parents[1] / 'tools/aihub_mcp.py'
        def launch():
            proc = subprocess.Popen([sys.executable, '-B', str(script), '--port', str(http.server_port), '--tool', 'codex',
                                     '--client-id', 'one'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding='utf-8', env=dict(os.environ, XDG_DATA_HOME=str(self.base / 'outbox')))
            def close():
                proc.stdin.close(); proc.wait(timeout=10); proc.stdout.close(); proc.stderr.close()
            self.addCleanup(close)
            def send(packet, reply=True):
                proc.stdin.write(json.dumps(packet) + '\n'); proc.stdin.flush()
                return json.loads(proc.stdout.readline()) if reply else None
            send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25',
                  'capabilities': {}, 'clientInfo': {'name': 'Test', 'version': '1'}}})
            send({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, False)
            return send
        send = launch()
        def tool(send, name, arguments):
            return send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'aihub_' + name, 'arguments': arguments}})['result']
        delivered = tool(send, 'context_bootstrap', {'task_id': self.task['id']})
        self.assertFalse(delivered['isError'], delivered)
        bundle = json.loads(delivered['content'][0]['text'])
        ack_args = {'task_id': self.task['id'], 'bundle_id': bundle['bundle_id'], 'bundle_sha256': bundle['bundle_sha256']}
        confirmed = tool(send, 'context_ack', ack_args)
        self.assertFalse(confirmed['isError'], confirmed)
        self.assertEqual(json.loads(confirmed['content'][0]['text'])['state'], 'client_acknowledged')
        restarted = launch()
        self.assertTrue(tool(restarted, 'context_ack', ack_args)['isError'])
        self.assertFalse(tool(restarted, 'context_bootstrap', {'task_id': self.task['id']})['isError'])

    def test_foreign_task_target_owner_and_disabled_harness_rejected(self):
        harnesses.save(self.cfg, {'id': 'zcode', 'revision': 0, 'connection_mode': 'mcp_stdio'})
        foreign = c.execute(self.cfg, 'task_create', {'project': 'Other', 'title': 'Other', 'target_tool': 'zcode'})
        with self.assertRaises(PermissionError): self.call('context_bootstrap', task_id=foreign['id'])
        c.execute(self.cfg, 'task_claim', {'task_id': self.task['id'], 'client_id': 'two'})
        with self.assertRaises(PermissionError): self.call('context_bootstrap')
        own = c.execute(self.cfg, 'task_create', {'project': 'Project', 'title': 'own'})
        bundle = self.call('context_bootstrap', task_id=own['id'])
        registered = harnesses.get(self.cfg, 'codex')
        harnesses.save(self.cfg, {'id': 'codex', 'revision': registered['revision'], 'enabled': False})
        with self.assertRaises(PermissionError): self.ack(bundle, task_id=own['id'])

    def test_status_and_failed_ack_do_not_initialize_receipt_schema(self):
        path = self.data / 'collaboration.sqlite3'
        self.assertEqual(self.call('context_status')['state'], 'bootstrap_required')
        with self.assertRaises(ValueError): self.call('context_ack', bundle_id='none', bundle_sha256='none')
        with sqlite3.connect(path) as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='context_receipts'").fetchone())
        self.assertFalse((self.data / 'collaboration-schema-backups').exists())
