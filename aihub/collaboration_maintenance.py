"""Bounded read-only report inventory and opt-in managed-temp recycling.

Source inventories never grant write/delete authority. Only collaboration's own
registered temporary artifacts can enter the separately audited recycler.
"""
import contextlib
import collections
import copy
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
import uuid

from . import config, recycle, service_control

INTERVAL = 3600
MAX_FILES = 20000
MAX_ITEMS = 2000
MAX_DIRS = 2000
SCAN_SECONDS = 10
TEXT_EXTENSIONS = {'.md', '.txt', '.rst', '.html', '.htm', '.pdf', '.docx'}
_SCAN_LOCK = threading.RLock()
_SCHEMA_LOCK = threading.RLock()
_SCAN_ENUMERATORS = {}
_SCAN_ENUMERATOR_LIMIT = 16
_SCAN_ENUMERATOR_TTL = 300
RECONFIRM_SECONDS = 600
_SOURCE_PREVIEWS = {}
REPORT_EXCLUDED = {'datasets', 'dataset', 'captions', 'caption', 'build', 'dist', 'releases', 'packages'}
WORK_BRANCHES = {'reports', 'report', 'outputs', 'output', 'deliverables', 'delivery', 'aihub', '报告', '交付'}
EXCLUDED = {'.git', '.codex', '.zcode', '.workbuddy', '.codebuddy', '.dsh',
            'node_modules', 'vendor', 'runtime', 'site-packages', 'venv', '.venv',
            '__pycache__', 'backups', 'sessions', 'credentials', 'secrets',
            'logs', 'cache', 'cookies', 'local storage', 'session storage'}


def _core():
    from . import collaboration
    return collaboration


def _root(cfg):
    return config._key(str(_core().root_path(cfg)))


