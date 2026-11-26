"""Explicit file ownership and bounded, read-only lifecycle previews.

This module has no deletion, migration or scheduling entrypoint. The separate
ledger contains metadata only; scanning never changes files or existing labels.
"""
import datetime as dt
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time

from . import config

KINDS = {'project', 'task', 'tool', 'artifact'}
STATES = {'active', 'retained', 'completed', 'expired', 'archived'}
PURPOSES = {'unknown', 'source', 'work', 'input', 'output', 'delivery', 'cache', 'temporary', 'configuration', 'model', 'archive'}
SOURCE = {'.py', '.pyw', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.cs', '.c', '.cpp', '.h', '.rs', '.go', '.java', '.vue', '.svelte', '.ipynb', '.sh', '.ps1', '.bat'}
WORK = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.psd', '.blend', '.fbx', '.obj', '.mp4', '.mov', '.wav', '.mp3', '.flac', '.md', '.txt', '.pdf', '.docx', '.xlsx', '.pptx', '.srt', '.aep', '.prproj', '.dra', '.xmind', '.canvas', '.sav', '.save', '.rpp', '.drp'}
MODEL = {'.safetensors', '.gguf', '.ckpt', '.pt', '.pth', '.onnx'}
CONFIGURATION = {'.json', '.yaml', '.yml', '.toml', '.ini', '.db', '.sqlite', '.sqlite3', '.env'}
PROTECTED_NAMES = {'.git', '.obsidian', 'obsidian', 'saves', 'savegames', 'saved games', 'diagnostic-models', 'diagnostics', '诊断模型', '存档', '映序', 'yingxu', 'skills', '.codex', '.agents', 'credentials', 'secrets'}
IDENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
_LOCK = threading.RLock()
_PREVIEWS = {}
MAX_LEDGER_BYTES = 4 * 1024 * 1024


def _root(cfg):
    value = cfg.get('ai_root')
    if not value:
        raise ValueError('请先选择工作区；文件预览不会自动扫描磁盘。')
    return Path(config.validate_asset_root(value))


def _path(root, raw):
    if not isinstance(raw, str) or not os.path.isabs(raw) or len(raw) > 2000:
        raise ValueError('请选择当前工作区内的绝对路径。')
    if '..' in raw.replace('\\', '/').split('/') or not config._within(raw, root):
        raise ValueError('路径超出当前工作区。')
    value = Path(os.path.abspath(raw))
    config._check_ancestors(str(value.parent))
    info = value.lstat()
    if config._is_reparse(info) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError('不支持联接、符号链接或特殊文件。')
    return value


def _identity(path):
    info = path.lstat()
    return [str(info.st_dev), str(info.st_ino), info.st_size, str(info.st_mtime_ns), info.st_mode]


def _store_path():
    path = Path(config.DATA_DIR) / 'file-lifecycle.json'
    config._check_ancestors(str(path.parent))
    if path.is_symlink() or (path.exists() and config._is_reparse(path.lstat())):
        raise ValueError('登记存储不能是链接。')
    return path


def _read():
    path = _store_path()
    if path.exists() and path.stat().st_size > MAX_LEDGER_BYTES:
        raise ValueError('登记文件超过安全读取上限。')
    raw = path.read_bytes() if path.exists() else b''
    if len(raw) > MAX_LEDGER_BYTES:
        raise ValueError('登记文件超过安全读取上限。')
    try:
        value = json.loads(raw) if raw else {'schema': 1, 'records': []}
        if value.get('schema') != 1 or not isinstance(value.get('records'), list) or len(value['records']) > 2000:
            raise ValueError()
        for record in value['records']:
            required = {'id', 'kind', 'name', 'path', 'purpose', 'state', 'protected', 'workspace', 'identity', 'expires_at', 'project_id', 'task_id', 'tool_id'}
            if not isinstance(record, dict) or set(record) != required or record['kind'] not in KINDS or record['state'] not in STATES or record['purpose'] not in PURPOSES:
                raise ValueError()
            if any(not isinstance(record[k], str) for k in required - {'protected', 'identity'}) or type(record['protected']) is not bool:
                raise ValueError()
            if not isinstance(record['identity'], list) or len(record['identity']) != 5:
                raise ValueError()
    except (ValueError, AttributeError) as exc:
        raise ValueError('登记文件不可读，已保留原文件。') from exc
    return value, hashlib.sha256(raw).hexdigest(), raw


@contextlib.contextmanager
def _write_lock():
    """An exclusive lock also prevents two separate app processes losing updates."""
    parent = _store_path().parent
    parent.mkdir(parents=True, exist_ok=True)
    lock = parent / 'file-lifecycle.lock'
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError('另一个进程正在保存登记，或上次保存锁尚未解除；请保留文件并核查后重试。') from exc
    identity = os.fstat(descriptor)
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            current = lock.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                lock.unlink()
        except FileNotFoundError:
            pass


def registrations(cfg):
    root = _root(cfg)
    with _LOCK:
        value, revision, _ = _read()
    return {'root': str(root), 'revision': revision,
            'records': [r for r in value['records'] if r.get('workspace') == config._key(root)],
            'review_context': str(cfg.get('file_lifecycle_review_context') or '')[:400],
            'preview_only': True, 'automatic_cleanup': False}


def _text(value, label, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError(label + '须为有效文本。')
    return value.strip()


def _date(value):
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(dt.timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('到期时间须为带时区的 ISO 时间。') from exc


def registration_preview(cfg, payload):
    root = _root(cfg)
    fields = {'id', 'kind', 'name', 'path', 'purpose', 'state', 'project_id', 'task_id', 'tool_id', 'expires_at', 'protected'}
    if not isinstance(payload, dict) or set(payload) - fields:
        raise ValueError('登记字段不受支持。')
    ident = payload.get('id')
    if not isinstance(ident, str) or not IDENT.fullmatch(ident):
        raise ValueError('登记 ID 须为字母、数字、下划线或短横线。')
    kind, state, purpose = payload.get('kind'), payload.get('state'), payload.get('purpose')
    if kind not in KINDS or state not in STATES or purpose not in PURPOSES:
        raise ValueError('请选择有效的登记类型、用途与生命周期。')
    path = _path(root, payload.get('path'))
    if kind != 'artifact' and not path.is_dir():
        raise ValueError('项目、任务和工具须登记目录。')
    protected = payload.get('protected', False)
    if type(protected) is not bool:
        raise ValueError('保护标记须为布尔值。')
    record = {'id': ident, 'kind': kind, 'name': _text(payload.get('name'), '名称'),
              'path': str(path), 'purpose': purpose, 'state': state, 'protected': protected,
              'workspace': config._key(root), 'identity': _identity(path)}
    expires = payload.get('expires_at') or ''
    record['expires_at'] = _date(expires).isoformat() if expires else ''
    with _LOCK:
        value, revision, _ = _read()
        records = [r for r in value['records'] if r.get('workspace') == config._key(root)]
        for field, expected in [('project_id', 'project'), ('task_id', 'task'), ('tool_id', 'tool')]:
            linked = payload.get(field) or ''
            if linked and not any(r.get('id') == linked and r.get('kind') == expected for r in records):
                raise ValueError('引用的项目、任务或工具尚未登记。')
            record[field] = linked
        previous = next((r for r in records if r.get('id') == ident), None)
        if previous and previous.get('kind') != kind:
            raise ValueError('已有登记 ID 不能改变类型。')
        now = time.monotonic()
        for key in list(_PREVIEWS):
            if _PREVIEWS[key]['until'] < now:
                _PREVIEWS.pop(key, None)
        if len(_PREVIEWS) >= 128:
            raise ValueError('预览数量达到上限，请稍后重试。')
        token = secrets.token_urlsafe(24)
        _PREVIEWS[token] = {'root': config._key(root), 'revision': revision, 'record': record, 'until': now + 600}
    return {'token': token, 'before': previous, 'after': record, 'files_modified': 0,
            'message': '确认只保存登记元数据，不改变文件位置、内容或清理策略。'}


def registration_save(cfg, token):
    root = _root(cfg)
    with _LOCK, _write_lock():
        preview = _PREVIEWS.get(token) if isinstance(token, str) else None
        if not preview or preview['until'] < time.monotonic() or preview['root'] != config._key(root):
            raise ValueError('登记预览过期或工作区已切换，请重新预览。')
        value, revision, raw = _read()
        record = preview['record']
        if revision != preview['revision'] or _identity(_path(root, record['path'])) != record['identity']:
            raise ValueError('登记或文件身份已变化，请重新预览。')
        value['records'] = [r for r in value['records'] if not (r.get('workspace') == config._key(root) and r.get('id') == record['id'])] + [record]
        serialized = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
        if len(serialized) > MAX_LEDGER_BYTES or len(value['records']) > 2000:
            raise ValueError('登记容量超过上限，已保留原台账。')
        path = _store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw:
            backup = path.parent / 'file-lifecycle-backups'
            config._check_ancestors(str(backup))
            backup.mkdir(exist_ok=True)
            with (backup / (secrets.token_hex(12) + '.json')).open('xb') as stream:
                stream.write(raw)
        # All registration state is new JSON metadata, never an existing SQLite DB.
        descriptor, temporary = tempfile.mkstemp(prefix='lifecycle-', suffix='.tmp', dir=str(path.parent))
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        _PREVIEWS.pop(token, None)
    return {'ok': True, 'record': record, 'files_modified': 0}


def _category(path):
    suffix = path.suffix.casefold()
    if suffix in SOURCE:
        return 'source'
    if suffix in WORK:
        return 'work'
    if suffix in MODEL:
        return 'model'
    if suffix in CONFIGURATION or path.name.casefold() in {'agents.md', 'skill.md', 'package-lock.json'}:
        return 'configuration'
    if suffix in {'.zip', '.7z', '.rar', '.tar', '.gz'}:
        return 'archive'
    if suffix in {'.tmp', '.cache', '.log', '.pyc'}:
        return 'temporary_hint'
    return 'unknown'


def _allocated(path, info):
    if os.name != 'nt':
        return info.st_blocks * 512 if hasattr(info, 'st_blocks') else None
    try:
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        class StandardInfo(ctypes.Structure):
            _fields_ = [('allocation', ctypes.c_longlong), ('end', ctypes.c_longlong),
                        ('links', wintypes.DWORD), ('delete_pending', ctypes.c_ubyte), ('directory', ctypes.c_ubyte)]
        class CompressionInfo(ctypes.Structure):
            _fields_ = [('compressed_size', ctypes.c_longlong), ('format', wintypes.WORD),
                        ('unit_shift', ctypes.c_ubyte), ('chunk_shift', ctypes.c_ubyte),
                        ('cluster_shift', ctypes.c_ubyte), ('reserved', ctypes.c_ubyte * 3)]
        api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        api.CreateFileW.restype = wintypes.HANDLE
        api.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.GetFileInformationByHandleEx.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        # GetCompressedFileSizeW returns file length for ordinary files, not
        # allocated clusters. Query the allocation by handle instead (resident
        # NTFS files can correctly report zero independently reclaimable bytes).
        handle = api.CreateFileW(str(path), 0x80, 7, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            return None
        try:
            compressed = bool(getattr(info, 'st_file_attributes', 0) & (0x800 | 0x200))
            result = CompressionInfo() if compressed else StandardInfo()
            if not api.GetFileInformationByHandleEx(handle, 8 if compressed else 1, ctypes.byref(result), ctypes.sizeof(result)):
                return None
            return max(0, result.compressed_size if compressed else result.allocation)
        finally:
            api.CloseHandle(handle)
    except (OSError, AttributeError):
        return None


def _git_state(root):
    """No fetch, hooks, fsmonitor, optional index refresh, or private config output."""
    def run(*args):
        return subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-C', str(root), *args],
                              capture_output=True, text=True, timeout=3,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    result = {'path': str(root), 'status': 'unverified', 'dirty': None, 'ahead': None}
    try:
        dirty = run('status', '--porcelain=v1', '--untracked-files=normal')
        ahead = run('rev-list', '--count', '@{upstream}..HEAD')
        if dirty.returncode == 0:
            result['dirty'] = bool(dirty.stdout.strip())
            result['status'] = 'local_checked'
        if ahead.returncode == 0 and ahead.stdout.strip().isdigit():
            result['ahead'] = int(ahead.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    result['remote_fetched'] = False
    return result


def _active_paths(root):
    """Consult the existing collaboration ledger without bootstrap or migration."""
    path = Path(config.DATA_DIR) / 'collaboration.sqlite3'
    if not path.exists():
        return [], True
    con = None
    try:
        config._check_ancestors(str(path.parent))
        if config._is_reparse(path.lstat()):
            return [], False
        con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
        rows = con.execute("SELECT paths FROM tasks WHERE root=? AND status IN ('active','queued','preparing') LIMIT 1001", (config._key(root),)).fetchall()
        if len(rows) > 1000:
            return [], False
        paths = []
        for (raw,) in rows:
            values = json.loads(raw)
            paths.extend(p for p in values.values() if isinstance(p, str) and os.path.isabs(p))
        return paths, True
    except (OSError, ValueError, sqlite3.Error, AttributeError):
        return [], False
    finally:
        if con:
            con.close()


def inventory(cfg, requested=None, max_entries=5000):
    root = _root(cfg)
    scope = _path(root, requested or str(root))
    if not scope.is_dir():
        raise ValueError('递归预览须选择目录。')
    if type(max_entries) is not int or not 1 <= max_entries <= 10000:
        raise ValueError('扫描上限须为 1 至 10000。')
    records = registrations(cfg)['records']
    by_id = {r['id']: r for r in records}
    stale = set()
    for record in records:
        try:
            current_identity = _identity(_path(root, record['path']))
            old = record['identity']
            same = current_identity[:2] == old[:2] and current_identity[4] == old[4]
            if stat.S_ISREG(current_identity[4]):
                same = same and current_identity == old
            if not same:
                stale.add(record['id'])
        except (OSError, ValueError, KeyError, TypeError):
            stale.add(record['id'])
    active_paths, activity_verified = _active_paths(root)
    now = dt.datetime.now(dt.timezone.utc)
    items, issues, repositories = [], [], {}
    stack = [scope]
    examined, allocated_seen, allocated_total, unknown_allocation = 0, set(), 0, 0
    truncated = False
    deadline = time.monotonic() + 15
    # Protect ancestor repositories even when the selected scope is a nested cache.
    for ancestor in [scope] + list(scope.parents):
        if (ancestor / '.git').exists():
            repositories[str(ancestor)] = _git_state(ancestor)
    while stack:
        current = stack.pop()
        if time.monotonic() > deadline:
            truncated = True
            break
        try:
            config._check_ancestors(str(current))
            if (current / '.git').exists() and str(current) not in repositories:
                repositories[str(current)] = _git_state(current)
            # Scandir is consumed with a global entry budget, not an unbounded list.
            with os.scandir(current) as entries:
                for entry in entries:
                    if examined >= max_entries or time.monotonic() > deadline:
                        truncated = True
                        break
                    examined += 1
                    path = Path(entry.path)
                    # Windows DirEntry.stat may report st_ino/st_dev/st_nlink as
                    # zero; lstat obtains the real file identity and link count.
                    info = path.lstat()
                    if config._is_reparse(info):
                        issues.append({'path': str(path), 'reason': 'reparse_point_skipped'})
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if path.name.casefold() in PROTECTED_NAMES:
                            issues.append({'path': str(path), 'reason': 'protected_directory_skipped'})
                        else:
                            stack.append(path)
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        issues.append({'path': str(path), 'reason': 'special_file_skipped'})
                        continue
                    owners = [r for r in records if config._within(path, r['path'])]
                    owners.sort(key=lambda r: len(r['path']), reverse=True)
                    declared = next((r for r in owners if r['kind'] == 'artifact'), None)
                    linked = [by_id[field] for field in (declared.get('project_id'), declared.get('task_id'), declared.get('tool_id')) if field in by_id] if declared else []
                    guards = owners + linked
                    category, reasons = _category(path), []
                    if not activity_verified:
                        reasons.append('activity_ledger_unavailable')
                    if any(config._within(path, p) for p in active_paths):
                        reasons.append('active_collaboration_task')
                    if any(r['id'] in stale for r in guards):
                        reasons.append('registration_identity_changed')
                    if category in {'source', 'work', 'model', 'configuration', 'archive'}:
                        reasons.append(category + '_protected')
                    if any(p.casefold() in PROTECTED_NAMES for p in path.parts):
                        reasons.append('protected_path')
                    repo = next((v for p, v in repositories.items() if config._within(path, p)), None)
                    if repo:
                        reasons.append('repository_source_protected')
                        if repo['dirty'] is not False:
                            reasons.append('uncommitted_or_unverified_repository')
                        if repo['ahead'] is None or repo['ahead'] > 0:
                            reasons.append('unpushed_or_unverified_repository')
                    if any(r['protected'] or r['purpose'] in {'source', 'work', 'input', 'output', 'delivery', 'configuration', 'model', 'archive'} for r in guards):
                        reasons.append('registered_protection')
                    if any(r['state'] == 'active' for r in guards):
                        reasons.append('active_task_or_owner')
                    if info.st_nlink != 1:
                        reasons.append('shared_hardlink')
                    size = _allocated(path, info)
                    identity = (info.st_dev, info.st_ino)
                    if size is None:
                        unknown_allocation += 1
                    elif identity not in allocated_seen:
                        allocated_seen.add(identity)
                        allocated_total += size
                    complete = bool(declared and all(declared.get(k) for k in ('project_id', 'task_id', 'tool_id')))
                    eligible = bool(complete and declared['purpose'] in {'cache', 'temporary'} and declared['state'] == 'expired'
                                    and declared['expires_at'] and _date(declared['expires_at']) <= now and not reasons)
                    if not reasons and not eligible:
                        reasons.append('unknown_purpose_or_incomplete_lifecycle')
                    items.append({'path': str(path), 'relative_path': str(path.relative_to(scope)), 'category': category,
                                  'logical_bytes': info.st_size, 'allocated_bytes': size, 'link_count': info.st_nlink,
                                  'identity': _identity(path), 'modified_at': dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc).isoformat(),
                                  'age_days': max(0, int((now.timestamp() - info.st_mtime) // 86400)),
                                  'registration_id': declared['id'] if declared else '',
                                  'project_id': declared['project_id'] if declared else '',
                                  'task_id': declared['task_id'] if declared else '',
                                  'tool_id': declared['tool_id'] if declared else '',
                                  'purpose': declared['purpose'] if declared else 'unknown',
                                  'lifecycle': declared['state'] if declared else 'unregistered',
                                  'decision': 'candidate' if eligible else 'protected' if any(r != 'unknown_purpose_or_incomplete_lifecycle' for r in reasons) else 'skip',
                                  'reasons': reasons, 'estimated_releasable_bytes': size if eligible else 0})
        except (OSError, ValueError):
            issues.append({'path': str(current), 'reason': 'unavailable_or_changed'})
        if truncated:
            break
    candidates = [r for r in items if r['decision'] == 'candidate']
    return {'root': str(root), 'scope': str(scope), 'captured_at': now.isoformat(), 'items': items, 'issues': issues,
            'repositories': list(repositories.values()), 'truncated': truncated, 'examined_entries': examined,
            'summary': {'files': len(items), 'logical_bytes': sum(r['logical_bytes'] for r in items),
                        'allocated_bytes_known': allocated_total, 'allocation_unknown_files': unknown_allocation,
                        'candidate_files': len(candidates), 'candidate_logical_bytes': sum(r['logical_bytes'] for r in candidates),
                        'estimated_releasable_bytes': None if any(r['allocated_bytes'] is None for r in candidates) else sum(r['allocated_bytes'] for r in candidates),
                        'actual_released_bytes': 0, 'volume_free_space_change_bytes': None},
            'preview_only': True, 'automatic_cleanup': False,
            'message': '只读时间快照；候选不等于已删除。实际释放为 0；未测量卷可用空间变化。'}
