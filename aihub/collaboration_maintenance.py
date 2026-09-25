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
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript('''
          CREATE TABLE IF NOT EXISTS sources(
            id TEXT PRIMARY KEY, root TEXT NOT NULL, path TEXT NOT NULL,
            label TEXT NOT NULL, tool TEXT NOT NULL, created_at REAL NOT NULL,
            scanned_at REAL, file_count INTEGER DEFAULT 0, bytes INTEGER DEFAULT 0,
            truncated INTEGER DEFAULT 0, UNIQUE(root,path));
          CREATE TABLE IF NOT EXISTS inventory(
            source_id TEXT NOT NULL, path TEXT NOT NULL, title TEXT, size INTEGER,
            mtime REAL, category TEXT, PRIMARY KEY(source_id,path));
          CREATE TABLE IF NOT EXISTS source_scan_progress(
            source_id TEXT PRIMARY KEY, root TEXT NOT NULL, state TEXT NOT NULL, lease_until REAL NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS source_scan_seen(
            source_id TEXT NOT NULL, generation TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(source_id,generation,path));
          CREATE TABLE IF NOT EXISTS policies(
            root TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
            days INTEGER NOT NULL DEFAULT 7, next_run REAL DEFAULT 0,
            lease_until REAL DEFAULT 0, last_run TEXT DEFAULT '{}');
        ''')
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


def list_sources(cfg):
    root = _root(cfg)
    with _db() as conn:
        rows = conn.execute('SELECT * FROM sources WHERE root=? ORDER BY created_at,id', (root,)).fetchall()
    return {'items': [{k: v for k, v in dict(r).items() if k != 'root'} for r in rows]}


def source_candidates(cfg):
    root = Path(_core().root_path(cfg))
    proposed = [(root / '00_Management/Reports', 'AI 工作区管理报告', 'any'),
                (root / '40_Projects', '项目工作产物', 'any'),
                (root / '50_Training/Projects', '训练项目报告', 'any'),
                (root / '80_Knowledge', '既有知识文档（待甄别）', 'any'),
                (Path.home() / 'Documents/Codex', 'Codex 本地任务目录', 'codex'),
                (Path.home() / '.zcode/workspace/default/_report', 'ZCode 既有报告', 'zcode')]
    items = []
    for path, label, tool in proposed:
        try:
            validated = config.validate_asset_root(str(path))
            items.append({'path': validated, 'label': label, 'tool': tool, 'registered': False})
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
    if tool not in ('any', 'codex', 'zcode', 'workbuddy', 'dsh'):
        raise ValueError('不支持的工具类型。')
    with _db() as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute('SELECT * FROM sources WHERE root=? AND path=?', (root, path)).fetchone()
        if old:
            return dict(old)
        if conn.execute('SELECT count(*) FROM sources WHERE root=?', (root,)).fetchone()[0] >= 50:
            raise ValueError('每个工作区最多登记 50 个报告来源。')
        row = {'id': uuid.uuid4().hex, 'path': path, 'label': label, 'tool': tool, 'created_at': time.time()}
        conn.execute('INSERT INTO sources(id,root,path,label,tool,created_at) VALUES(?,?,?,?,?,?)',
                     (row['id'], root, path, label, tool, row['created_at']))
    return row


def category(path):
    value = str(path).replace('\\', '/').casefold()
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


