"""Read-only queue and admission tests use temporary stores, never real services."""
import contextlib
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from aihub import collaboration, config, service_control


class UpdateInstallGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='aihub-install-guards-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        self.cfg = {'workspace_managed': True, 'ai_root': str(self.root)}
        self.data = self.base / 'data'
        self.database = self.data / 'collaboration.sqlite3'
        override = patch.object(config, 'DATA_DIR', str(self.data))
        override.start()
        self.addCleanup(override.stop)

    def seed(self, rows):
        self.data.mkdir(exist_ok=True)
        with contextlib.closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('CREATE TABLE tasks(root TEXT, status TEXT)')
            connection.executemany('INSERT INTO tasks VALUES(?,?)', rows)

    def test_no_workspace_or_store_does_not_create_database(self):
        self.assertTrue(collaboration.can_stop({}))
        self.assertTrue(collaboration.can_stop({'workspace_managed': True}))
        self.assertTrue(collaboration.can_stop(self.cfg))
        self.assertFalse(self.data.exists())

    def test_only_current_workspace_queued_and_active_block_stop(self):
        key = config._key(self.root)
        for state, allowed in [('queued', False), ('active', False), ('completed', True)]:
            with self.subTest(state=state):
                if self.database.exists():
                    self.database.unlink()
                self.seed([(key, state), (config._key(self.base / 'other'), 'active')])
                before = hashlib.sha256(self.database.read_bytes()).digest()
                self.assertEqual(collaboration.can_stop(self.cfg), allowed)
                self.assertEqual(hashlib.sha256(self.database.read_bytes()).digest(), before)

    def test_wal_queue_is_visible(self):
        self.seed([])
        with contextlib.closing(sqlite3.connect(self.database)) as writer, writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('INSERT INTO tasks VALUES(?,?)', (config._key(self.root), 'queued'))
            writer.commit()
            self.assertTrue(Path(str(self.database) + '-wal').exists())
            self.assertFalse(collaboration.can_stop(self.cfg))

    def test_corruption_and_missing_schema_fail_closed(self):
        self.data.mkdir()
        self.database.write_bytes(b'not a database')
        gate = service_control.ActivityGate(lambda: collaboration.can_stop(self.cfg))
        with self.assertRaises(sqlite3.Error):
            collaboration.can_stop(self.cfg)
        self.assertFalse(gate.request_stop())
        self.assertFalse(gate.stopping)
        self.database.unlink()
        with contextlib.closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('CREATE TABLE unrelated(value TEXT)')
        self.assertFalse(gate.request_stop())
        self.assertTrue(gate.enter())
        gate.leave()

    def test_guard_only_runs_when_idle_and_requires_literal_true(self):
        calls = []
        gate = service_control.ActivityGate(lambda: calls.append('guard') or True)
        self.assertTrue(gate.enter())
        self.assertFalse(gate.request_stop())
        self.assertEqual(calls, [])
        gate.leave()
        for result in [False, None, 1, 'true']:
            gate.set_stop_guard(lambda: result)
            self.assertFalse(gate.request_stop())
        gate.set_stop_guard(lambda: True)
        self.assertTrue(gate.request_stop())
        self.assertFalse(gate.enter())

    def test_guard_exception_retains_admission(self):
        def unavailable():
            raise OSError('fixture unavailable')
        gate = service_control.ActivityGate(unavailable)
        self.assertFalse(gate.request_stop())
        self.assertTrue(gate.enter())
        gate.leave()
        gate.set_stop_guard(None)
        self.assertTrue(gate.request_stop())

    def test_task_creation_admitted_first_blocks_idle_stop(self):
        self.seed([])
        gate = service_control.ActivityGate(lambda: collaboration.can_stop(self.cfg))
        self.assertTrue(gate.enter())
        self.assertFalse(gate.request_stop())
        with contextlib.closing(sqlite3.connect(self.database)) as writer, writer:
            writer.execute('INSERT INTO tasks VALUES(?,?)', (config._key(self.root), 'queued'))
        gate.leave()
        self.assertFalse(gate.request_stop())
        self.assertFalse(gate.stopping)

    def test_guard_holds_admission_lock_until_stop_is_committed(self):
        self.seed([])
        checking, release, attempted, entered = (threading.Event() for _ in range(4))
        def check():
            checking.set()
            if not release.wait(3):
                raise TimeoutError('fixture release missing')
            return collaboration.can_stop(self.cfg)
        gate = service_control.ActivityGate(check)
        results = {}
        def stop():
            results['stop'] = gate.request_stop()
        def create():
            attempted.set()
            results['admitted'] = gate.enter()
            entered.set()
            if results['admitted']:
                try:
                    with contextlib.closing(sqlite3.connect(self.database)) as writer, writer:
                        writer.execute('INSERT INTO tasks VALUES(?,?)', (config._key(self.root), 'queued'))
                finally:
                    gate.leave()
        stopper = threading.Thread(target=stop)
        creator = threading.Thread(target=create)
        stopper.start()
        try:
            self.assertTrue(checking.wait(2))
            creator.start()
            self.assertTrue(attempted.wait(2))
            self.assertFalse(entered.wait(.05))
        finally:
            release.set()
            stopper.join(3)
            if creator.ident is not None:
                creator.join(3)
        self.assertFalse(stopper.is_alive())
        self.assertFalse(creator.is_alive())
        self.assertEqual(results, {'stop': True, 'admitted': False})
        self.assertTrue(collaboration.can_stop(self.cfg))


if __name__ == '__main__':
    unittest.main()
