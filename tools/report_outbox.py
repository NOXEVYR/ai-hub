"""Bounded, private report delivery spool. Never persist authentication or leases."""
import hashlib
import base64
import json
import os
from pathlib import Path
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager

MAX_FILE = 2 * 1024 * 1024
MAX_ITEMS = 32
MAX_ATTEMPTS = 8
MAX_BINDINGS = 256
MAX_RECEIPTS = 64
MAX_RECORDS = MAX_ITEMS + MAX_RECEIPTS
MAX_METADATA = 16384
ERROR_CODES = {None, 'network_unavailable', 'response_invalid', 'server_busy',
               'rejected', 'needs_lease', 'task_unavailable', 'task_not_active',
               'ownership_changed', 'retry_limit', 'storage_unavailable', 'workspace_changed', 'submission_invalid', 'submission_conflict'}
PAYLOAD_KEYS = {'task_id', 'category', 'title', 'filename', 'content', 'memory_candidates', 'submission_id'}


class OutboxError(Exception):
    """Intentionally contains no path, body, or original exception."""


def default_dir():
    if os.name == 'nt':
        base = Path(os.environ.get('APPDATA') or Path.home() / 'AppData/Roaming')
    else:
        base = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    return base / 'AIHub/report-outbox'


def _ordinary(path, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise OutboxError('storage_unavailable')
    return info


def _ancestors(path):
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            _ordinary(item, directory=True)


def _private(path):
    if os.name != 'nt':
        os.chmod(path, 0o700)
        return
    # Set an exact protected DACL on our dedicated spool directory. No native
    # harness configuration is inspected. All arguments remain literal argv.
    script = ("$p='" + str(path).replace("'", "''") + "'; $sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User; "
              "$acl=New-Object System.Security.AccessControl.DirectorySecurity; "
              "$acl.SetOwner($sid); $acl.SetAccessRuleProtection($true,$false); "
              "foreach($s in @($sid,(New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')))){ "
              "$rule=New-Object System.Security.AccessControl.FileSystemAccessRule($s,'FullControl','ContainerInherit,ObjectInherit','None','Allow'); "
              "$acl.AddAccessRule($rule) }; [System.IO.Directory]::SetAccessControl($p,$acl)")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                             base64.b64encode(script.encode('utf-16-le')).decode('ascii')], capture_output=True, timeout=10,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise OutboxError('storage_unavailable')