def _scan_source(cfg, body):
    root, ident = _root(cfg), body.get('source_id')
    with _db() as conn:
        source = conn.execute('SELECT * FROM sources WHERE root=? AND id=?', (root, ident)).fetchone()
    if not source:
        raise ValueError('来源不存在或不属于当前工作区。')
    base = _source_path(source['path'])
    root_identity = os.stat(base)
    identity = [str(root_identity.st_dev), str(root_identity.st_ino)]
    with _db() as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        saved = conn.execute('SELECT * FROM source_scan_progress WHERE root=? AND source_id=?', (root, ident)).fetchone()
        if saved and saved['lease_until'] > time.time():
            raise ValueError('该来源正在盘点，请等待当前批次结束。')
        state = json.loads(saved['state']) if saved else None
        if state and state.get('identity') != identity:
            raise ValueError('来源目录身份改变，保留旧索引，请重新确认来源。')
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
            relative, depth, after = queue.popleft()
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
                with os.scandir(folder) as stream:
                    # Bound the name snapshot as well as stat calls. Exceptionally huge
                    # directories remain explicitly partial; siblings still get a turn.
                    entries = []
                    for entry in stream:
                        if len(entries) >= MAX_FILES or time.monotonic() - started >= SCAN_SECONDS:
                            if len(errors) < 20:
                                errors.append('目录名称枚举达到预算，未完整盘点：' + folder)
                            break
                        entries.append(entry)
                    entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))
                remaining = [entry for entry in entries if entry.name.casefold() + '\0' + entry.name > after]
                cursor = after
                for index, entry in enumerate(remaining):
                    if len(rows) >= MAX_ITEMS or examined >= MAX_FILES or time.monotonic() - started >= SCAN_SECONDS:
                        queue.append([relative, depth, cursor])
                        break
                    examined += 1
                    cursor = entry.name.casefold() + '\0' + entry.name
                    if not _safe_name(entry.name):
                        continue
                    try:
                        info = os.lstat(entry.path)
                        if config._is_reparse(info):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if _descend_report_folder(entry.path, base):
                                if depth < 12:
                                    queue.append([os.path.relpath(entry.path, base), depth + 1, ''])
                                elif len(errors) < 20:
                                    errors.append('达到目录深度上限：' + entry.path)
                        elif (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                              and inventory_path_allowed(entry.path, base)):
                            rows.append({'source_id': ident, 'path': entry.path, 'title': entry.name,
                                         'size': info.st_size, 'mtime': info.st_mtime, 'category': category(entry.path)})
                    except OSError as error:
                        if len(errors) < 20:
                            errors.append(str(error)[:300])
            except (OSError, ValueError) as error:
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
            if not conn.execute('SELECT id FROM sources WHERE root=? AND id=? AND path=?', (root, ident, base)).fetchone():
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


def inventory(cfg):
    root = _root(cfg)
    with _db() as conn:
        rows = conn.execute('SELECT i.*,s.label,s.tool FROM inventory i JOIN sources s ON s.id=i.source_id '
                            'WHERE s.root=? ORDER BY i.mtime DESC,i.path LIMIT 500', (root,)).fetchall()
        count = conn.execute('SELECT count(*) FROM inventory i JOIN sources s ON s.id=i.source_id WHERE s.root=?', (root,)).fetchone()[0]
    return {'items': [dict(row) for row in rows], 'total': count, 'limit': 500}


def preview(cfg):
    result = _core().retention_preview(cfg)
    if isinstance(result, list):
        result = {'items': result}
    return {**result, 'policy': policy(cfg), 'last_run': policy(cfg)['last_run']}


def run_retention(cfg, scheduled=False):
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
        count = _core().retention_count(cfg, now=now)
        offset = json.loads(row['last_run']).get('next_offset', 0)
        if type(offset) is not int or offset < 0 or offset >= count:
            offset = 0
        initial = _core().retention_candidates(cfg, now=now, offset=offset, limit=100)
        items = initial.get('items', []) if isinstance(initial, dict) else initial
        for item in items[:100]:
            if scheduled and not policy(cfg)['enabled']:
                break
            result = _core().recycle_candidate(cfg, item['id'], recycle.recycle_file, now=now)
            results.append(result)
            if result.get('status') == 'error':
                errors.append(result.get('error') or result.get('reason') or '回收失败')
        removed = sum(r.get('status') == 'recycled' for r in results)
        next_offset = offset + len(results) - removed
        if next_offset >= count - removed or len(items) < 100:
            next_offset = 0
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
    read_actions = {'source_list': lambda: list_sources(cfg), 'source_candidates': lambda: source_candidates(cfg),
                    'source_inventory': lambda: inventory(cfg), 'retention_preview': lambda: preview(cfg)}
    ui_actions = {'source_add': lambda: add_source(cfg, body), 'source_scan': lambda: scan_source(cfg, body),
                  'retention_policy': lambda: save_policy(cfg, body), 'retention_run': lambda: run_retention(cfg)}
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