@contextlib.contextmanager
def _db():
    path = Path(config.DATA_DIR) / 'collaboration-maintenance.sqlite3'
    config._check_ancestors(str(path.parent))
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ('', '-wal', '-shm', '-journal'):
        candidate = Path(str(path) + suffix)
        if os.path.lexists(candidate):
            info = candidate.lstat()
            if config._is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('维护数据库不能是链接或特殊文件。')
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with _SCHEMA_LOCK:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript('''
              CREATE TABLE IF NOT EXISTS sources(
                id TEXT PRIMARY KEY, root TEXT NOT NULL, path TEXT NOT NULL,
                label TEXT NOT NULL, tool TEXT NOT NULL, created_at REAL NOT NULL,
                scanned_at REAL, file_count INTEGER DEFAULT 0, bytes INTEGER DEFAULT 0,
                truncated INTEGER DEFAULT 0, identity TEXT, revision INTEGER NOT NULL DEFAULT 0,
                deleted_at TEXT, deleted_by TEXT, record_revision INTEGER NOT NULL DEFAULT 0, UNIQUE(root,path));
              CREATE TABLE IF NOT EXISTS source_record_audit(
                id INTEGER PRIMARY KEY, root TEXT NOT NULL, source_id TEXT NOT NULL,
                action TEXT NOT NULL, created_at TEXT NOT NULL, actor TEXT NOT NULL, detail TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS inventory(
                source_id TEXT NOT NULL, path TEXT NOT NULL, title TEXT, size INTEGER,
                mtime REAL, category TEXT, historical INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(source_id,path));
              CREATE TABLE IF NOT EXISTS source_scan_progress(
                source_id TEXT PRIMARY KEY, root TEXT NOT NULL, state TEXT NOT NULL, lease_until REAL NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS source_scan_seen(
                source_id TEXT NOT NULL, generation TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(source_id,generation,path));
              CREATE TABLE IF NOT EXISTS policies(
                root TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                days INTEGER NOT NULL DEFAULT 7, next_run REAL DEFAULT 0,
                lease_until REAL DEFAULT 0, last_run TEXT DEFAULT '{}');
            ''')
            columns = {row[1] for row in conn.execute('PRAGMA table_info(sources)')}
            inventory_columns = {row[1] for row in conn.execute('PRAGMA table_info(inventory)')}
            if not {'identity', 'revision', 'deleted_at', 'deleted_by', 'record_revision'} <= columns or 'historical' not in inventory_columns:
                backup_dir = path.parent / 'maintenance-schema-backups'
                config._check_ancestors(str(backup_dir))
                backup_dir.mkdir(exist_ok=True)
                backup = sqlite3.connect(str(backup_dir / ('sources-' + uuid.uuid4().hex + '.sqlite3')))
                try:
                    conn.backup(backup)
                finally:
                    backup.close()
            if 'identity' not in columns:
                conn.execute("ALTER TABLE sources ADD COLUMN identity TEXT")
            if 'revision' not in columns:
                conn.execute('ALTER TABLE sources ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
            for column, kind in [('deleted_at', 'TEXT'), ('deleted_by', 'TEXT'), ('record_revision', 'INTEGER NOT NULL DEFAULT 0')]:
                if column not in columns:
                    conn.execute('ALTER TABLE sources ADD COLUMN ' + column + ' ' + kind)
            if 'historical' not in inventory_columns:
                conn.execute('ALTER TABLE inventory ADD COLUMN historical INTEGER NOT NULL DEFAULT 0')
            # Existing successful/partial scans already hold the accepted identity.
            legacy = conn.execute('SELECT s.id,p.state FROM sources s JOIN source_scan_progress p ON p.source_id=s.id '
                                  'WHERE s.identity IS NULL').fetchall()
            for source in legacy:
                accepted = json.loads(source['state']).get('identity')
                if accepted:
                    conn.execute('UPDATE sources SET identity=? WHERE id=?', (json.dumps(accepted), source['id']))
            conn.commit()
        yield conn
    finally:
        conn.close()


def policy(cfg):
    root = _root(cfg)
    with _db() as conn:
        row = conn.execute('SELECT * FROM policies WHERE root=?', (root,)).fetchone()
    value = dict(row) if row else {'enabled': 1, 'days': 7, 'next_run': 0, 'last_run': '{}'}
    return {'enabled': bool(value['enabled']), 'days': value['days'],
            'interval_seconds': INTERVAL, 'next_run_at': value['next_run'],
            'last_run': json.loads(value['last_run']),
            'scope': 'registered_temp_completed_tasks_only',
            'days_apply_to': 'new_artifacts', 'service_required': True}


def save_policy(cfg, body):
    root = _root(cfg)
    enabled, days = body.get('enabled'), body.get('days')
    if type(enabled) is not bool or type(days) is not int or not 1 <= days <= 365:
        raise ValueError('自动回收开关须为布尔值，保留天数须为 1–365 的整数。')
    with _db() as conn, conn:
        conn.execute('INSERT INTO policies(root,enabled,days) VALUES(?,?,?) '
                     'ON CONFLICT(root) DO UPDATE SET enabled=excluded.enabled,days=excluded.days,next_run=0',
                     (root, int(enabled), days))
    return policy(cfg)


def list_sources(cfg, include_deleted=False):
    if type(include_deleted) is not bool:
        raise ValueError('include_deleted 必须是布尔值。')
    root = _root(cfg)
    with _db() as conn:
        rows = conn.execute('SELECT * FROM sources WHERE root=?' + ('' if include_deleted else ' AND deleted_at IS NULL') + ' ORDER BY created_at,id', (root,)).fetchall()
    return {'items': [{**{k: v for k, v in dict(r).items() if k != 'root'},
                       'identity': json.loads(r['identity']) if r['identity'] else None} for r in rows]}


def source_candidates(cfg):
    root = Path(_core().root_path(cfg))
    proposed = [(root / '00_Management/Reports', 'AI 工作区管理报告', 'any'),
                (root / '40_Projects', '项目工作产物', 'any'),
                (root / '50_Training/Projects', '训练项目报告', 'any'),
                (root / '80_Knowledge', '既有知识文档（待甄别）', 'any'),
                (Path.home() / 'Documents/Codex', 'Codex 本地任务目录', 'codex'),
                (Path.home() / '.zcode/workspace/default/_report', 'ZCode 既有报告', 'zcode')]
    items = []
    registered = {config._key(source['path']): source for source in list_sources(cfg, include_deleted=True)['items']}
    for path, label, tool in proposed:
        try:
            existing = registered.get(config._key(path))
            if existing and existing.get('deleted_at'):
                continue
            validated = config.validate_asset_root(str(path))
            items.append({'path': validated, 'label': label, 'tool': tool, 'registered': bool(existing)})
        except (OSError, ValueError):
            continue
    return {'items': items, 'note': '候选来源仅经文件元数据探测；添加后手动盘点，不读取工具原生会话或凭据。'}


def _source_path(value):
    path = config.validate_asset_root(value)
    # This specific legacy report leaf is application output, not native state.
    legacy_report = Path.home() / '.zcode/workspace/default/_report'
    legacy_allowed = config._key(path) == config._key(legacy_report)
    parts = list(Path(path).parts)
    if legacy_allowed:
        parts = [p for p in parts if p.casefold() != '.zcode']
    if any(p.casefold().startswith('.') or p.casefold() in EXCLUDED for p in parts):
        raise ValueError('请选择项目报告目录，不接入工具内部状态、隐藏目录或凭据目录。')
    return path


def add_source(cfg, body):
    root = _root(cfg)
    path = _source_path(body.get('path'))
    label, tool = body.get('label') or Path(path).name, body.get('tool', 'any')
    if not isinstance(label, str) or not 1 <= len(label) <= 120 or any(ord(c) < 32 for c in label):
        raise ValueError('来源名称须为 1–120 个可显示字符。')
    _core()._tool(tool, cfg=cfg)
    with _db() as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        old = next((row for row in conn.execute('SELECT * FROM sources WHERE root=?', (root,))
                    if config._key(row['path']) == config._key(path)), None)
        if old:
            if old['deleted_at']:
                raise ValueError('此来源已被移除，请从已移除来源中恢复，不能重新扫描或收录。')
            return {**dict(old), 'identity': json.loads(old['identity']) if old['identity'] else None}
        if conn.execute('SELECT count(*) FROM sources WHERE root=? AND deleted_at IS NULL', (root,)).fetchone()[0] >= 50:
            raise ValueError('每个工作区最多登记 50 个报告来源。')
        row = {'id': uuid.uuid4().hex, 'path': path, 'label': label, 'tool': tool, 'created_at': time.time()}
        row.update(identity=_source_identity(path), revision=0)
        conn.execute('INSERT INTO sources(id,root,path,label,tool,created_at,identity) VALUES(?,?,?,?,?,?,?)',
                     (row['id'], root, path, label, tool, row['created_at'], json.dumps(row['identity'])))
    return row


def _source_identity(path):
    info = os.stat(_source_path(path))
    return [str(info.st_dev), str(info.st_ino)]


def source_identity_matches(source):
    accepted = source.get('identity')
    if isinstance(accepted, str):
        accepted = json.loads(accepted)
    # A legacy, never-scanned source has no documents to authorize.
    return not accepted or accepted == _source_identity(source['path'])


def source_reconfirm_preview(cfg, body):
    root, now = _root(cfg), time.time()
    with _SCAN_LOCK, _db() as conn:
        source = conn.execute('SELECT * FROM sources WHERE root=? AND id=? AND deleted_at IS NULL', (root, body.get('source_id'))).fetchone()
        if not source:
            raise ValueError('来源不存在或不属于当前工作区。')
        source = dict(source)
        current = _source_identity(source['path'])
        lease = conn.execute('SELECT lease_until,state FROM source_scan_progress WHERE root=? AND source_id=?', (root, source['id'])).fetchone()
        count = conn.execute('SELECT count(*) FROM inventory WHERE source_id=?', (source['id'],)).fetchone()[0]
        previous = json.loads(source['identity']) if source['identity'] else None
        can_apply = not lease or lease['lease_until'] <= now
        result = {'token': None, 'source_id': source['id'], 'path': source['path'],
                  'previous_identity': previous, 'current_identity': current,
                  'retained_inventory_count': count, 'expires_at': now + RECONFIRM_SECONDS,
                  'can_apply': can_apply, 'history_retained': True}
        for token in list(_SOURCE_PREVIEWS):
            if _SOURCE_PREVIEWS[token]['expires_at'] <= now:
                del _SOURCE_PREVIEWS[token]
        if can_apply:
            if len(_SOURCE_PREVIEWS) >= 100:
                raise ValueError('待确认来源预览过多，请稍后再试。')
            result['token'] = uuid.uuid4().hex
            _SOURCE_PREVIEWS[result['token']] = {**result, 'root': root, 'source': source,
                'database': config._key(config.DATA_DIR), 'scan_state': lease['state'] if lease else None}
        return result


def source_reconfirm_apply(cfg, body):
    root, token = _root(cfg), body.get('token')
    with _SCAN_LOCK:
        pending = _SOURCE_PREVIEWS.get(token) if isinstance(token, str) else None
        if not pending or pending['expires_at'] <= time.time():
            raise ValueError('来源预览不存在或已过期，请重新预览。')
        if pending['root'] != root or pending['database'] != config._key(config.DATA_DIR):
            raise ValueError('工作环境已切换，请重新预览来源。')
        with _db() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            source = conn.execute('SELECT * FROM sources WHERE root=? AND id=? AND deleted_at IS NULL', (root, pending['source_id'])).fetchone()
            lease = conn.execute('SELECT lease_until,state FROM source_scan_progress WHERE root=? AND source_id=?', (root, pending['source_id'])).fetchone()
            if not source or dict(source) != pending['source'] or (lease['state'] if lease else None) != pending['scan_state']:
                raise ValueError('来源或扫描进度已改变，请重新预览。')
            if lease and lease['lease_until'] > time.time():
                raise ValueError('该来源正在盘点，请等待当前批次结束。')
            if _source_identity(source['path']) != pending['current_identity']:
                raise ValueError('来源目录身份再次改变，请重新预览。')
            ident = source['id']
            conn.execute('DELETE FROM source_scan_progress WHERE source_id=?', (ident,))
            conn.execute('DELETE FROM source_scan_seen WHERE source_id=?', (ident,))
            conn.execute('UPDATE inventory SET historical=1 WHERE source_id=?', (ident,))
            conn.execute('UPDATE sources SET identity=?,revision=revision+1,scanned_at=NULL,truncated=0 WHERE id=? AND root=?',
                         (json.dumps(pending['current_identity']), ident, root))
        for key in list(_SCAN_ENUMERATORS):
            if key[:2] == (root, ident):
                _close_scan_enumerator(key)
        del _SOURCE_PREVIEWS[token]
        return {'applied': True, 'source_id': ident, 'path': source['path'], 'history_retained': True,
                'retained_inventory_count': pending['retained_inventory_count'], 'scanned_at': None}


def category(path, source_root):
    """Classify within the selected source, never by its machine-specific parents.

    This is an inventory hint only and grants no retention/recycling authority.
    """
    value = Path(path).relative_to(Path(source_root)).as_posix().casefold()
    if any(p in {'temp', 'tmp', 'scratch', 'drafts', '草稿', '临时'} for p in value.split('/')):
        return 'temp_candidate'
    if any(k in value for k in ('memory', 'knowledge', 'sop', '记忆', '知识', '规范')):
        return 'knowledge'
    if any(k in value for k in ('report', 'validation', 'review', '报告', '验收', '总结', '复盘')):
        return 'report'
    return 'other_text'


def _safe_name(name):
    folded = name.casefold()
    return (not folded.startswith('.') and not any(token in folded for token in
            ('credential', 'secret', 'token', 'password', 'cookie', 'auth.json', '凭据', '密码')))


def inventory_path_allowed(path, base):
    """Report-only role filter, relative to the explicitly authorized source."""
    try:
        relative = Path(path).relative_to(Path(base))
    except ValueError:
        return False
    parts = [part.casefold() for part in relative.parts]
    if not parts or any(not _safe_name(part) for part in relative.parts):
        return False
    if any(part in EXCLUDED or part in REPORT_EXCLUDED for part in parts[:-1]):
        return False
    if any('.pre-update-' in part or '.pre-migration-' in part for part in parts[:-1]):
        return False
    # A source explicitly rooted inside Work can opt into its real reports.
    if 'work' in parts[:-1]:
        index = parts.index('work')
        tail = parts[index + 1:-1]
        if tail and tail[0] not in WORK_BRANCHES:
            return False
    return Path(path).suffix.casefold() in TEXT_EXTENSIONS


def _descend_report_folder(path, base):
    relative = Path(path).relative_to(Path(base))
    parts = [part.casefold() for part in relative.parts]
    if any(not _safe_name(part) or part in EXCLUDED or part in REPORT_EXCLUDED for part in parts):
        return False
    if any('.pre-update-' in part or '.pre-migration-' in part for part in parts):
        return False
    if 'work' in parts:
        index = parts.index('work')
        tail = parts[index + 1:]
        if tail and tail[0] not in WORK_BRANCHES:
            return False
    return not config._within(path, config.APP_DIR) and not config._within(path, config.DATA_DIR)


def scan_source(cfg, body):
    # In-process serialization plus persisted lease protect the same cursor across services.
    with _SCAN_LOCK:
        return _scan_source(cfg, body)


def _directory_signature(path):
    info = os.stat(path, follow_symlinks=False)
    return (info.st_dev, info.st_ino, getattr(info, 'st_mtime_ns', int(info.st_mtime * 1_000_000_000)))


def _close_scan_enumerator(key):
    entry = _SCAN_ENUMERATORS.pop(key, None)
    if entry:
        try:
            entry['stream'].close()
        except OSError:
            pass


def _clear_scan_enumerators():
    for key in list(_SCAN_ENUMERATORS):
        _close_scan_enumerator(key)


def _scan_enumerator(root, source_id, generation, relative, folder, cursor, signature):
    now = time.monotonic()
    for key, entry in list(_SCAN_ENUMERATORS.items()):
        if now - entry['last_used'] > _SCAN_ENUMERATOR_TTL:
            _close_scan_enumerator(key)
        elif key[:2] == (root, source_id) and key[2] != generation:
            _close_scan_enumerator(key)
    key = (root, source_id, generation, relative)
    cached = _SCAN_ENUMERATORS.get(key)
    if cached and cached['cursor'] == cursor and cached['signature'] == signature:
        cached['last_used'] = now
        return cached, cursor
    _close_scan_enumerator(key)
    # A scandir iterator cannot be serialized. After process restart or cache
    # eviction, replay this directory from the beginning; the scan remains
    # partial until a full pass completes, so repeated rows cannot prune history.
    cursor = 0
    stream = os.scandir(folder)
    while len(_SCAN_ENUMERATORS) >= _SCAN_ENUMERATOR_LIMIT:
        oldest = min(_SCAN_ENUMERATORS, key=lambda item: _SCAN_ENUMERATORS[item]['last_used'])
        _close_scan_enumerator(oldest)
    entry = {'stream': stream, 'cursor': cursor, 'signature': signature, 'last_used': now}
    _SCAN_ENUMERATORS[key] = entry
    return entry, cursor


def _scan_source(cfg, body):
    root, ident = _root(cfg), body.get('source_id')
    with _db() as conn:
        source = conn.execute('SELECT * FROM sources WHERE root=? AND id=? AND deleted_at IS NULL', (root, ident)).fetchone()
    if not source:
        raise ValueError('来源不存在或不属于当前工作区。')
    base = _source_path(source['path'])
    root_identity = os.stat(base)
    identity = [str(root_identity.st_dev), str(root_identity.st_ino)]
    if source['identity'] and json.loads(source['identity']) != identity:
        raise ValueError('来源目录身份改变，保留旧索引，请重新确认来源。')
    with _db() as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        saved = conn.execute('SELECT * FROM source_scan_progress WHERE root=? AND source_id=?', (root, ident)).fetchone()
        if saved and saved['lease_until'] > time.time():
            raise ValueError('该来源正在盘点，请等待当前批次结束。')
        state = json.loads(saved['state']) if saved else None
        if state and state.get('identity') != identity:
            raise ValueError('来源目录身份改变，保留旧索引，请重新确认来源。')
        if not source['identity']:
            conn.execute('UPDATE sources SET identity=? WHERE root=? AND id=?', (json.dumps(identity), root, ident))
        resumed = bool(state and state.get('queue'))
        if not resumed:
            state = {'generation': uuid.uuid4().hex, 'identity': identity, 'queue': [['', 0, '']],
                     'errors': [], 'examined': 0, 'directories': 0, 'started_at': time.time()}
            conn.execute('DELETE FROM source_scan_seen WHERE source_id=?', (ident,))
        state['batch_token'] = uuid.uuid4().hex
        lease_state = json.dumps(state)
        conn.execute('INSERT INTO source_scan_progress VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET root=excluded.root,state=excluded.state,lease_until=excluded.lease_until',
                     (ident, root, lease_state, time.time() + max(SCAN_SECONDS * 3, 30)))
    queue, rows = collections.deque(state['queue']), []
    errors = list(state.get('errors', []))
    examined, directories = 0, 0
    started = time.monotonic()
    try:
        while queue and len(rows) < MAX_ITEMS and examined < MAX_FILES and directories < MAX_DIRS and time.monotonic() - started < SCAN_SECONDS:
            relative, depth, saved_cursor = queue.popleft()
            cursor = saved_cursor if type(saved_cursor) is int and saved_cursor >= 0 else 0
            folder = os.path.join(base, relative)
            # Cursor data is untrusted persisted state: cannot grant a new path scope.
            if (not config._within(folder, base) or '..' in relative.replace('\\', '/').split('/')
                    or os.path.isabs(relative)):
                raise ValueError('来源续扫路径不合法，原索引保留。')
            try:
                if relative and not _descend_report_folder(folder, base):
                    raise ValueError('续扫目录不在报告盘点范围内。')
                config._check_ancestors(folder)
                directories += 1
                signature = _directory_signature(folder)
                enumerator, cursor = _scan_enumerator(root, ident, state['generation'], relative,
                                                      folder, cursor, signature)
                continued = False
                while True:
                    if len(rows) >= MAX_ITEMS or examined >= MAX_FILES or time.monotonic() - started >= SCAN_SECONDS:
                        queue.append([relative, depth, enumerator['cursor']])
                        continued = True
                        break
                    try:
                        entry = next(enumerator['stream'])
                    except StopIteration:
                        _close_scan_enumerator((root, ident, state['generation'], relative))
                        enumerator = None
                        break
                    enumerator['cursor'] += 1
                    enumerator['last_used'] = time.monotonic()
                    examined += 1
                    if not _safe_name(entry.name):
                        continue
                    try:
                        info = os.lstat(entry.path)
                        if config._is_reparse(info):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if _descend_report_folder(entry.path, base):
                                if depth < 12:
                                    child = os.path.relpath(entry.path, base)
                                    if not any(item[0] == child for item in queue):
                                        queue.append([child, depth + 1, 0])
                                elif len(errors) < 20:
                                    errors.append('达到目录深度上限：' + entry.path)
                        elif (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                              and inventory_path_allowed(entry.path, base)):
                            rows.append({'source_id': ident, 'path': entry.path, 'title': entry.name,
                                         'size': info.st_size, 'mtime': info.st_mtime, 'category': category(entry.path, base)})
                    except OSError as error:
                        if len(errors) < 20:
                            errors.append(str(error)[:300])
                if continued:
                    # Keep at most this one open iterator across requests. This is
                    # the actual continuation point for unsorted directory entries.
                    break
                after_signature = _directory_signature(folder)
                if after_signature != signature:
                    _close_scan_enumerator((root, ident, state['generation'], relative))
                    if not any(item[0] == relative for item in queue):
                        queue.append([relative, depth, 0])
                    break
            except (OSError, ValueError) as error:
                _close_scan_enumerator((root, ident, state['generation'], relative))
                if len(errors) < 20:
                    errors.append(str(error)[:300])
        config._check_ancestors(base)
        current = os.stat(base)
        if identity != [str(current.st_dev), str(current.st_ino)]:
            raise ValueError('来源目录在盘点期间发生变化，原索引保留。')
        scanned_at = time.time()
        state.update(queue=list(queue), errors=errors, examined=state['examined'] + examined,
                     directories=state['directories'] + directories)
        truncated = bool(queue or errors)
        with _db() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            lease = conn.execute('SELECT state FROM source_scan_progress WHERE root=? AND source_id=?', (root, ident)).fetchone()
            if not lease or lease['state'] != lease_state:
                raise ValueError('本批次盘点租约已被接替，原索引保留。')
            # Revalidate ownership before committing metadata to this root.
            current_source = conn.execute('SELECT identity FROM sources WHERE root=? AND id=? AND path=? AND revision=? AND deleted_at IS NULL',
                                          (root, ident, base, source['revision'])).fetchone()
            if not current_source or json.loads(current_source['identity']) != identity:
                raise ValueError('来源配置已改变，未提交本轮盘点。')
            conn.executemany('INSERT OR REPLACE INTO inventory(source_id,path,title,size,mtime,category) VALUES(?,?,?,?,?,?)',
                             [(ident, r['path'], r['title'], r['size'], r['mtime'], r['category']) for r in rows])
            conn.executemany('INSERT OR IGNORE INTO source_scan_seen VALUES(?,?,?)',
                             [(ident, state['generation'], r['path']) for r in rows])
            if not truncated:
                conn.execute('DELETE FROM inventory WHERE source_id=? AND path NOT IN (SELECT path FROM source_scan_seen WHERE source_id=? AND generation=?)',
                             (ident, ident, state['generation']))
            counts = conn.execute('SELECT count(*),coalesce(sum(size),0) FROM inventory WHERE source_id=?', (ident,)).fetchone()
            conn.execute('UPDATE sources SET scanned_at=?,file_count=?,bytes=?,truncated=? WHERE id=? AND root=?',
                         (scanned_at, counts[0], counts[1], int(truncated), ident, root))
            conn.execute('UPDATE source_scan_progress SET state=?,lease_until=0 WHERE root=? AND source_id=?',
                         (json.dumps(state), root, ident))
        categories = collections.Counter(row['category'] for row in rows)
        return {'items': rows, 'scanned_at': scanned_at, 'truncated': truncated, 'errors': errors,
                'summary': {'files': len(rows), 'bytes': sum(r['size'] for r in rows), 'categories': dict(categories)},
                'progress': {'resumed': resumed, 'pending_directories': len(queue), 'examined': state['examined'],
                             'complete': not truncated}, 'read_only': True, 'retention_authority': False}
    finally:
        with _db() as conn, conn:
            conn.execute('UPDATE source_scan_progress SET lease_until=0 WHERE root=? AND source_id=? AND state=?', (root, ident, lease_state))


def inventory(cfg, include_deleted=False):
    if type(include_deleted) is not bool:
        raise ValueError('include_deleted 必须是布尔值。')
    root = _root(cfg)
    with _db() as conn:
        condition = '' if include_deleted else ' AND s.deleted_at IS NULL'
        rows = conn.execute('SELECT i.*,s.label,s.tool,s.deleted_at AS source_deleted_at FROM inventory i JOIN sources s ON s.id=i.source_id '
                            'WHERE s.root=?' + condition + ' ORDER BY i.mtime DESC,i.path LIMIT 500', (root,)).fetchall()
        count = conn.execute('SELECT count(*) FROM inventory i JOIN sources s ON s.id=i.source_id WHERE s.root=?' + condition, (root,)).fetchone()[0]
    return {'items': [dict(row) for row in rows], 'total': count, 'limit': 500}


def record_lifecycle(cfg, action, body):
    """Revoke only report-source authorization; never touch its files/native state."""
    core, root, identifier = _core(), _root(cfg), body['entity_id']
    with _SCAN_LOCK, core._LOCK, _db() as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        raw = conn.execute('SELECT * FROM sources WHERE root=? AND id=?', (root, identifier)).fetchone()
        if not raw:
            raise ValueError('来源不存在或不属于当前工作环境。')
        source = dict(raw)
        lease = conn.execute('SELECT * FROM source_scan_progress WHERE root=? AND source_id=?', (root, identifier)).fetchone()
        if action == 'record_restore':
            if not source['deleted_at']:
                raise ValueError('来源未被移除。')
            if not source_identity_matches(source):
                raise ValueError('来源目录身份已改变，保留移除状态；请先恢复原目录再恢复授权。')
            conn.execute('UPDATE sources SET deleted_at=NULL,deleted_by=NULL,record_revision=record_revision+1 WHERE root=? AND id=?', (root, identifier))
            verb = 'restored'
        else:
            if source['deleted_at']:
                raise ValueError('来源已移除，请先恢复。')
            inventory_rows = [dict(r) for r in conn.execute('SELECT * FROM inventory WHERE source_id=? ORDER BY path', (identifier,))]
            audit = [dict(r) for r in conn.execute('SELECT * FROM source_record_audit WHERE root=? AND source_id=? ORDER BY id', (root, identifier))]
            try:
                current_identity = _source_identity(source['path'])
            except (ValueError, OSError):
                current_identity = None
            snapshot = {'source': source, 'inventory': inventory_rows, 'scan': dict(lease) if lease else None,
                        'audit': audit, 'current_identity': current_identity}
            can_apply = not lease or lease['lease_until'] <= time.time()
            warnings = ['只撤销此来源的扫描授权并隐藏来源索引；原文件、既有索引和工具原生记录保留，可恢复。']
            if not can_apply:
                warnings.append('此来源正在扫描，请等待本次扫描结束再移除。')
            if action == 'record_delete_preview':
                return core._record_preview(cfg, 'source', source, snapshot,
                    {'artifacts': 0, 'memories': 0, 'approved_memories': 0, 'inventory': len(inventory_rows)}, warnings, can_apply)
            if action != 'record_delete_apply':
                raise ValueError('未知记录操作。')
            if not can_apply:
                raise ValueError('不能移除正在扫描的来源。')
            token = core._record_confirm(cfg, body, snapshot)
            conn.execute('UPDATE sources SET deleted_at=?,deleted_by=?,record_revision=record_revision+1,revision=revision+1 WHERE root=? AND id=?', (core._now(), 'ui', root, identifier))
            verb = 'applied'
        conn.execute('INSERT INTO source_record_audit(root,source_id,action,created_at,actor,detail) VALUES(?,?,?,?,?,?)',
                     (root, identifier, action, core._now(), 'ui', json.dumps({'files_preserved': True})))
        record = dict(conn.execute('SELECT * FROM sources WHERE root=? AND id=?', (root, identifier)).fetchone())
        record.pop('root', None)
        record['identity'] = json.loads(record['identity']) if record['identity'] else None
    if action == 'record_delete_apply':
        core._RECORD_PREVIEWS.pop(token, None)
        for key in list(_SCAN_ENUMERATORS):
            if key[:2] == (root, identifier):
                _close_scan_enumerator(key)
    return {verb: True, 'entity_type': 'source', 'entity_id': identifier, 'record': record,
            'files_preserved': True, 'recoverable': True}


def preview(cfg, body=None):
    result = _core().retention_preview(cfg, offset=(body or {}).get('offset', 0))
    if isinstance(result, list):
        result = {'items': result}
    return {**result, 'policy': policy(cfg), 'last_run': policy(cfg)['last_run']}


def run_retention(cfg, scheduled=False, artifact_ids=None):
    if artifact_ids is not None and (scheduled or not isinstance(artifact_ids, list)
            or not 1 <= len(artifact_ids) <= 1000
            or any(not isinstance(item, str) or not item or len(item) > 100 for item in artifact_ids)
            or len(set(artifact_ids)) != len(artifact_ids)):
        raise ValueError('待回收列表无效，请重新预览。')
    root, now = _root(cfg), time.time()
    with _db() as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT OR IGNORE INTO policies(root) VALUES(?)', (root,))
        row = conn.execute('SELECT * FROM policies WHERE root=?', (root,)).fetchone()
        if row['lease_until'] > now or (scheduled and (not row['enabled'] or row['next_run'] > now)):
            return {'items': [], 'checked': 0, 'recycled': 0, 'errors': [], 'skipped': True}
        conn.execute('UPDATE policies SET lease_until=?,next_run=? WHERE root=?', (now + 1800, now + INTERVAL, root))
    results, errors, next_offset = [], [], 0
    try:
        if artifact_ids is not None:
            items = [{'id': ident} for ident in artifact_ids]
        else:
            count = _core().retention_count(cfg, now=now)
            offset = json.loads(row['last_run']).get('next_offset', 0)
            if type(offset) is not int or offset < 0 or offset >= count:
                offset = 0
            initial = _core().retention_candidates(cfg, now=now, offset=offset, limit=100)
            items = initial.get('items', []) if isinstance(initial, dict) else initial
        for item in items:
            if scheduled and not policy(cfg)['enabled']:
                break
            result = _core().recycle_candidate(cfg, item['id'], recycle.recycle_file, now=now)
            results.append(result)
            if result.get('status') == 'error':
                errors.append(result.get('error') or result.get('reason') or '回收失败')
        removed = sum(r.get('status') == 'recycled' for r in results)
        if artifact_ids is None:
            next_offset = offset + len(results) - removed
            if next_offset >= count - removed or len(items) < 100:
                next_offset = 0
        else:
            next_offset = json.loads(row['last_run']).get('next_offset', 0)
    except Exception as error:
        errors.append(str(error)[:500])
    finally:
        result = {'items': results, 'checked': len(results),
                  'recycled': sum(r.get('status') == 'recycled' for r in results),
                  'errors': errors, 'finished_at': time.time(), 'scheduled': scheduled,
                  'next_offset': next_offset}
        with _db() as conn, conn:
            conn.execute('UPDATE policies SET lease_until=0,last_run=? WHERE root=?',
                         (json.dumps(result, ensure_ascii=False), root))
    return {**result, 'policy': policy(cfg)}


def execute(cfg, action, body, actor='ui'):
    if not isinstance(body, dict):
        raise ValueError('请求须为 JSON 对象。')
    read_actions = {'source_list': lambda: list_sources(cfg, body.get('include_deleted', False)), 'source_candidates': lambda: source_candidates(cfg),
                    'source_inventory': lambda: inventory(cfg, body.get('include_deleted', False)), 'retention_preview': lambda: preview(cfg, body)}
    ui_actions = {'source_add': lambda: add_source(cfg, body), 'source_scan': lambda: scan_source(cfg, body),
                  'source_reconfirm_preview': lambda: source_reconfirm_preview(cfg, body),
                  'source_reconfirm_apply': lambda: source_reconfirm_apply(cfg, body),
                  'retention_policy': lambda: save_policy(cfg, body),
                  'retention_run': lambda: run_retention(cfg, artifact_ids=body.get('artifact_ids'))}
    if action in read_actions:
        return read_actions[action]()
    if actor != 'ui':
        raise PermissionError('此操作只能从 AI Hub 管理界面发起。')
    if action not in ui_actions:
        raise ValueError('未知维护操作。')
    return ui_actions[action]()


def start_scheduler(cfg):
    """One low-frequency worker per service; persisted lease prevents duplicate runs."""
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            gate = service_control.GATE
            if not gate.enter():
                break
            try:
                snapshot = copy.deepcopy(cfg)
                if snapshot.get('workspace_managed'):
                    run_retention(snapshot, scheduled=True)
            except (OSError, ValueError, sqlite3.Error):
                pass  # Missing/disconnected workspaces must never trigger fallback roots.
            finally:
                gate.leave()
            stop.wait(60)

    thread = threading.Thread(target=worker, name='aihub-managed-temp-retention', daemon=True)
    thread.start()
    return stop, thread
