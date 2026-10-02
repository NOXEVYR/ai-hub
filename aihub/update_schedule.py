"""Small, persistent update-check timing policy; no threads or network I/O.

All times are Unix seconds. The manager owns UI readiness, enable/disable and
worker lifetime. Call ``started()`` before any request (including a manual one)
and proceed only when it returns True; then call ``finished(success)`` once.
An interrupted check counts as a failure because its retry is reserved first.
Production callers can inject their existing guarded JSON reader and writer.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
from pathlib import Path
import stat
import threading
import time
import uuid

SCHEMA = "ai-hub-update-schedule-v1"
UI_DELAY = 60
RETRY_DELAY = 15 * 60
CHECK_INTERVAL = 24 * 60 * 60
MAX_FAILURES = 8
MAX_STATE_BYTES = 4096


def _guard_parent(path):
    for item in reversed((path.parent, *path.parent.parents)):
        try:
            info = os.lstat(item)
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
                or not stat.S_ISDIR(info.st_mode)):
            raise ValueError("Update schedule directory must not contain links.")


def _guard_file(path, max_bytes=None):
    info = os.lstat(path)
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (max_bytes is not None and info.st_size > max_bytes)):
        raise ValueError("Update schedule must be a bounded ordinary file.")


def _read_json(path, max_bytes):
    _guard_parent(path)
    _guard_file(path, max_bytes)
    with open(path, "rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("Update schedule exceeds its size limit.")
    return json.loads(raw)


def _atomic_json(path, value):
    _guard_parent(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _guard_parent(path)
    if os.path.lexists(path):
        _guard_file(path)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        _guard_parent(path)
        if os.path.lexists(path):
            _guard_file(path)
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _backoff(failures):
    return min(CHECK_INTERVAL, RETRY_DELAY * 2 ** (max(1, failures) - 1))


class UpdateSchedule:
    """Reserve retries durably; persistence errors fail closed.

    ``reader(path, max_bytes)`` and ``writer(path, value)`` must use bounded
    reads and atomic writes, rejecting reparse points, symlinks and hardlinks.
    ``due``/``delay`` may repair a backwards clock, but status properties never
    perform I/O. No state file is created for a fresh, unused schedule.
    """

    def __init__(self, path, clock=time.time, reader=None, writer=None):
        self.path = Path(os.path.abspath(path))
        self._clock = clock
        self._reader = reader or _read_json
        self._writer = writer or _atomic_json
        self._lock = threading.RLock()
        self._active = False
        self._blocked = False
        self._last_error = None
        now = self._now()
        self._saved_at = now
        self._next_check_at = 0.0
        self._failures = 0
        try:
            saved = self._reader(self.path, MAX_STATE_BYTES)
            self._load(saved)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
            self._persist(now, now + RETRY_DELAY, 1, "invalid_schedule")
        self._repair_clock(now)

    def _now(self):
        value = self._clock()
        if not _timestamp(value):
            raise ValueError("Clock must return finite, nonnegative Unix seconds.")
        return float(value)

    def _load(self, saved):
        if not isinstance(saved, dict) or saved.get("schema") != SCHEMA:
            raise ValueError("Unknown update schedule schema.")
        recorded, deadline, failures = (saved.get("saved_at"), saved.get("next_check_at"),
                                        saved.get("failures"))
        if (not _timestamp(recorded) or not _timestamp(deadline)
                or type(failures) is not int or not 0 <= failures <= MAX_FAILURES
                or not 0 <= deadline - recorded <= CHECK_INTERVAL):
            raise ValueError("Invalid update schedule timing.")
        self._saved_at = float(recorded)
        self._next_check_at = float(deadline)
        self._failures = failures

    def _persist(self, now, deadline, failures, error=None):
        value = {"schema": SCHEMA, "saved_at": now, "next_check_at": deadline,
                 "failures": failures}
        self._saved_at, self._next_check_at, self._failures = now, deadline, failures
        try:
            self._writer(self.path, value)
        except (OSError, ValueError, TypeError, OverflowError):
            self._blocked = True
            self._last_error = "schedule_persistence_failed"
            return False
        self._blocked = False
        self._last_error = error
        return True

    def _repair_clock(self, now):
        if not self._blocked and now < self._saved_at:
            interval = _backoff(self._failures) if self._failures else CHECK_INTERVAL
            self._persist(now, now + interval, self._failures, "clock_moved_backwards")

    @property
    def next_check_at(self):
        with self._lock:
            return self._next_check_at

    @property
    def failure_count(self):
        with self._lock:
            return self._failures

    @property
    def last_error(self):
        with self._lock:
            return self._last_error

    def delay(self, ui_ready_at):
        """Seconds until automatic checking is allowed; always a bounded wait."""
        with self._lock:
            now = self._now()
            self._repair_clock(now)
            if self._blocked or self._active:
                return CHECK_INTERVAL
            if not _timestamp(ui_ready_at):
                return UI_DELAY
            return min(CHECK_INTERVAL, max(0.0, self._next_check_at - now,
                                           float(ui_ready_at) + UI_DELAY - now))

    def due(self, ui_ready_at):
        return self.delay(ui_ready_at) == 0

    def started(self):
        """Reserve the next retry before I/O; manual callers bypass only due()."""
        with self._lock:
            if self._active:
                return False
            now = self._now()
            failures = min(MAX_FAILURES, self._failures + 1)
            if not self._persist(now, now + _backoff(failures), failures):
                return False
            self._active = True
            return True

    def finished(self, success):
        """Finish one reserved request. An unfinished reservation remains valid."""
        with self._lock:
            if not self._active:
                return False
            now = self._now()
            failures = 0 if success else self._failures
            interval = CHECK_INTERVAL if success else _backoff(failures)
            self._active = False
            return self._persist(now, now + interval, failures)
