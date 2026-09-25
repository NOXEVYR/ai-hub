import concurrent.futures
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from aihub import collaboration as c, config


class CollaborationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / 'workspace'
        self.root.mkdir()
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True}
        self.data_patch = patch.object(config, 'DATA_DIR', str(base / 'data'))
        self.data_patch.start()
        self.heartbeat('one')

    def tearDown(self):
        self.data_patch.stop()
        self.tmp.cleanup()

    def call(self, action, **payload):
        return c.execute(self.cfg, action, payload)

    def heartbeat(self, client, tool='codex'):
        return self.call('client_heartbeat', client_id=client, tool=tool, name=client, protocol_version=1)

    def task(self, claim=True):
        task = self.call('task_create', project='Project', title='任务', description='untrusted text', target_tool='any')
        if claim:
            claim = self.call('task_claim', task_id=task['id'], client_id='one')
            self.lease = dict(task_id=task['id'], client_id='one', lease_token=claim['lease_token'])
        return task

    def artifact(self, kind='report', filename='result.md', **extra):
        return self.call('artifact_write', **self.lease, kind=kind, title='报告', filename=filename, content='test', **extra)

    def test_task_paths_no_overwrite_and_input_validation(self):
        task = self.task()
        self.assertTrue(Path(task['paths']['reports']).is_dir())
        self.assertIn('40_Projects', task['paths']['work'])
        brief = (Path(task['paths']['work']) / 'TASK_BRIEF.md').read_text(encoding='utf-8')
        self.assertIn(task['id'], brief)
        self.assertIn('不是跨任务授权', brief)
        self.assertIn('memory_search', brief)
        for project in ['../escape', 'C:\\root', 'CON', 'a:b', '.', 'a/b']:
            with self.subTest(project=project), self.assertRaises(ValueError):
                self.call('task_create', project=project, title='bad')
        with self.assertRaises(ValueError):
            c.execute(dict(self.cfg, workspace_managed=False), 'task_create', {'project': 'x', 'title': 'x'})

    def test_root_isolation_and_status_hides_credentials(self):
        task = self.task()
        other = self.root.parent / 'other'
        other.mkdir()
        cfg = dict(self.cfg, ai_root=str(other))
        self.assertEqual(c.status(cfg)['tasks'], [])
        with self.assertRaises(ValueError):
            c.execute(cfg, 'task_finish', self.lease)
        serialized = json.dumps(c.status(self.cfg)) + json.dumps(self.call('task_list'))
        self.assertNotIn('lease_token', serialized)
        self.assertNotIn('lease_hash', serialized)
        self.assertNotIn(self.lease['lease_token'], serialized)

    def test_atomic_claim_race_and_handoff(self):
        task = self.task(False)
        self.heartbeat('two', 'zcode')
        def attempt(client):
            try:
                return self.call('task_claim', task_id=task['id'], client_id=client)
            except ValueError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ['one', 'two']))
        winners = [result for result in results if result]
        self.assertEqual(len(winners), 1)
        winner = winners[0]
        lease = dict(task_id=task['id'], client_id=winner['task']['owner'], lease_token=winner['lease_token'])
        self.call('task_handoff', **lease, target_tool='dsh', summary='next')
        with self.assertRaises(ValueError):
            self.call('task_finish', **lease)
        with self.assertRaises(ValueError):
            self.call('task_claim', task_id=task['id'], client_id='one')
        self.heartbeat('three', 'dsh')
        self.assertEqual(self.call('task_claim', task_id=task['id'], client_id='three')['task']['owner'], 'three')

    def test_wrong_lease_and_foreign_client(self):
        self.task()
        with self.assertRaises(ValueError):
            self.call('artifact_write', **dict(self.lease, lease_token='wrong'), kind='report', title='x', filename='x', content='x')
        with self.assertRaises(ValueError):
            self.heartbeat('one', 'workbuddy')
        with self.assertRaises(ValueError):
            self.call('client_heartbeat', client_id='new', tool='codex', name='x', protocol_version=True)

    def test_artifact_new_file_hash_and_expiry(self):
        self.task()
        row = self.artifact()
        self.assertEqual(Path(row['path']).read_text(), 'test')
        self.assertIsNone(row['expires_at'])
        with self.assertRaises(FileExistsError):
            self.artifact()
        self.assertEqual(Path(row['path']).read_text(), 'test')
        temp = self.artifact('temp')
        delta = dt.datetime.fromisoformat(temp['expires_at']) - dt.datetime.fromisoformat(temp['created_at'])
        self.assertEqual(delta.days, 7)
        for name in ['../outside', 'C:\\bad', 'file:secret', 'CON']:
            with self.assertRaises(ValueError):
                self.artifact(filename=name)
        with self.assertRaises(ValueError):
            self.call('artifact_write', **self.lease, kind='report', title='x', filename='big.md', content='中' * 400000)

    def test_registered_sources_unchanged_and_kind_boundary(self):
        task = self.task()
        path = Path(task['paths']['reports']) / 'source.md'
        path.write_text('keep')
        row = self.call('artifact_register', **self.lease, kind='report', title='x', path=str(path))
        self.assertEqual(path.read_text(), 'keep')
        with self.assertRaises((ValueError, sqlite3.IntegrityError)):
            self.call('artifact_register', **self.lease, kind='temp', title='x', path=str(path))
        with self.assertRaises(sqlite3.IntegrityError):
            self.call('artifact_register', **self.lease, kind='report', title='x', path=str(path))
        outside = self.root / 'outside.md'
        outside.write_text('outside')
        with self.assertRaises(ValueError):
            self.call('artifact_register', **self.lease, kind='report', title='x', path=str(outside))
        self.assertEqual(self.call('artifact_list')['items'][0]['id'], row['id'])

    def test_hard_links_rejected(self):
        task = self.task()
        source = self.root / 'source.md'
        source.write_text('source')
        linked = Path(task['paths']['reports']) / 'linked.md'
        os.link(source, linked)
        with self.assertRaises(ValueError):
            self.call('artifact_register', **self.lease, kind='report', title='x', path=str(linked))

    def test_symlinks_and_ancestor_links_rejected(self):
        task = self.task()
        source = self.root / 'source.md'
        source.write_text('source')
        linked = Path(task['paths']['reports']) / 'linked.md'
        try:
            linked.symlink_to(source)
        except OSError:
            self.skipTest('OS does not permit symbolic links')
        with self.assertRaises(ValueError):
            self.call('artifact_register', **self.lease, kind='report', title='x', path=str(linked))
        folder = Path(task['paths']['reports']) / 'linked-folder'
        folder.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.call('artifact_register', **self.lease, kind='report', title='x', path=str(folder / 'source.md'))

    def test_failed_registration_rolls_back_only_new_file(self):
        self.task()
        with patch.object(c, '_insert_artifact', side_effect=RuntimeError('db failure')):
            with self.assertRaises(RuntimeError):
                self.artifact()
        self.assertEqual(self.call('artifact_list')['items'], [])
        path = Path(self.call('task_list')['items'][0]['paths']['reports']) / 'result.md'
        self.assertFalse(path.exists())
        path.write_text('existing source')
        with patch.object(c, '_insert_artifact', side_effect=RuntimeError('db failure')):
            with self.assertRaises(RuntimeError):
                self.call('artifact_register', **self.lease, kind='report', title='x', path=str(path))
        self.assertEqual(path.read_text(), 'existing source')

    def test_memory_review_visibility_source_and_actor(self):
        self.task()
        source = self.artifact()
        row = c.execute(self.cfg, 'memory_propose', dict(title='memory', content='retain', scope='project', project='Project', source_artifact_id=source['id']), actor='mcp')
        self.assertEqual(self.call('memory_search')['items'], [])
        with self.assertRaises(ValueError):
            c.execute(self.cfg, 'memory_review', dict(memory_id=row['id'], status='approved'), actor='mcp')
        self.call('memory_review', memory_id=row['id'], status='approved')
        self.assertEqual(self.call('memory_search', query='retain', project='Project')['items'][0]['id'], row['id'])
        self.assertEqual(self.call('memory_search')['items'][0]['source_path'], source['path'])
        self.assertEqual(self.call('memory_search', query='retain', project='Other')['items'], [])
        self.call('memory_review', memory_id=row['id'], status='retired')
        self.assertEqual(self.call('memory_search')['items'], [])
        Path(source['path']).write_text('changed')
        with self.assertRaises(ValueError):
            self.call('memory_review', memory_id=row['id'], status='approved')
        temp = self.artifact('temp')
        with self.assertRaises(ValueError):
            self.call('memory_propose', title='x', content='x', scope='workspace', source_artifact_id=temp['id'])

    def test_retention_protection_and_recycle_callback(self):
        self.task()
        temp = self.artifact('temp')
        report = self.artifact('report')
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=9)
        self.assertEqual(c.retention_candidates(self.cfg, future), [])
        self.call('task_finish', **self.lease)
        self.assertEqual(len(c.retention_candidates(self.cfg, future)), 1)
        self.assertEqual(c.retention_candidates(self.cfg), [])
        callback = lambda path: Path(path).rename(Path(path).with_suffix('.recycled-test'))
        self.assertEqual(c.recycle_candidate(self.cfg, report['id'], callback, future)['status'], 'protected')
        self.call('artifact_pin', artifact_id=temp['id'], pinned=True)
        self.assertEqual(c.recycle_candidate(self.cfg, temp['id'], callback, future)['status'], 'protected')
        self.call('artifact_pin', artifact_id=temp['id'], pinned=False)
        self.assertEqual(c.recycle_candidate(self.cfg, temp['id'], callback, future)['status'], 'recycled')
        self.assertTrue(Path(report['path']).exists())
        self.assertEqual(c.retention_candidates(self.cfg, future), [])

    def test_retention_changed_identity_hash_and_errors_keep_files(self):
        self.task()
        temp = self.artifact('temp')
        self.call('task_finish', **self.lease)
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=9)
        path = Path(temp['path'])
        old_stat = path.stat()
        path.write_text('evil')
        os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        called = []
        self.assertEqual(c.recycle_candidate(self.cfg, temp['id'], lambda p: called.append(p), future)['status'], 'protected')
        self.assertEqual(called, [])
        path.write_text('test')
        os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        def failed(path):
            raise OSError('recycle unsupported')
        self.assertEqual(c.recycle_candidate(self.cfg, temp['id'], failed, future)['status'], 'error')
        self.assertTrue(path.exists())
        row = self.call('artifact_list', kind='temp')['items'][0]
        self.assertEqual(row['status'], 'active')
        self.assertIn('unsupported', row['recycle_error'])

    def test_file_changed_during_hash_is_rejected(self):
        path = self.root / 'file'
        path.write_text('data')
        real_sha = c.hashlib.sha256
        class MutatingHash:
            def __init__(self):
                self.hash = real_sha()
            def update(self, chunk):
                path.write_text('changed')
                self.hash.update(chunk)
            def hexdigest(self):
                return self.hash.hexdigest()
        with patch.object(c.hashlib, 'sha256', MutatingHash), self.assertRaises(ValueError):
            c.file_snapshot(path)

    def test_failed_write_preserves_intervening_user_change(self):
        task = self.task()
        path = Path(task['paths']['reports']) / 'result.md'
        def fail(*args):
            path.write_text('user replacement')
            raise RuntimeError('failed database')
        with patch.object(c, '_insert_artifact', fail), self.assertRaises(RuntimeError):
            self.artifact()
        self.assertEqual(path.read_text(), 'user replacement')

    def test_task_creation_failure_removes_only_new_empty_directories(self):
        project = self.root / '40_Projects' / 'Existing'
        project.mkdir(parents=True)
        existing = project / 'keep.md'
        existing.write_text('keep')
        with patch.object(c, '_audit', side_effect=RuntimeError('audit failure')), self.assertRaises(RuntimeError):
            self.call('task_create', project='Existing', title='failed')
        self.assertTrue(project.exists())
        self.assertEqual(existing.read_text(), 'keep')
        self.assertFalse((project / 'Work').exists())
        self.assertEqual(self.call('task_list')['items'], [])

    def test_recycle_rechecks_new_hardlink_and_identity_replacement(self):
        self.task()
        artifact = self.artifact('temp')
        self.call('task_finish', **self.lease)
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=9)
        path = Path(artifact['path'])
        link = self.root / 'link'
        os.link(path, link)
        self.assertEqual(c.recycle_candidate(self.cfg, artifact['id'], lambda p: self.fail('callback must not run'), future)['status'], 'protected')
        link.unlink()
        old = path.stat()
        path.rename(path.with_suffix('.old'))
        path.write_text('test')
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertEqual(c.recycle_candidate(self.cfg, artifact['id'], lambda p: self.fail('callback must not run'), future)['status'], 'protected')

    def test_pin_and_recycle_serialized(self):
        self.task()
        artifact = self.artifact('temp')
        self.call('task_finish', **self.lease)
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=9)
        entered, released, pin_started = threading.Event(), threading.Event(), threading.Event()
        def recycler(path):
            entered.set()
            self.assertTrue(released.wait(3))
            Path(path).rename(Path(path).with_suffix('.recycled-test'))
        def pin():
            pin_started.set()
            try:
                self.call('artifact_pin', artifact_id=artifact['id'], pinned=True)
            except ValueError:
                return 'already-recycled'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            recycled = pool.submit(c.recycle_candidate, self.cfg, artifact['id'], recycler, future)
            self.assertTrue(entered.wait(3))
            pinned = pool.submit(pin)
            self.assertTrue(pin_started.wait(3))
            self.assertFalse(pinned.done())
            released.set()
            self.assertEqual(recycled.result()['status'], 'recycled')
            self.assertEqual(pinned.result(), 'already-recycled')

    def test_ui_only_reviews_and_pin(self):
        self.task()
        artifact = self.artifact()
        for action, payload in [('artifact_pin', {'artifact_id': artifact['id'], 'pinned': True}), ('memory_list', {}), ('memory_review', {'memory_id': 'missing', 'status': 'approved'})]:
            with self.subTest(action=action), self.assertRaises(ValueError):
                c.execute(self.cfg, action, payload, actor='mcp')

    def test_timestamp_epoch_and_snapshot_size_bound(self):
        self.assertTrue(c._now(0).startswith('1970-01-01'))
        for value in [True, False, float('inf'), float('nan')]:
            with self.assertRaises(ValueError):
                c._now(value)
        path = self.root / 'oversized.md'
        path.write_text('12345')
        with self.assertRaises(ValueError):
            c.file_snapshot(path, max_bytes=4)

    def test_retention_preview_filters_changed_files_without_mutation(self):
        self.task()
        good = self.artifact('temp', 'good.md')
        changed = self.artifact('temp', 'changed.md')
        self.call('task_finish', **self.lease)
        Path(changed['path']).write_text('changed')
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=9)
        before = self.call('artifact_list')
        preview = c.retention_preview(self.cfg, future)
        self.assertEqual([row['id'] for row in preview['items']], [good['id']])
        self.assertEqual([row['id'] for row in preview['protected']], [changed['id']])
        self.assertNotIn('inode', preview['items'][0])
        self.assertEqual(before, self.call('artifact_list'))
        self.assertTrue(Path(good['path']).exists())

    def test_retention_pagination_and_count(self):
        self.task()
        artifacts = [self.artifact('temp', 'page%d.md' % n) for n in range(5)]
        self.artifact('report')
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=9)
        self.assertEqual(c.retention_count(self.cfg, future), 0)
        self.call('task_finish', **self.lease)
        # Force equal expiry timestamps to verify the secondary stable ID ordering.
        with c.store(self.cfg) as (con, root):
            con.execute("UPDATE artifacts SET expires_at=? WHERE root=? AND kind='temp'", (artifacts[0]['expires_at'], root))
        expected = sorted(row['id'] for row in artifacts)
        self.assertEqual(c.retention_count(self.cfg, future), 5)
        pages = [c.retention_candidates(self.cfg, future, offset=n, limit=2) for n in (0, 2, 4)]
        self.assertEqual([row['id'] for page in pages for row in page], expected)
        self.assertEqual(c.retention_candidates(self.cfg, future, offset=5, limit=2), [])
        self.call('artifact_pin', artifact_id=expected[0], pinned=True)
        self.assertEqual(c.retention_count(self.cfg, future), 4)
        self.assertEqual(c.retention_count(self.cfg), 0)
        for kwargs in [{'offset': -1}, {'offset': True}, {'offset': 1000000001}, {'offset': 1.5}, {'limit': 0}, {'limit': 1001}, {'limit': False}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                c.retention_candidates(self.cfg, future, **kwargs)

    def test_same_client_cannot_replace_active_lease_and_ui_can_requeue(self):
        task = self.task()
        original_lease = dict(self.lease)
        with self.assertRaises(ValueError):
            self.call('task_claim', task_id=task['id'], client_id='one')
        # The rejected second window must leave the original lease usable.
        artifact = self.artifact()
        with self.assertRaises(ValueError):
            c.execute(self.cfg, 'task_requeue', {'task_id': task['id'], 'summary': 'claim recovery'}, actor='mcp')
        self.artifact(filename='after-rejection.md')
        before = self.call('artifact_list')
        queued = self.call('task_requeue', task_id=task['id'], summary='用户确认连接中断，重新领取')
        self.assertEqual(queued['status'], 'queued')
        self.assertIsNone(queued['owner'])
        self.assertEqual(before, self.call('artifact_list'))
        self.assertEqual(Path(artifact['path']).read_text(), 'test')
        with self.assertRaises(ValueError):
            self.call('task_requeue', task_id=task['id'])
        reclaimed = self.call('task_claim', task_id=task['id'], client_id='one')
        self.assertNotEqual(reclaimed['lease_token'], original_lease['lease_token'])
        with self.assertRaises(ValueError):
            self.call('task_finish', **original_lease)
        self.call('task_finish', **dict(original_lease, lease_token=reclaimed['lease_token']))
        with self.assertRaises(ValueError):
            self.call('task_requeue', task_id=task['id'])
        with c.store(self.cfg) as (con, root):
            audits = list(con.execute("SELECT actor,detail FROM audit WHERE root=? AND action='task_requeue'", (root,)))
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]['actor'], 'ui')
        self.assertIn('连接中断', audits[0]['detail'])


if __name__ == '__main__':
    unittest.main()
