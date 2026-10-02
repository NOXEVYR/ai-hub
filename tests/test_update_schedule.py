"""Persistent timing and scheduler admission; isolated files and no network."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from aihub import app_update, service_control, update_schedule as policy


class SchedulerAdmissionTests(unittest.TestCase):
    def test_worker_completion_write_holds_admission_until_durable(self):
        with tempfile.TemporaryDirectory(prefix='aihub-update-completion-') as temporary:
            gate = service_control.ActivityGate()
            manager = app_update.UpdateManager(temporary, '2.13.7', gate=gate)
            entered, release = threading.Event(), threading.Event()
            writer = manager._schedule_state._writer
            def blocked_writer(path, value):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('synthetic completion write was not released')
                writer(path, value)
            self.assertTrue(manager._schedule_state.started())
            manager._schedule_state._writer = blocked_writer
            try:
                manager._start_worker('check', lambda: None)
                self.assertTrue(entered.wait(2))
                self.assertFalse(gate.request_stop())
                release.set()
                manager.close()
                self.assertEqual(json.loads((manager.base / 'schedule.json').read_text())['failures'], 0)
                self.assertTrue(gate.request_stop())
                self.assertFalse(manager.status()['operation_active'])
            finally:
                release.set()
                manager.close()

    def test_completion_exception_releases_admission_and_worker_state(self):
        with tempfile.TemporaryDirectory(prefix='aihub-update-completion-failure-') as temporary:
            gate = service_control.ActivityGate()
            manager = app_update.UpdateManager(temporary, '2.13.7', gate=gate)
            manager._worker = threading.current_thread()
            manager._worker_kind = 'check'
            try:
                with mock.patch.object(manager._schedule_state, 'finished', side_effect=RuntimeError('synthetic failure')):
                    with self.assertRaises(RuntimeError):
                        manager._worker_entry('check', lambda: None)
                self.assertIsNone(manager._worker)
                self.assertIsNone(manager._worker_kind)
                self.assertTrue(gate.request_stop())
            finally:
                manager.close()

    def test_refused_worker_performs_no_work_or_persistent_completion(self):
        with tempfile.TemporaryDirectory(prefix='aihub-update-refused-') as temporary:
            gate = service_control.ActivityGate()
            manager = app_update.UpdateManager(temporary, '2.13.7', gate=gate)
            self.assertTrue(manager._schedule_state.started())
            before = (manager.base / 'schedule.json').read_bytes()
            self.assertTrue(gate.request_stop())
            try:
                with mock.patch.object(manager, '_persist_locked') as persist, mock.patch.object(
                        manager._schedule_state, 'finished') as finished:
                    operation = mock.Mock()
                    manager._worker_entry('check', operation)
                    operation.assert_not_called()
                    persist.assert_not_called()
                    finished.assert_not_called()
                self.assertEqual((manager.base / 'schedule.json').read_bytes(), before)
            finally:
                manager.close()

    def test_schedule_writes_block_exit_sleep_allows_exit_and_stop_refuses_new_writes(self):
        for phase in ('reservation', 'clock-repair'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(prefix='aihub-scheduler-gate-') as temporary:
                gate = service_control.ActivityGate()
                manager = app_update.UpdateManager(temporary, '2.13.7', gate=gate)
                entered, release, sleeping = threading.Event(), threading.Event(), threading.Event()
                now = time.time()
                manager._ui_ready_at = now - 61
                # Clock repairs occur in delay() even when automatic checks are
                # disabled; they must receive the same admission as reservations.
                manager._settings['auto_check'] = phase == 'reservation'
                manager._schedule_state._clock = lambda: now if phase == 'reservation' else now - 1000
                writer = manager._schedule_state._writer
                writes = []
                def blocked_writer(path, value):
                    writes.append(dict(value))
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError('synthetic schedule write was not released')
                    writer(path, value)
                manager._schedule_state._writer = blocked_writer
                wait = manager._wake_scheduler.wait
                waits = []
                def observed_wait(timeout):
                    waits.append(timeout)
                    if len(waits) > 1:
                        sleeping.set()
                    return wait(timeout)
                try:
                    with mock.patch.object(manager, '_start_worker') as start, mock.patch.object(
                            manager._wake_scheduler, 'wait', side_effect=observed_wait):
                        # Disabled checks may still have a sleeping existing scheduler.
                        manager._scheduler = threading.Thread(target=manager._schedule, daemon=True)
                        manager._scheduler.start()
                        manager._wake_scheduler.set()
                        self.assertTrue(entered.wait(2))
                        self.assertFalse(gate.request_stop())
                        self.assertFalse(gate.stopping)
                        release.set()
                        self.assertTrue(sleeping.wait(2))
                        self.assertTrue((manager.base / 'schedule.json').is_file())
                        before = (manager.base / 'schedule.json').read_bytes()
                        write_count = len(writes)
                        self.assertTrue(gate.request_stop())
                        manager._wake_scheduler.set()
                        manager._scheduler.join(2)
                        self.assertFalse(manager._scheduler.is_alive())
                        self.assertEqual(len(writes), write_count)
                        self.assertEqual((manager.base / 'schedule.json').read_bytes(), before)
                        self.assertEqual(start.call_count, int(phase == 'reservation'))
                finally:
                    release.set()
                    manager.close()


class UpdateScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "updates" / "schedule.json"
        self.now = 2_000_000.0

    def schedule(self, **kwargs):
        return policy.UpdateSchedule(self.path, clock=lambda: self.now, **kwargs)

    def write(self, value):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_new_install_requires_explicit_ui_ready_and_sixty_seconds(self):
        schedule = self.schedule()
        ready_at = self.now
        self.now += 100
        self.assertFalse(schedule.due(None))
        self.assertFalse(self.path.exists())
        self.now = ready_at + 59
        self.assertEqual(schedule.delay(ready_at), 1)
        self.assertFalse(schedule.due(ready_at))
        self.now += 1
        self.assertTrue(schedule.due(ready_at))

    def test_success_waits_twenty_four_hours_across_restarts(self):
        schedule = self.schedule()
        self.assertTrue(schedule.started())
        self.now += 10
        self.assertTrue(schedule.finished(True))
        deadline = self.now + policy.CHECK_INTERVAL
        self.now += 100
        ready_at = self.now
        restarted = self.schedule()
        self.assertEqual(restarted.next_check_at, deadline)
        self.assertEqual(restarted.failure_count, 0)
        self.assertFalse(restarted.due(ready_at))
        self.now = deadline - 1
        self.assertFalse(restarted.due(ready_at))
        self.now += 1
        self.assertTrue(restarted.due(ready_at))

    def test_failures_double_from_fifteen_minutes_and_cap_at_one_day(self):
        for interval in (900, 1800, 3600, 7200, 14400, 28800, 57600, 86400, 86400):
            schedule = self.schedule()
            self.assertTrue(schedule.started())
            self.now += 2
            self.assertTrue(schedule.finished(False))
            self.assertEqual(schedule.next_check_at, self.now + interval)
            restarted = self.schedule()
            self.now += interval - 1
            self.assertFalse(restarted.due(self.now - 60))
            self.now += 1
            self.assertTrue(restarted.due(self.now - 60))
        self.assertEqual(restarted.failure_count, policy.MAX_FAILURES)
        self.assertTrue(restarted.started())
        self.assertTrue(restarted.finished(True))
        self.assertEqual(restarted.failure_count, 0)
        self.assertTrue(restarted.started())
        self.assertEqual(restarted.next_check_at, self.now + 900)

    def test_started_persists_before_request_and_crash_keeps_retry(self):
        schedule = self.schedule()
        self.assertTrue(schedule.started())
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["next_check_at"], self.now + 900)
        self.assertEqual(saved["failures"], 1)
        self.now += 10
        restarted = self.schedule()
        self.assertFalse(restarted.due(self.now - 60))
        self.now = saved["next_check_at"]
        self.assertTrue(restarted.due(self.now - 60))
        self.assertTrue(restarted.started())
        self.assertEqual(restarted.next_check_at, self.now + 1800)

    def test_manual_check_bypasses_deadline_and_readiness_but_updates_timing(self):
        schedule = self.schedule()
        schedule.started()
        schedule.finished(True)
        self.now += 5
        self.assertFalse(schedule.due(None))
        self.assertFalse(schedule.due(self.now - 60))
        self.assertTrue(schedule.started())
        self.assertEqual(schedule.next_check_at, self.now + 900)
        self.assertFalse(schedule.started())
        self.now += 10
        self.assertTrue(schedule.finished(False))
        self.assertEqual(self.schedule().next_check_at, self.now + 900)

    def test_corrupt_records_repair_once_and_do_not_reset_wait_on_restart(self):
        values = ["truncated", [], {}, {"schema": policy.SCHEMA, "saved_at": self.now,
                  "next_check_at": self.now + 10, "failures": True},
                  {"schema": policy.SCHEMA, "saved_at": self.now,
                   "next_check_at": self.now + 10, "failures": 1000000},
                  {"schema": policy.SCHEMA, "saved_at": float("nan"),
                   "next_check_at": self.now + 10, "failures": 1}]
        for value in values:
            with self.subTest(value=value):
                self.write(value)
                schedule = self.schedule()
                deadline = self.now + 900
                self.assertEqual(schedule.last_error, "invalid_schedule")
                self.assertEqual(schedule.next_check_at, deadline)
                self.now += 61
                restarted = self.schedule()
                self.assertEqual(restarted.next_check_at, deadline)
                self.assertFalse(restarted.due(self.now - 60))
                self.now = deadline
                self.assertTrue(restarted.due(self.now - 60))

    def test_invalid_far_future_deadline_repairs_to_bounded_retry(self):
        self.write({"schema": policy.SCHEMA, "saved_at": self.now,
                    "next_check_at": self.now + 100 * policy.CHECK_INTERVAL, "failures": 0})
        schedule = self.schedule()
        self.assertEqual(schedule.next_check_at, self.now + 900)
        self.now += 100
        self.assertEqual(self.schedule().next_check_at, schedule.next_check_at)

    def test_truncated_json_is_repaired_before_any_request(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b'{"schema":')
        schedule = self.schedule()
        self.assertEqual(schedule.next_check_at, self.now + 900)
        self.assertFalse(schedule.due(self.now - 60))
        self.now += 61
        self.assertEqual(self.schedule().next_check_at, schedule.next_check_at)

    def test_future_saved_clock_is_rebased_once_with_bounded_wait(self):
        for failures, interval in ((0, policy.CHECK_INTERVAL), (2, 1800)):
            with self.subTest(failures=failures):
                self.write({"schema": policy.SCHEMA, "saved_at": self.now + 1000000,
                            "next_check_at": self.now + 1000000 + interval, "failures": failures})
                schedule = self.schedule()
                self.assertEqual(schedule.last_error, "clock_moved_backwards")
                self.assertEqual(schedule.next_check_at, self.now + interval)
                self.now += 100
                self.assertEqual(self.schedule().next_check_at, schedule.next_check_at)

    def test_running_clock_rollback_rebases_persistently_and_forward_jump_allows_due(self):
        schedule = self.schedule()
        schedule.started()
        schedule.finished(True)
        self.now -= 7 * policy.CHECK_INTERVAL
        self.assertFalse(schedule.due(self.now - 60))
        self.assertEqual(schedule.next_check_at, self.now + policy.CHECK_INTERVAL)
        self.assertEqual(self.schedule().next_check_at, schedule.next_check_at)
        self.now += 100 * policy.CHECK_INTERVAL
        self.assertTrue(schedule.due(self.now - 60))

    def test_persistence_failure_prevents_request_and_status_properties_do_not_write(self):
        writer = mock.Mock(side_effect=OSError("disk unavailable"))
        schedule = self.schedule(writer=writer)
        self.assertFalse(schedule.started())
        self.assertEqual(schedule.last_error, "schedule_persistence_failed")
        self.now += 2 * policy.CHECK_INTERVAL
        self.assertFalse(schedule.due(self.now - 60))
        before = writer.call_count
        self.assertGreater(schedule.next_check_at, 0)
        self.assertGreater(schedule.failure_count, 0)
        self.assertIsNotNone(schedule.last_error)
        self.assertEqual(writer.call_count, before)
        writer.side_effect = policy._atomic_json
        self.assertTrue(schedule.started())
        self.assertTrue(self.path.is_file())

    def test_corrupt_record_with_failed_repair_stays_blocked(self):
        self.write({})
        writer = mock.Mock(side_effect=OSError("disk full"))
        schedule = self.schedule(writer=writer)
        self.now += policy.CHECK_INTERVAL
        self.assertFalse(schedule.due(self.now - 60))
        self.assertFalse(schedule.started())
        self.assertEqual(schedule.last_error, "schedule_persistence_failed")

    def test_failed_completion_keeps_original_reservation_after_restart(self):
        writer = mock.Mock(side_effect=policy._atomic_json)
        schedule = self.schedule(writer=writer)
        self.assertTrue(schedule.started())
        deadline = schedule.next_check_at
        writer.side_effect = OSError("disk full")
        self.now += 5
        self.assertFalse(schedule.finished(True))
        restarted = self.schedule()
        self.assertEqual(restarted.next_check_at, deadline)
        self.assertFalse(restarted.due(self.now - 60))

    def test_atomic_write_failure_keeps_previous_record_and_cleans_temp(self):
        schedule = self.schedule()
        schedule.started()
        schedule.finished(True)
        previous = self.path.read_bytes()
        with mock.patch.object(policy.os, "replace", side_effect=OSError("replace denied")):
            self.assertFalse(schedule.started())
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_oversized_record_is_bounded_and_repaired_without_copying_extra_fields(self):
        self.write({"junk": "x" * (policy.MAX_STATE_BYTES + 100)})
        schedule = self.schedule()
        self.assertFalse(schedule.due(self.now - 60))
        self.assertLess(self.path.stat().st_size, policy.MAX_STATE_BYTES)
        self.assertEqual(set(json.loads(self.path.read_text(encoding="utf-8"))),
                         {"schema", "saved_at", "next_check_at", "failures"})

    def test_injected_existing_updater_readers_and_writers(self):
        from aihub import app_update
        schedule = self.schedule(reader=app_update._read_json, writer=app_update._atomic_json)
        self.assertTrue(schedule.started())
        self.assertTrue(schedule.finished(True))
        restarted = self.schedule(reader=app_update._read_json, writer=app_update._atomic_json)
        self.assertEqual(restarted.next_check_at, schedule.next_check_at)

    def test_reparse_file_and_directory_are_rejected_without_symlink_privilege(self):
        self.write({})
        original_lstat = os.lstat
        for target in (self.path, self.path.parent):
            with self.subTest(target=target):
                def marked_lstat(path, *args, **kwargs):
                    info = original_lstat(path, *args, **kwargs)
                    if Path(path) == target:
                        return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink,
                                               st_size=info.st_size, st_file_attributes=0x400)
                    return info
                with mock.patch.object(policy.os, "lstat", side_effect=marked_lstat):
                    schedule = self.schedule()
                    self.assertFalse(schedule.started())
                    self.assertFalse(schedule.due(self.now - 60))
                self.assertEqual(self.path.read_text(encoding="utf-8"), "{}")

    def test_hardlinked_state_is_rejected_without_changing_target(self):
        self.path.parent.mkdir()
        target = self.path.parent / "original.json"
        target.write_text("{}", encoding="utf-8")
        try:
            os.link(target, self.path)
        except OSError as error:
            self.skipTest(str(error))
        schedule = self.schedule()
        self.assertFalse(schedule.due(self.now - 60))
        self.assertFalse(schedule.started())
        self.assertEqual(target.read_text(encoding="utf-8"), "{}")

    def test_linked_state_or_parent_is_rejected_without_writing_target(self):
        target = Path(self.temporary.name) / "target"
        target.mkdir()
        target_file = target / "schedule.json"
        target_file.write_text("{}", encoding="utf-8")
        self.path.parent.mkdir()
        try:
            self.path.symlink_to(target_file)
        except OSError as error:
            self.skipTest(str(error))
        schedule = self.schedule()
        self.assertFalse(schedule.started())
        self.assertEqual(target_file.read_text(encoding="utf-8"), "{}")
        self.path.unlink()
        self.path.parent.rmdir()
        self.path.parent.symlink_to(target, target_is_directory=True)
        self.assertFalse(self.schedule().started())
        self.assertEqual(target_file.read_text(encoding="utf-8"), "{}")


if __name__ == "__main__":
    unittest.main()