def _id(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise OutboxError('storage_unavailable') from None


def _read(path):
    info = _ordinary(path)
    if info.st_size > MAX_FILE or info.st_nlink != 1:
        raise OutboxError('storage_unavailable')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise OutboxError('storage_unavailable')
        data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise OutboxError('storage_unavailable')
    entry = json.loads(data.rsplit(b'\n', 1)[-1].decode('utf-8'))
    if not isinstance(entry, dict) or entry.get('id') != path.stem:
        raise OutboxError('storage_unavailable')
    return entry


def _read_header(path):
    info = _ordinary(path)
    if info.st_size > MAX_FILE or info.st_nlink != 1:
        raise OutboxError('storage_unavailable')
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise OutboxError('storage_unavailable')
        header = stream.readline(MAX_METADATA + 1)
    if len(header) > MAX_METADATA:
        raise OutboxError('storage_unavailable')
    entry = json.loads(header.decode('utf-8'))
    if not isinstance(entry, dict) or entry.get('id') != path.stem:
        raise OutboxError('storage_unavailable')
    return entry


def _metadata(entry):
    if (not isinstance(entry, dict) or entry.get('status') not in {'pending', 'failed', 'submitted'}
            or type(entry.get('attempts')) is not int or not 0 <= entry['attempts'] <= MAX_ATTEMPTS
            or entry.get('error_code') not in ERROR_CODES):
        raise OutboxError('storage_unavailable')
    result = {'id': _id(entry.get('id')), 'status': entry['status'],
              'attempts': entry['attempts'], 'error_code': entry.get('error_code')}
    for key, maximum in [('client_id', 80), ('tool', 64), ('task_id', 4096), ('title', 200)]:
        value = entry.get(key)
        if not isinstance(value, str) or len(value) > maximum:
            raise OutboxError('storage_unavailable')
        result[key] = ''.join(c if ord(c) >= 32 else ' ' for c in value)
    return result


def _check_payload(payload):
    if not isinstance(payload, dict) or set(payload) - PAYLOAD_KEYS:
        raise OutboxError('storage_unavailable')
    if not payload:
        return
    if set(payload) != PAYLOAD_KEYS:
        raise OutboxError('storage_unavailable')
    for key in PAYLOAD_KEYS - {'memory_candidates'}:
        if not isinstance(payload[key], str):
            raise OutboxError('storage_unavailable')
    if len(payload['content'].encode('utf-8')) > 1048576:
        raise OutboxError('storage_unavailable')
    candidates = payload['memory_candidates']
    if not isinstance(candidates, list) or len(candidates) > 5:
        raise OutboxError('storage_unavailable')
    for candidate in candidates:
        if (not isinstance(candidate, dict) or set(candidate) != {'title', 'content', 'scope'}
                or not all(isinstance(value, str) for value in candidate.values())):
            raise OutboxError('storage_unavailable')


def _digest(payload):
    # Exact server artifact_write normalization; routing and bearer values do
    # not identify the report contents. Never include the lease in a digest.
    normalized = {key: value for key, value in payload.items()
                  if key not in {'lease_token', 'client_id', 'task_id', '_workspace_root', 'submission_id'}}
    normalized['kind'] = 'report'
    return hashlib.sha256(json.dumps({'action': 'artifact_write', 'payload': normalized},
        sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


class ReportOutbox:
    def __init__(self, workspace_root, client_id, tool, base_dir=None):
        self.root, self.client_id, self.tool = workspace_root, client_id, tool
        self.base = Path(base_dir) if base_dir is not None else default_dir()
        if not self.base.is_absolute():
            raise OutboxError('storage_unavailable')
        binding = json.dumps([workspace_root, client_id, tool], ensure_ascii=False)
        self.path = self.base / hashlib.sha256(binding.encode('utf-8')).hexdigest()
        try:
            _ancestors(self.base)
            self.base.mkdir(parents=True, exist_ok=True)
            _private(self.base)
            if not self.path.exists():
                with os.scandir(self.base) as bindings:
                    for index, _ in enumerate(bindings):
                        if index >= MAX_BINDINGS - 1:
                            raise OutboxError('storage_unavailable')
            _ancestors(self.path)
            self.path.mkdir(exist_ok=True)
            _private(self.path)
        except (OSError, subprocess.SubprocessError):
            raise OutboxError('storage_unavailable') from None

    @contextmanager
    def locked(self):
        """Nonblocking cross-process exclusion: never create background workers."""
        _ancestors(self.path)
        target = self.path / '.delivery.lock'
        if target.exists() or target.is_symlink():
            if _ordinary(target).st_nlink != 1:
                raise OutboxError('storage_unavailable')
        descriptor = os.open(target, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b'0')
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == 'nt':
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _files(self):
        _ancestors(self.path)
        files = []
        with os.scandir(self.path) as rows:
            for index, row in enumerate(rows):
                if index >= (MAX_RECORDS + 1) * 2:
                    raise OutboxError('storage_unavailable')
                if row.name.endswith('.json'):
                    _id(row.name[:-5])
                    files.append(self.path / row.name)
        if len(files) > MAX_RECORDS + 1:
            raise OutboxError('storage_unavailable')
        return files

    def load(self, identifier):
        _ancestors(self.path)
        entry = _read(self.path / (_id(identifier) + '.json'))
        _metadata(entry)
        if (entry.get('workspace_root'), entry.get('client_id'), entry.get('tool')) != (self.root, self.client_id, self.tool):
            raise OutboxError('workspace_changed')
        _check_payload(entry.get('payload'))
        if entry['payload'] and _digest(entry['payload']) != entry.get('payload_hash'):
            raise OutboxError('storage_unavailable')
        return entry

    def save(self, entry):
        _ancestors(self.path)
        _metadata(entry)
        _check_payload(entry.get('payload'))
        if entry['status'] == 'submitted':
            entry['payload'] = {}  # confirmation ends the need to retain report text
        header = dict(_metadata(entry), workspace_root=entry['workspace_root'])
        if len(json.dumps(header, ensure_ascii=False).encode('utf-8')) >= MAX_METADATA:
            raise OutboxError('storage_unavailable')
        data = (json.dumps(header, ensure_ascii=False) + '\n' + json.dumps(entry, ensure_ascii=False)).encode('utf-8')
        if len(data) > MAX_FILE:
            raise OutboxError('storage_unavailable')
        target = self.path / (_id(entry['id']) + '.json')
        if target.exists() or target.is_symlink():
            if _ordinary(target).st_nlink != 1:
                raise OutboxError('storage_unavailable')
        temporary = self.path / (str(uuid.uuid4()) + '.tmp')
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                   getattr(os, 'O_BINARY', 0), 0o600), 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _ancestors(self.path)
            os.replace(temporary, target)
            if entry['status'] == 'submitted':
                receipts = []
                for record in self._files():
                    metadata = _read_header(record)
                    if metadata.get('status') == 'submitted':
                        receipts.append(record)
                receipts.sort(key=lambda record: record.stat().st_mtime_ns, reverse=True)
                for record in receipts[MAX_RECEIPTS:]:
                    # Only confirmed compact receipts expire. Failed/pending
                    # report bodies are never evicted to make room.
                    if self.load(record.stem)['status'] == 'submitted':
                        record.unlink()
        finally:
            if temporary.exists():
                temporary.unlink()

    def stage(self, payload):
        if set(payload) - PAYLOAD_KEYS - {'lease_token'}:
            raise OutboxError('storage_unavailable')
        identifier = _id(payload.get('submission_id')) if payload.get('submission_id') else str(uuid.uuid4())
        safe = {key: value for key, value in payload.items() if key in PAYLOAD_KEYS}
        safe['submission_id'] = identifier
        _check_payload(safe)
        fingerprint = _digest(safe)
        if len(safe.get('content', '').encode('utf-8')) > 1048576:
            raise OutboxError('storage_unavailable')
        try:
            existing = self.load(identifier)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing.get('payload_hash') != fingerprint or existing.get('task_id') != safe['task_id']:
                if existing.get('payload') or existing.get('task_id') != safe['task_id']:
                    # Preserve an earlier failed body and its task binding.
                    raise OutboxError('submission_conflict')
                # Replacing our compact receipt with the new staged body loses
                # no report text. The server still decides whether it matches.
            else:
                return existing
        if sum(_read_header(path).get('status') != 'submitted' for path in self._files()) >= MAX_ITEMS:
            raise OutboxError('storage_unavailable')
        entry = {'id': identifier, 'workspace_root': self.root, 'client_id': self.client_id,
                 'tool': self.tool, 'task_id': safe['task_id'], 'title': safe['title'][:200],
                 'status': 'pending', 'attempts': 0, 'error_code': None, 'payload': safe, 'payload_hash': fingerprint}
        self.save(entry)
        return entry

    def metadata(self, entry):
        return _metadata(entry)

    def list(self):
        return [_metadata(_read_header(path)) for path in self._files()]


def list_current(workspace_root, base_dir=None):
    """Read current-workspace metadata only; never create a spool during inspection."""
    base = Path(base_dir) if base_dir is not None else default_dir()
    items, partial, truncated = [], False, False
    inspected, deadline = 0, time.monotonic() + .25
    try:
        _ancestors(base)
        if not base.exists():
            return {'items': [], 'error_code': None, 'partial': False, 'truncated': False}
        with os.scandir(base) as bindings:
            for index, binding in enumerate(bindings):
                if index >= MAX_BINDINGS:
                    truncated = True
                    break
                if not re_binding(binding.name):
                    continue
                directory = base / binding.name
                try:
                    _ordinary(directory, directory=True)
                except (OutboxError, OSError):
                    partial = True
                    continue
                with os.scandir(directory) as files:
                    for count, file in enumerate(files):
                        if (count >= (MAX_RECORDS + 1) * 2 or inspected >= 512
                                or len(items) >= 200 or time.monotonic() >= deadline):
                            truncated = True
                            break
                        if not file.name.endswith('.json'):
                            continue
                        inspected += 1
                        try:
                            _id(file.name[:-5])
                            entry = _read_header(directory / file.name)
                        except (OutboxError, OSError, ValueError, UnicodeError):
                            partial = True
                            continue
                        if entry.get('workspace_root') == workspace_root:
                            expected = hashlib.sha256(json.dumps([workspace_root, entry.get('client_id'), entry.get('tool')], ensure_ascii=False).encode('utf-8')).hexdigest()
                            if binding.name != expected:
                                raise OutboxError('storage_unavailable')
                            try:
                                items.append(_metadata(entry))
                            except OutboxError:
                                partial = True
                    if truncated:
                        break
        return {'items': items, 'error_code': None, 'partial': partial, 'truncated': truncated}
    except (OutboxError, OSError, ValueError, UnicodeError):
        return {'items': items, 'error_code': 'storage_unavailable', 'partial': True, 'truncated': truncated}


def re_binding(value):
    return len(value) == 64 and all(c in '0123456789abcdef' for c in value)
