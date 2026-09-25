"""Private, instance-bound desktop shutdown; never controls another process by PID."""
import contextlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import threading
import uuid

PROTOCOL = 'ai-hub-local-control-v1'


class ActivityGate:
    """Admission and idle shutdown share one lock, including background work."""
    def __init__(self):
        self._lock = threading.Lock()
        self._active = 0
        self.stopping = False

    def enter(self):
        with self._lock:
            if self.stopping:
                return False
            self._active += 1
            return True

    def leave(self):
        with self._lock:
            self._active -= 1

    def request_stop(self):
        with self._lock:
            if self._active:
                return False
            self.stopping = True
            return True


GATE = ActivityGate()


def _regular(path):
    if os.path.lexists(path):
        info = os.lstat(path)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('服务控制文件不能是链接或特殊文件。')


def _private(path):
    if os.name != 'nt':
        os.chmod(path, 0o600)
        return
    # Protect the credential before writing it: owner, SYSTEM and administrators.
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (wintypes.LPCWSTR, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD))
    convert.restype = wintypes.BOOL
    apply = advapi.SetFileSecurityW
    apply.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p)
    apply.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    if not convert('D:P(A;;FA;;;OW)(A;;FA;;;SY)(A;;FA;;;BA)', 1,
                   ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not apply(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


@contextlib.contextmanager
def _file_lock(path):
    """Serialize publication/cleanup across services using the same data directory."""
    _regular(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+b') as handle:
        if os.fstat(handle.fileno()).st_nlink != 1:
            raise ValueError('服务控制锁不能是硬链接。')
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ServiceControl:
    def __init__(self, install_root, data_root, port, gate=None):
        self.gate = gate or GATE
        self.record = {'schema': PROTOCOL, 'app': 'ai-hub',
                       'install_root': os.path.realpath(install_root),
                       'data_root': os.path.realpath(data_root), 'pid': os.getpid(),
                       'port': port, 'instance_id': uuid.uuid4().hex,
                       'token': secrets.token_urlsafe(32)}
        self.path = Path(self.record['data_root']) / 'desktop' / 'server-control.json'
        self.lock_path = self.path.with_suffix('.lock')

    def publish(self):
        from . import config
        config._check_ancestors(str(self.path.parent))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name('.server-control-' + self.record['instance_id'] + '.tmp')
        try:
            with _file_lock(self.lock_path):
                _regular(self.path)
                with open(temporary, 'x', encoding='utf-8') as handle:
                    _private(temporary)
                    json.dump(self.record, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def close(self):
        with _file_lock(self.lock_path):
            _regular(self.path)
            try:
                current = json.loads(self.path.read_text(encoding='utf-8'))
            except (FileNotFoundError, ValueError):
                return
            if isinstance(current, dict) and current.get('instance_id') == self.record['instance_id']:
                self.path.unlink()

    def public_identity(self):
        return {'service_instance_id': self.record['instance_id'],
                'install_root': self.record['install_root'], 'control_protocol': PROTOCOL}

    def authorize(self, headers, body):
        # A browser never needs shutdown authority, including a compromised local UI.
        if headers.get('Origin') is not None or headers.get('Sec-Fetch-Site') is not None:
            return False
        token = headers.get('X-AIHub-Control-Token', '')
        if not isinstance(token, str) or not hmac.compare_digest(token.encode('utf-8'), self.record['token'].encode('utf-8')):
            return False
        if not isinstance(body, dict) or body.get('instance_id') != self.record['instance_id']:
            return False
        root = body.get('install_root')
        try:
            return (isinstance(root, str) and os.path.isabs(root)
                    and os.path.normcase(os.path.realpath(root)) == os.path.normcase(self.record['install_root']))
        except (OSError, ValueError):
            return False
