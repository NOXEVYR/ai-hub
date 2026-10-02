"""Read-only document federation; user labels live separately from source files.

An index entry is discovery evidence, never permission to run, move or delete it.
Document IDs are resolved against the current root-scoped catalog on every action.
"""
import collections
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time

from . import config, management, collaboration_maintenance as maintenance, collaboration

CATEGORIES = collaboration.CATEGORIES
CLASSIFICATIONS = {'classified': '已分类', 'needs_review': '待确认'}
CATEGORY_SOURCES = {'manual': '人工校正', 'submitted': 'AI 提交声明', 'inferred': '线索推断', 'unclassified': '未分类'}
RETENTIONS = {'retained': '保留资料', 'temp_expiring': '临时到期候选', 'temp_pinned': '临时资料已固定保留'}
UNKNOWN_ATTRIBUTION = ':unknown:'  # ':' cannot occur in a validated client/task ID.
INTAKES = {'registered': '协作登记', 'indexed': '来源盘点', 'legacy': '既有资料库'}
PROJECT_KINDS = {'registered': '正式项目', 'candidate': '未登记目录候选', 'template': '模板目录', 'source': '资料来源'}
TASK_SUMMARY_LIMIT = 20
TEXT = {'.md', '.txt', '.rst', '.html', '.htm'}
PREVIEW_BYTES = 400 * 1024
_LOCK = threading.RLock()
_REMOVAL_PREVIEWS = {}
REMOVAL_PREVIEW_SECONDS = 300


def _workspace(cfg):
    # Legacy report browsing still works before opting into managed collaboration.
    return config.validate_asset_root(cfg.get('ai_root'))


def _identifier(root, path, prefix='doc'):
    raw = config._key(root) + '\0' + config._key(path)
    return prefix + '_' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


def _database_path(name):
    path = Path(config.DATA_DIR) / name
    config._check_ancestors(str(path.parent))
    for suffix in ('', '-wal', '-shm', '-journal'):
        candidate = Path(str(path) + suffix)
        if os.path.lexists(candidate):
            info = candidate.lstat()
            if config._is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('工作中心数据库不能是链接或特殊文件。')
    return path


@contextlib.contextmanager
def _readonly(name):
    path = _database_path(name)
    if not path.exists():
        yield None
        return
    con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA query_only=ON')
        con.execute('BEGIN')
        yield con
    finally:
        con.close()


def _tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _overrides(root):
    with _readonly('workcenter.sqlite3') as con:
        if con is None:
            return {}, {}
        tables = _tables(con)
        labels = {r['document_id']: {'category': r['category']} for r in con.execute(
            'SELECT document_id,category FROM document_labels WHERE root=?', (root,))} if 'document_labels' in tables else {}
        if 'document_label_evidence' in tables:
            for row in con.execute('SELECT * FROM document_label_evidence WHERE root=?', (root,)):
                if row['document_id'] in labels:
                    labels[row['document_id']]['identity'] = row['identity']
        names = {r['project_id']: r['name'] for r in con.execute(
            'SELECT project_id,name FROM project_labels WHERE root=?', (root,))} if 'project_labels' in tables else {}
        return labels, names


def _removals(root):
    # Reading or previewing never creates or upgrades the management database.
    with _readonly('workcenter.sqlite3') as con:
        if con is None or 'document_removals' not in _tables(con):
            return {}
        return {row['document_id']: dict(row) for row in con.execute(
            'SELECT * FROM document_removals WHERE root=? AND restored_at IS NULL', (root,))}


def _removal_root(cfg, body=None):
    root = _workspace(cfg)
    if body is not None:
        if not isinstance(body, dict):
            raise ValueError('文档移除请求须为对象。')
        requested = body.get('_workspace_root')
        if not isinstance(requested, str) or config._key(requested) != config._key(root):
            raise ValueError('工作环境已切换，请刷新文档列表。')
    info = os.stat(root)
    return root, config._key(root), json.dumps([str(info.st_dev), str(info.st_ino)])


def _removal_fingerprint(doc):
    # No body reads: list removal grants no permission to access source content.
    def identity(path):
        try:
            info = os.lstat(path)
            return [str(info.st_dev), str(info.st_ino), str(info.st_mode), str(info.st_nlink),
                    str(info.st_size), str(info.st_mtime_ns)]
        except OSError:
            return None
    boundary = identity(doc['_boundary'])
    payload = {'document': doc, 'file': identity(doc['path']), 'boundary': boundary[:2] if boundary else None}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def _removal_record(root, document_id):
    with _readonly('workcenter.sqlite3') as con:
        if con is None or 'document_removals' not in _tables(con):
            return None
        row = con.execute('SELECT * FROM document_removals WHERE root=? AND document_id=?', (root, document_id)).fetchone()
        return dict(row) if row else None


@contextlib.contextmanager
def _removal_write():
    path = _database_path('workcenter.sqlite3')
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=10)
    try:
        if existed:
            backup_path = _database_path('workcenter.sqlite3.pre-removal-' + str(time.time_ns()) + '.backup')
            backup = sqlite3.connect(str(backup_path))
            try:
                con.backup(backup)
            finally:
                backup.close()
        con.execute('BEGIN IMMEDIATE')
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def removal_preview(cfg, body):
    with _LOCK:
        root, root_key, root_identity = _removal_root(cfg, body)
        doc = _lookup(cfg, body.get('document_id'))
        now = time.time()
        for token in list(_REMOVAL_PREVIEWS):
            if _REMOVAL_PREVIEWS[token]['expires_at'] <= now:
                del _REMOVAL_PREVIEWS[token]
        if len(_REMOVAL_PREVIEWS) >= 100:
            raise ValueError('待确认文档预览过多，请稍后再试。')
        token = secrets.token_urlsafe(32)
        warning = ('这是正式报告或已提交成果，移出后会影响报告列表中的查阅和追溯。'
                   if doc['category'] == 'report' or doc['intake_status'] == 'registered' else '该文档将不再显示在报告列表中。')
        warning += '原文件、提交记录和原工具记忆全部保留；可以在“已移出列表”中恢复。'
        result = {'preview_token': token, 'document': _public(doc), 'warning': warning,
                  'expires_at': now + REMOVAL_PREVIEW_SECONDS, 'files_preserved': True, 'workspace_root': root}
        _REMOVAL_PREVIEWS[token] = {**result, 'root': root_key, 'root_identity': root_identity,
            'database': config._key(config.DATA_DIR), 'fingerprint': _removal_fingerprint(doc),
            'removal_state': _removal_record(root_key, doc['id'])}
        return result


def removal_apply(cfg, body):
    with _LOCK:
        root, root_key, root_identity = _removal_root(cfg, body)
        token = body.get('preview_token')
        pending = _REMOVAL_PREVIEWS.get(token) if isinstance(token, str) else None
        if not pending or pending['expires_at'] <= time.time():
            raise ValueError('移出预览已过期或不存在，请重新预览。')
        if (pending['root'] != root_key or pending['root_identity'] != root_identity
                or pending['database'] != config._key(config.DATA_DIR)):
            raise ValueError('工作环境或目录身份已变化，请重新预览。')
        def validate():
            if (_removal_root(cfg, body)[2] != root_identity
                    or pending['removal_state'] != _removal_record(root_key, pending['document']['id'])
                    or pending['fingerprint'] != _removal_fingerprint(_lookup(cfg, pending['document']['id']))):
                raise ValueError('文档或收录记录已变化，请重新预览后确认。')
        validate()
        with _removal_write() as con:
            validate()
            con.execute('CREATE TABLE IF NOT EXISTS document_removals(root TEXT,document_id TEXT,root_identity TEXT,document_json TEXT,removed_at REAL,restored_at REAL,PRIMARY KEY(root,document_id))')
            con.execute('INSERT OR REPLACE INTO document_removals VALUES(?,?,?,?,?,NULL)',
                        (root_key, pending['document']['id'], root_identity,
                         json.dumps(pending['document'], ensure_ascii=False), time.time()))
        del _REMOVAL_PREVIEWS[token]
        return {'removed': True, 'document_id': pending['document']['id'], 'files_preserved': True, 'workspace_root': root}


def removal_list(cfg, params=None):
    root, root_key, root_identity = _removal_root(cfg)
    params = params or {}
    page, size = _pagination(params)
    items = []
    for record in _removals(root_key).values():
        doc = json.loads(record['document_json'])
        items.append({**doc, 'removed_at': record['removed_at'],
                      'can_restore': record['root_identity'] == root_identity, 'files_preserved': True})
    items.sort(key=lambda doc: doc['removed_at'], reverse=True)
    query = str(params.get('query', '')).casefold().strip()
    if query:
        items = [doc for doc in items if query in ' '.join(str(doc.get(key, '')) for key in ('title', 'path', 'project_name')).casefold()]
    return {'root': root, 'workspace_root': root, 'items': items[(page - 1) * size:page * size],
            'total': len(items), 'page': page, 'page_size': size, 'files_preserved': True}


def removal_restore(cfg, body):
    with _LOCK:
        root, root_key, root_identity = _removal_root(cfg, body)
        ident = body.get('document_id')
        if not isinstance(ident, str) or not re.fullmatch(r'doc_[0-9a-f]{32}', ident):
            raise ValueError('文档 ID 格式不正确。')
        record = _removals(root_key).get(ident)
        if not record:
            raise ValueError('该文档不在当前工作环境的已移出列表中。')
        if record['root_identity'] != root_identity:
            raise ValueError('工作环境目录身份已变化，不能恢复旧环境记录。')
        with _removal_write() as con:
            if _removal_root(cfg, body)[2] != root_identity:
                raise ValueError('工作环境目录身份已变化，请刷新重试。')
            cursor = con.execute('UPDATE document_removals SET restored_at=? WHERE root=? AND document_id=? AND removed_at=? AND restored_at IS NULL',
                                 (time.time(), root_key, ident, record['removed_at']))
            if cursor.rowcount != 1:
                raise ValueError('移出记录已变化，请刷新重试。')
        return {'restored': True, 'document_id': ident, 'files_preserved': True, 'workspace_root': root,
                'note': '已解除列表隐藏；文档仍须属于当前允许的收录来源，已移除任务或产物须分别恢复。'}


def _path_status(path, boundary):
    """Validate lexical spelling before link resolution; missing files remain visible."""
    try:
        raw = os.fspath(path)
        if not os.path.isabs(raw) or '..' in raw.replace('\\', '/').split('/'):
            return 'rejected'
        if not config._within(raw, boundary) or config._key(raw) == config._key(boundary):
            return 'rejected'
        if any(ord(char) < 32 for char in raw) or (os.name == 'nt' and ':' in raw[2:]):
            return 'rejected'
        config._check_ancestors(os.path.dirname(raw))
        info = os.lstat(raw)
        if config._is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return 'rejected'
        return 'available'
    except FileNotFoundError:
        return 'missing'
    except (OSError, ValueError, TypeError):
        return 'rejected'


def _label_identity(path):
    info = os.lstat(path)
    if config._is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('分类只支持当前普通文件。')
    return json.dumps([str(info.st_dev), str(info.st_ino), str(info.st_size), str(info.st_mtime_ns)])


def _category(path, kind=None):
    if kind:
        return {'report': 'report', 'output': 'delivery', 'temp': 'temp_candidate'}[kind]
    name = Path(path).stem.casefold()
    parts = {part.casefold() for part in Path(path).parts}
    if parts & {'temp', 'tmp', 'scratch', 'drafts', '草稿', '临时'}:
        return 'temp_candidate'
    for category, words in (
        ('requirement', ('requirement', '需求', 'brief', '规格')),
        ('plan', ('plan', '方案', '计划', '设计')),
        ('report', ('report', 'review', 'validation', '验收', '报告', '总结', '复盘', '审计')),
        ('delivery', ('delivery', 'deliverable', '交付')),
        ('reference', ('readme', 'agents', 'guide', 'memory', 'sop', '指南', '规范', '知识', '说明', '记忆'))):
        if any(word in name for word in words):
            return category
    return 'other_text'


def _project_index(projects):
    """Normalize once per catalog; an explicit registration wins an equal path."""
    result = {}
    priority = {'registered': 3, 'discovered': 2, 'inferred': 1, 'source': 0}
    for project in projects:
        key = config._key(project['path'])
        previous = result.get(key)
        if previous is None or priority.get(project['origin'], 0) > priority.get(previous['origin'], 0):
            result[key] = project
    return result


def _template_path(path, root):
    # The workspace's own name and external ancestors carry no project role.
    # External candidates can describe their own directory, never their parents.
    parts = (Path(os.path.relpath(path, root)).parts if config._within(path, root)
             else (Path(path).name,))
    return any(re.sub(r'^\d+[_-]', '', part.casefold()) in
               {'template', 'templates', '模板', '项目模板'} or part.casefold().startswith('_template_')
               for part in parts)


def _creation_manifest(path):
    """Accept only a bounded, identity-stable manifest emitted by project creation."""
    manifest = Path(path) / '.aihub-project.json'
    fd = None
    try:
        config._check_ancestors(str(manifest.parent))
        before = manifest.lstat()
        if config._is_reparse(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 65536:
            return None
        fd = os.open(manifest, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            raw = handle.read(65537)
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if len(raw) > 65536 or identity(before) != identity(os.fstat(fd)) or identity(before) != identity(manifest.lstat()):
            return None
        value = json.loads(raw.decode('utf-8-sig'))
        if (not isinstance(value, dict) or value.get('owner') != 'AIHub.projects' or type(value.get('version')) is not int
                or value['version'] != 1 or not isinstance(value.get('root'), str) or not os.path.isabs(value['root'])
                or config._key(value['root']) != config._key(path) or value.get('name') != Path(path).name
                or value.get('enforcement') != 'guidance_only' or value.get('prompt_path') != 'TASK_BRIEF.md'
                or value.get('input_read_only') != ['Inputs']
                or value.get('write_directories') != ['Work', 'Outputs', 'Deliverables']
                or not isinstance(value.get('tools'), dict) or len(value['tools']) > 100
                or not all(isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', key)
                           and record == {'mode': 'guidance_only'} for key, record in value['tools'].items())):
            return None
        return value
    except (OSError, ValueError, UnicodeError, TypeError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _project_kind(project, root):
    if _template_path(project['path'], root):
        return 'template', 'directory_role_hint'
    if project.get('origin') == 'registered':
        return 'registered', 'project_registry'
    if project.get('origin') != 'source' and _creation_manifest(project['path']):
        return 'registered', 'creation_manifest'
    return ('source', 'source_directory') if project.get('origin') == 'source' else ('candidate', 'directory_discovery')


def _project_for(path, source, project_index, root):
    # Walking normalized ancestors costs path depth, not document-count x project-count.
    cursor = config._key(path)
    while True:
        current = project_index.get(cursor)
        if current is not None:
            return dict(current)
        parent = os.path.dirname(cursor)
        if parent == cursor:
            break
        cursor = parent
    base = Path(source['path'])
    relative = Path(path).relative_to(base)
    if source.get('tool') == 'codex' and len(relative.parts) >= 3 and re.fullmatch(r'\d{4}-\d{2}-\d{2}', relative.parts[0]):
        folder = base / relative.parts[0] / relative.parts[1]
        return {'path': str(folder), 'name': relative.parts[1], 'origin': 'inferred'}
    for area in ('40_Projects', '50_Training/Projects', '10_Apps'):
        folder = Path(root) / area
        if config._within(path, folder):
            tail = Path(path).relative_to(folder)
            if len(tail.parts) > 1:
                project = folder / tail.parts[0]
                return {'path': str(project), 'name': project.name, 'origin': 'discovered'}
    return {'path': str(base), 'name': source['label'], 'origin': 'source'}


def _catalog(cfg, include_removed=False):
    root = _workspace(cfg)
    root_key = config._key(root)
    managed_projects = management.projects(cfg)
    projects = [{**p, 'origin': 'registered' if p.get('registered') else 'discovered'}
                for p in managed_projects['items']
                if not any('.pre-update-' in part.casefold() or '.pre-migration-' in part.casefold()
                           for part in Path(p['path']).parts)]
    rows, source_rows, revoked_source_paths = [], [], []
    with _readonly('collaboration-maintenance.sqlite3') as con:
        if con is not None and {'sources', 'inventory'} <= _tables(con):
            source_columns = {row[1] for row in con.execute('PRAGMA table_info(sources)')}
            source_filter = ' AND deleted_at IS NULL' if 'deleted_at' in source_columns else ''
            if 'deleted_at' in source_columns:
                revoked_source_paths = [r['path'] for r in con.execute(
                    'SELECT path FROM sources WHERE root=? AND deleted_at IS NOT NULL', (root_key,))]
            source_rows = [dict(r) for r in con.execute('SELECT * FROM sources WHERE root=?' + source_filter + ' ORDER BY created_at,id', (root_key,))]
            indexed = [dict(r) for r in con.execute(
                'SELECT i.*,s.path AS source_path,s.label,s.tool FROM inventory i JOIN sources s ON s.id=i.source_id WHERE s.root=?' + source_filter.replace('deleted_at', 's.deleted_at') + ' ORDER BY s.created_at,s.id,i.path', (root_key,))]
        else:
            indexed = []
    counts, excluded = collections.Counter(), collections.Counter()
    sources = {s['id']: s for s in source_rows}
    invalid_sources = set()
    project_discovery_partial = False
    for source in source_rows:
        source_project = {'path': source['path'], 'name': source['label'], 'origin': 'source',
                          'tool': source['tool'], '_source_status': 'unscanned' if not source['scanned_at'] else 'available'}
        projects.append(source_project)
        try:
            maintenance._source_path(source['path'])
            if not maintenance.source_identity_matches(source):
                raise ValueError('来源目录身份已改变。')
        except (ValueError, OSError):
            invalid_sources.add(source['id'])
            source_project['_source_status'] = 'unavailable'
            continue
        # Date/task directories are candidates, not registry entries or progress evidence.
        if source['tool'] == 'codex':
            discovered_count = 0
            source_partial = False
            try:
                for day in Path(source['path']).iterdir():
                    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day.name):
                        continue
                    info = day.lstat()
                    if config._is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                        continue
                    config._check_ancestors(str(day))
                    for task in day.iterdir():
                        info = task.lstat()
                        if config._is_reparse(info) or not stat.S_ISDIR(info.st_mode) or not maintenance._safe_name(task.name):
                            continue
                        projects.append({'path': str(task), 'name': task.name, 'origin': 'inferred', 'tool': 'codex'})
                        discovered_count += 1
                        if discovered_count >= 5000:
                            project_discovery_partial = True
                            source_partial = True
                            break
                    if source_partial:
                        break
            except OSError:
                project_discovery_partial = True
    for entry in indexed:
        source = sources[entry['source_id']]
        if not maintenance.inventory_path_allowed(entry['path'], source['path']) or not config._within(entry['path'], source['path']):
            excluded[source['id']] += 1
            continue
        counts[source['id']] += 1
        rows.append((entry, {**source, '_invalid': source['id'] in invalid_sources or bool(entry.get('historical'))}, 'indexed', None))
    legacy_diagnostics = {}
    legacy_reports = management.reports(cfg, config.REPORTS_DIR, diagnostics=legacy_diagnostics)
    for entry in legacy_reports:
        path = Path(entry['path'])
        if path.suffix.casefold() not in maintenance.TEXT_EXTENSIONS:
            continue
        # Revoking an explicit source must also stop implicit rediscovery of
        # its files. Other active sources and submitted artifacts retain their
        # own authority; only the legacy fallback is suppressed here.
        if any(config._within(str(path), revoked) for revoked in revoked_source_paths):
            continue
        source = {'path': str(path.parent), 'label': entry.get('group', '既有资料库'), 'tool': 'any'}
        rows.append((entry, source, 'legacy', None))
    blocked_paths = set()
    with _readonly('collaboration.sqlite3') as con:
        if con is not None and 'tasks' in _tables(con):
            task_columns = {row[1] for row in con.execute('PRAGMA table_info(tasks)')}
            if {'root', 'id', 'project', 'title', 'status', 'target_tool'} <= task_columns:
                task_filter = ' AND deleted_at IS NULL' if 'deleted_at' in task_columns else ''
                for task in con.execute('SELECT id,project,title,status,target_tool FROM tasks WHERE root=?' + task_filter + ' ORDER BY id', (root_key,)):
                    try:
                        from .collaboration import _segment
                        name = _segment(task['project'], 'project')
                    except ValueError:
                        continue
                    projects.append({'path': str(Path(root) / '40_Projects' / name), 'name': name,
                                     'origin': 'discovered', '_task': dict(task)})
        if con is not None and {'tasks', 'artifacts'} <= _tables(con):
            task_columns = {row[1] for row in con.execute('PRAGMA table_info(tasks)')}
            artifact_columns = {row[1] for row in con.execute('PRAGMA table_info(artifacts)')}
            # A legacy schema may lack attribution fields. Read-only federation
            # must not migrate it or infer missing identities from paths.
            task_fields = ','.join(('t.' + column if column in task_columns else 'NULL') + ' AS ' + alias
                                  for column, alias in (('title', 'task_title'), ('status', 'task_status'),
                                                        ('target_tool', 'task_target_tool'), ('owner', 'task_owner_id')))
            task_deleted = 't.deleted_at' if 'deleted_at' in task_columns else 'NULL'
            artifacts = con.execute('SELECT a.*,t.project,' + task_fields + ',' + task_deleted + ' AS _task_deleted_at FROM artifacts a '
                                    'JOIN tasks t ON a.task_id=t.id AND a.root=t.root WHERE a.root=?', (root_key,)).fetchall()
            clients = {}
            if 'clients' in _tables(con):
                client_columns = {row[1] for row in con.execute('PRAGMA table_info(clients)')}
                if {'root', 'id', 'name', 'tool'} <= client_columns:
                    clients = {row['id']: dict(row) for row in con.execute(
                        'SELECT id,name,tool FROM clients WHERE root=?', (root_key,))}
            for raw in artifacts:
                entry = dict(raw)
                if entry.get('deleted_at') or entry['_task_deleted_at']:
                    blocked_paths.add(config._key(entry['path']))
                    continue
                if entry['status'] == 'recycled':
                    continue
                if entry['kind'] not in ('report', 'output', 'temp'):
                    continue
                from .collaboration import _segment, KINDS
                try:
                    project = _segment(entry['project'], 'project')
                    task_id = _segment(entry['task_id'], 'task')
                except ValueError:
                    continue
                boundary = Path(root) / '40_Projects' / project / 'Work/AIHub' / task_id / KINDS[entry['kind']]
                if not config._within(entry['path'], boundary):
                    continue
                projects.append({'path': str(Path(root) / '40_Projects' / project), 'name': project,
                                 'origin': 'discovered', '_artifact': {'id': entry['id'], 'source_tool': entry.get('source_tool')}})
                if Path(entry['path']).suffix.casefold() not in maintenance.TEXT_EXTENSIONS:
                    continue
                submitter = clients.get(entry.get('source_client_id'))
                entry['source_client_name'] = (submitter['name'] if submitter and entry.get('source_tool')
                                               and submitter['tool'] == entry['source_tool'] else None)
                if entry.get('task_status') != 'active':
                    entry['task_owner_id'] = None
                owner = clients.get(entry.get('task_owner_id'))
                owner_matches = owner is not None and entry.get('task_target_tool') in ('any', owner['tool'])
                entry['task_owner_name'] = owner['name'] if owner_matches else None
                entry['task_owner_tool'] = owner['tool'] if owner_matches else None
                entry['mtime'] = int(entry['mtime_ns']) / 1e9
                rows.append((entry, {'path': str(boundary), 'label': '协作产物登记', 'tool': entry.get('source_tool') or 'any'}, 'registered', entry['kind']))
    documents = {}
    project_index = _project_index([project for project in projects if project['origin'] != 'source'])
    project_kinds = {key: _project_kind(project, root)[0] for key, project in project_index.items()}
    ranks = {'legacy': 0, 'indexed': 1, 'registered': 2}
    for entry, source, intake, kind in rows:
        path = entry['path']
        if config._key(path) in blocked_paths:
            continue
        project = (_project_for(path, source, project_index, root))
        project_key = config._key(project['path'])
        if project_key not in project_kinds:
            project_kinds[project_key] = _project_kind(project, root)[0]
        ident = _identifier(root, path)
        doc = {'id': ident, 'title': entry.get('title') or entry.get('name') or Path(path).name, 'path': path,
               'project_id': _identifier(root, project['path'], 'project'),
               'project_name': project['name'], 'project_path': project['path'], 'project_origin': project['origin'],
               'project_kind': project_kinds[project_key],
               'tool': source.get('tool', 'any'), 'category': _category(os.path.relpath(path, source['path']), kind), 'intake_status': intake,
               'status': 'rejected' if source.get('_invalid') else _path_status(path, source['path']), 'size': entry.get('size', 0),
               'mtime': entry.get('mtime', 0), 'source_labels': [source['label']], '_boundary': source['path'], '_invalid': source.get('_invalid', False)}
        doc['_source_identity'] = source.get('identity')
        submitted = entry.get('category') if intake == 'registered' else None
        if submitted in CATEGORIES:
            doc['category'] = submitted
        doc.update(category_source=('submitted' if submitted in CATEGORIES else 'unclassified') if intake == 'registered'
                   else ('unclassified' if doc['category'] == 'other_text' else 'inferred'),
                   classification_status='classified' if submitted in CATEGORIES and submitted != 'other_text' else 'needs_review',
                   retention=('temp_pinned' if entry.get('pinned') else 'temp_expiring') if kind == 'temp' else 'retained',
                   retention_managed=intake == 'registered', expires_at=entry.get('expires_at'))
        for key in ('task_id', 'task_title', 'task_status', 'task_target_tool', 'task_owner_id',
                    'task_owner_name', 'task_owner_tool', 'source_client_id', 'source_client_name', 'source_tool'):
            doc[key] = entry.get(key) if intake == 'registered' else None
        if intake == 'registered':
            doc.update(artifact_id=entry['id'], task_id=entry['task_id'], source_client_id=entry.get('source_client_id'),
                       submission_evidence='registered_snapshot', _artifact_snapshot=entry)
            # Listings remain metadata-only. Verify the full digest only on use.
            try:
                info = os.lstat(path)
                matches = all(str(entry[key]) == str(value) for key, value in (
                    ('device', info.st_dev), ('inode', info.st_ino), ('size', info.st_size), ('mtime_ns', info.st_mtime_ns)))
            except OSError:
                matches = False
            if not matches or doc['status'] != 'available':
                doc.update(submission_evidence='snapshot_changed', classification_status='needs_review',
                           status='changed' if doc['status'] == 'available' else doc['status'], _invalid=True)
        previous = documents.get(ident)
        if previous:
            labels = sorted(set(previous['source_labels'] + doc['source_labels']))
            # Registered submissions always keep their snapshot boundary. Among
            # equally ranked discovery records, a live source wins stale history.
            previous_score = (ranks[previous['intake_status']], not previous['_invalid'], previous['tool'] != 'any')
            current_score = (ranks[intake], not doc['_invalid'], doc['tool'] != 'any')
            if current_score < previous_score:
                previous['source_labels'] = labels
                continue
            doc['source_labels'] = labels
        documents[ident] = doc
    labels, names = _overrides(root_key)
    for doc in documents.values():
        label = labels.get(doc['id'], {})
        try:
            matches = label.get('identity') == _label_identity(doc['path']) if label else False
        except (ValueError, OSError):
            matches = False
        doc['category_override_status'] = ('current' if matches else 'identity_changed' if label.get('identity')
                                           else 'legacy_unverified' if label else 'none')
        if label.get('category') in CATEGORIES and matches and not doc['_invalid'] and doc.get('submission_evidence') != 'snapshot_changed':
            doc['category'] = label['category']
            doc['category_manual'] = True
            doc['category_source'] = 'manual'
            doc['classification_status'] = 'needs_review' if doc['category'] == 'other_text' else 'classified'
        else:
            doc['category_manual'] = False
            if label:
                doc['classification_status'] = 'needs_review'
        if doc['project_id'] in names:
            doc['project_name'] = names[doc['project_id']]
    removals = _removals(root_key)
    if not include_removed:
        documents = {ident: doc for ident, doc in documents.items() if ident not in removals}
    coverage = []
    for source in source_rows:
        row = {key: source.get(key) for key in ('id', 'path', 'label', 'tool', 'scanned_at', 'file_count', 'truncated')}
        try:
            maintenance._source_path(source['path'])
            if not maintenance.source_identity_matches(source):
                raise ValueError('来源目录身份已改变。')
            row['status'] = 'unscanned' if not source['scanned_at'] else ('partial' if source['truncated'] else 'complete')
        except (ValueError, OSError):
            row['status'] = 'unavailable'
        row.update(visible_documents=counts[source['id']], excluded_documents=excluded[source['id']])
        coverage.append(row)
    intake_counts = collections.Counter(doc['intake_status'] for doc in documents.values())
    return root, list(documents.values()), projects, {'sources': coverage,
        'catalog_documents': len(documents), 'registered_documents': intake_counts['registered'],
        'indexed_visible_documents': intake_counts['indexed'], 'legacy_documents': intake_counts['legacy'],
        'partial_sources': sum(r['status'] == 'partial' for r in coverage),
        'unscanned_sources': sum(r['status'] == 'unscanned' for r in coverage),
        'unavailable_sources': sum(r['status'] == 'unavailable' for r in coverage),
        'indexed_documents': len(indexed), 'excluded_documents': sum(excluded.values()),
        'project_discovery_partial': project_discovery_partial or managed_projects.get('discovery', {}).get('partial', False),
        'project_discovery': managed_projects.get('discovery', {}),
        'legacy_discovery_partial': bool(legacy_diagnostics.get('partial_locations') or legacy_diagnostics.get('unavailable_locations')),
        'legacy_discovery': legacy_diagnostics,
        'warnings': list(dict.fromkeys(managed_projects.get('warnings', []) + legacy_diagnostics.get('warnings', []))),
        'note': '数量来自已登记来源和既有资料库；未盘点或部分盘点不代表没有文档。'}


def _public(doc):
    return {key: value for key, value in doc.items() if not key.startswith('_')}


def _pagination(params):
    def number(key, default, maximum):
        value = params.get(key, default)
        if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)) or not 1 <= int(value) <= maximum:
            raise ValueError(key + '超出允许范围。')
        return int(value)
    return number('page', 1, 1000000), number('page_size', 50, 100)


def _filtered(documents, params):
    query = params.get('query', '')
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError('搜索词格式不正确。')
    keys = ('project_id', 'tool', 'category', 'intake_status', 'classification_status', 'task_id', 'source_client_id')
    for key in keys:
        if params.get(key) is not None and not isinstance(params[key], str):
            raise ValueError('筛选项格式不正确。')
    return [d for d in documents if all(not params.get(key) or
            ((not d.get(key)) if params[key] == UNKNOWN_ATTRIBUTION and key in ('task_id', 'source_client_id')
             else d.get(key) == params[key]) for key in keys)
            and (not query or query.casefold() in '\n'.join(filter(None, (d['title'], d['path'], d['project_name'],
                 d.get('task_title'), d.get('task_id'), d.get('source_client_name'), d.get('source_client_id')))).casefold())]


def _facets(documents):
    result = {}
    dictionaries = {'category': CATEGORIES, 'intake_status': INTAKES, 'classification_status': CLASSIFICATIONS,
                    'category_source': CATEGORY_SOURCES, 'retention': RETENTIONS}
    for key, plural in (('project_id', 'projects'), ('tool', 'tools'), ('category', 'categories'), ('intake_status', 'intake_statuses'),
                        ('classification_status', 'classification_statuses'), ('category_source', 'category_sources'), ('retention', 'retentions')):
        counts = collections.Counter(d[key] for d in documents)
        labels = {d[key]: d['project_name'] if key == 'project_id' else dictionaries.get(key, {}).get(d[key], d[key]) for d in documents}
        result[plural] = [{'value': value, 'label': labels[value], 'count': count} for value, count in sorted(counts.items(), key=lambda item: (labels[item[0]].casefold(), item[0]))]
    result['needs_review_count'] = sum(d['classification_status'] == 'needs_review' for d in documents)
    for key, plural in (('task_id', 'tasks'), ('source_client_id', 'submitters')):
        counts, labels = collections.Counter(), {}
        for doc in documents:
            value = doc.get(key) or UNKNOWN_ATTRIBUTION
            counts[value] += 1
            if value == UNKNOWN_ATTRIBUTION:
                labels[value] = '未登记任务' if key == 'task_id' else '提交者未知'
            elif key == 'task_id':
                labels[value] = doc['project_name'] + ' · ' + (doc.get('task_title') or '任务标题未知') + ' · ' + value
            else:
                labels[value] = (doc.get('source_client_name') or '客户端名称未知') + ' · ' + value
        result[plural] = [{'value': value, 'label': labels[value], 'count': count}
                          for value, count in sorted(counts.items(), key=lambda item: (labels[item[0]].casefold(), item[0]))]
    return result


def list_documents(cfg, params=None):
    params = params or {}
    page, page_size = _pagination(params)
    root, documents, _, coverage = _catalog(cfg)
    selected = _filtered(documents, params)
    selected.sort(key=lambda d: (-(d['mtime'] or 0), d['path'].casefold(), d['id']))
    start = (page - 1) * page_size
    return {'items': [_public(d) for d in selected[start:start + page_size]], 'total': len(selected),
            'page': page, 'page_size': page_size, 'workspace_root': root,
            'facets': _facets(documents), 'coverage': coverage}


def list_projects(cfg, params=None):
    params = params or {}
    page, page_size = _pagination(params)
    root, documents, discovered, coverage = _catalog(cfg)
    # Validate document filters even when this workspace has no documents.
    _filtered([], params)
    kind = params.get('entry_kind', 'all' if params.get('project_id') else 'project')
    if not isinstance(kind, str) or kind not in {'project', 'candidate', 'template', 'source', 'all'}:
        raise ValueError('项目目录分类不受支持。')
    if params.get('source_type') is not None and not isinstance(params['source_type'], str):
        raise ValueError('来源类型格式不正确。')
    _, names = _overrides(config._key(root))
    candidates = list(discovered)
    candidates.extend({'path': doc['project_path'], 'name': doc['project_name'],
                       'origin': doc['project_origin'], 'tool': doc['tool']} for doc in documents)
    canonical = _project_index(candidates)
    grouped, project_docs = {}, collections.defaultdict(list)
    for path_key, project in canonical.items():
        ident = _identifier(root, project['path'], 'project')
        project_kind, evidence = _project_kind(project, root)
        entry_kind = 'project' if project_kind == 'registered' else project_kind
        grouped[ident] = {'id': ident, 'name': names.get(ident, project['name']), 'path': project['path'],
            'origin': project['origin'], 'entry_kind': entry_kind, 'classification_evidence': evidence,
            'registration_status': ('registered' if project['origin'] == 'registered' else 'created' if evidence == 'creation_manifest' else 'unregistered'),
            'registry_id': project.get('id'), 'project_type': project.get('type'),
            'document_count': 0, 'report_count': 0, 'artifact_count': 0,
            'status': project.get('_source_status', 'unscanned'), 'representative_document_id': None,
            'source_types': set(), 'linked_tools': set(), 'task_count': 0, 'task_status_counts': collections.Counter(),
            'tasks': [], 'task_summary_limit': TASK_SUMMARY_LIMIT,
            '_task_ids': set(), '_task_search': [], '_artifact_ids': set(),
            'association_evidence': 'registered_tasks_and_artifacts_only'}
    for project in candidates:
        p = grouped[_identifier(root, project['path'], 'project')]
        if project.get('tool'):
            p['source_types'].add(project['tool'])
        if project.get('_artifact'):
            artifact = project['_artifact']
            p['_artifact_ids'].add(artifact['id'])
            if artifact.get('source_tool') and artifact['source_tool'] != 'any':
                p['linked_tools'].add(artifact['source_tool'])
        if project.get('_task'):
            task = project['_task']
            p['task_count'] += 1
            p['_task_ids'].add(task['id'])
            p['_task_search'].append(task['title'] + '\n' + task['id'])
            p['task_status_counts'][task['status']] += 1
            if len(p['tasks']) < TASK_SUMMARY_LIMIT:
                p['tasks'].append({key: task[key] for key in ('id', 'title', 'status', 'target_tool')})
            if task['target_tool'] != 'any':
                p['linked_tools'].add(task['target_tool'])
    for doc in documents:
        p = grouped[doc['project_id']]
        project_docs[p['id']].append(doc)
        p['document_count'] += 1
        p['report_count'] += doc['category'] == 'report'
        p['source_types'].add(doc['tool'])
        if doc['status'] == 'available':
            p['status'] = 'available'
            if p['representative_document_id'] is None:
                p['representative_document_id'] = doc['id']
        elif p['status'] == 'unscanned':
            p['status'] = doc['status']
        if doc.get('artifact_id'):
            p['_artifact_ids'].add(doc['artifact_id'])
            if doc.get('source_tool') and doc['source_tool'] != 'any':
                p['linked_tools'].add(doc['source_tool'])
    for p in grouped.values():
        p['source_types'] = sorted(p['source_types'] or {'any'})
        p['linked_tools'] = sorted(p['linked_tools'])
        p['tool'] = p['source_types'][0] if len(p['source_types']) == 1 else 'any'
        p['task_status_counts'] = dict(p['task_status_counts'])
        p['artifact_count'] = len(p['_artifact_ids'])
    counts = {key: sum(p['entry_kind'] == key for p in grouped.values())
              for key in ('project', 'candidate', 'template', 'source')}
    type_counts = collections.Counter(tool for p in grouped.values() for tool in p['source_types'])
    facets = {'entry_kinds': [{'value': key, 'label': PROJECT_KINDS['registered' if key == 'project' else key], 'count': count}
                              for key, count in counts.items()],
              'source_types': [{'value': key, 'label': key, 'count': count} for key, count in sorted(type_counts.items())]}
    # Match a project's own metadata or ANY matching document, without shrinking
    # the summary to a filtered report subset. Empty folders remain searchable.
    document_keys = ('category', 'intake_status', 'classification_status', 'task_id', 'source_client_id')
    matching_doc_params = {key: params[key] for key in document_keys if params.get(key)}
    query = params.get('query', '').casefold()
    result = []
    for p in grouped.values():
        if kind != 'all' and p['entry_kind'] != kind:
            continue
        if params.get('project_id') and p['id'] != params['project_id']:
            continue
        if params.get('source_type') and params['source_type'] not in p['source_types']:
            continue
        if params.get('tool') and params['tool'] not in p['source_types']:
            continue
        docs = _filtered(project_docs[p['id']], matching_doc_params)
        task_only_match = (set(matching_doc_params) == {'task_id'}
                           and matching_doc_params['task_id'] in p['_task_ids'])
        if matching_doc_params and not docs and not task_only_match:
            continue
        if (query and query not in (p['name'] + '\n' + p['path']).casefold()
                and not _filtered(docs, {'query': query})
                and not any(query in text.casefold() for text in p['_task_search'])):
            continue
        result.append(p)
    result.sort(key=lambda p: (p['name'].casefold(), p['id']))
    start = (page - 1) * page_size
    return {'items': [_public(p) for p in result[start:start + page_size]], 'total': len(result), 'page': page,
            'page_size': page_size, 'workspace_root': root, 'coverage': coverage,
            'entry_kind': kind, 'catalog_total': len(grouped), 'counts': counts, 'facets': facets}


def _lookup(cfg, document_id):
    if not isinstance(document_id, str) or not re.fullmatch(r'doc_[0-9a-f]{32}', document_id):
        raise ValueError('文档 ID 格式不正确。')
    _, documents, _, _ = _catalog(cfg)
    for doc in documents:
        if doc['id'] == document_id:
            return doc
    raise ValueError('文档未进入当前工作环境的允许索引。')


def lookup_document(cfg, path):
    if not isinstance(path, str) or not os.path.isabs(path) or '..' in path.replace('\\', '/').split('/'):
        raise ValueError('文档路径不合法。')
    doc = _lookup(cfg, _identifier(_workspace(cfg), path))
    return _public(doc)


def resolve_document(cfg, document_id):
    doc = _lookup(cfg, document_id)
    if doc['_invalid'] or _path_status(doc['path'], doc['_boundary']) != 'available':
        raise ValueError('文档已丢失、不可读或变成链接，不能打开。')
    _verify_submission(doc)
    return Path(doc['path'])


def _verify_submission(doc):
    snapshot = doc.get('_artifact_snapshot')
    if snapshot is not None and not collaboration._same_snapshot(snapshot, collaboration.file_snapshot(doc['path'], doc['_boundary'])):
        raise ValueError('协作登记的文件内容或身份已变化，不能沿用原提交证据。')


def _source_current(doc):
    try:
        return maintenance.source_identity_matches({'identity': doc.get('_source_identity'), 'path': doc['_boundary']})
    except (OSError, ValueError):
        return False


def read_document(cfg, document_id):
    doc = _lookup(cfg, document_id)
    path = Path(doc['path'])
    if doc['_invalid'] or not _source_current(doc) or _path_status(path, doc['_boundary']) != 'available':
        raise ValueError('文档已丢失、不可读或变成链接，不能预览。')
    _verify_submission(doc)
    result = {**_public(doc), 'path': str(path), 'content': '', 'truncated': False}
    if path.suffix.casefold() not in TEXT:
        return {**result, 'preview_supported': False, 'reason': '此格式仅显示元数据，请打开所在文件夹后使用本机阅读器。'}
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(str(path), flags)
    try:
        def identity(info):
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink
        opened = os.fstat(fd)
        if identity(before) != identity(opened) or opened.st_nlink != 1 or not stat.S_ISREG(opened.st_mode):
            raise ValueError('预览期间文档身份变化。')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            if doc.get('_artifact_snapshot') is None:
                content = handle.read(PREVIEW_BYTES + 1)
            else:
                digest, content, total = hashlib.sha256(), b'', 0
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    total += len(chunk)
                    if total > collaboration.MAX_ARTIFACT_BYTES:
                        raise ValueError('协作登记文件读取期间超过大小上限。')
                    digest.update(chunk)
                    content += chunk[:max(0, PREVIEW_BYTES + 1 - len(content))]
                if digest.hexdigest() != doc['_artifact_snapshot']['sha256']:
                    raise ValueError('协作登记的文件内容已变化，不能沿用原提交证据。')
        if identity(before) != identity(os.fstat(fd)) or identity(before) != identity(path.lstat()) or not _source_current(doc) or _path_status(path, doc['_boundary']) != 'available':
            raise ValueError('预览期间文档发生变化。')
    finally:
        os.close(fd)
    return {**result, 'content': content[:PREVIEW_BYTES].decode('utf-8-sig', errors='replace'),
            'preview_supported': True, 'truncated': len(content) > PREVIEW_BYTES, 'format': 'text'}


def classify(cfg, body):
    if not isinstance(body, dict):
        raise ValueError('分类请求须为对象。')
    doc = _lookup(cfg, body.get('document_id'))
    if doc.get('submission_evidence') == 'snapshot_changed':
        raise ValueError('协作登记文件已变化，不能沿用原提交进行分类。')
    _verify_submission(doc)
    root = config._key(_workspace(cfg))
    category, name = body.get('category'), body.get('project_name')
    if category is None and name is None:
        raise ValueError('请提供类别或项目显示名称。')
    if category is not None and (not isinstance(category, str) or category not in CATEGORIES):
        raise ValueError('文档类别不受支持。')
    if name is not None and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or any(ord(c) < 32 for c in name)):
        raise ValueError('项目显示名称须为 1–120 个可显示字符。')
    label_identity = None
    if category is not None:
        if doc['_invalid'] or not _source_current(doc) or _path_status(doc['path'], doc['_boundary']) != 'available':
            raise ValueError('文档已失效，不能保存当前分类。')
        label_identity = _label_identity(doc['path'])
    with _LOCK:
        path = _database_path('workcenter.sqlite3')
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path), timeout=10)
        try:
            if category is not None and 'document_labels' in _tables(con) and 'document_label_evidence' not in _tables(con):
                backup_path = Path(str(path) + '.pre-label-evidence-' + str(time.time_ns()) + '.backup')
                backup = sqlite3.connect(str(backup_path))
                try:
                    con.backup(backup)
                finally:
                    backup.close()
            con.execute('BEGIN IMMEDIATE')
            con.execute('CREATE TABLE IF NOT EXISTS document_labels(root TEXT,document_id TEXT,category TEXT,updated_at REAL,PRIMARY KEY(root,document_id))')
            con.execute('CREATE TABLE IF NOT EXISTS project_labels(root TEXT,project_id TEXT,name TEXT,updated_at REAL,PRIMARY KEY(root,project_id))')
            if category is not None:
                if label_identity != _label_identity(doc['path']):
                    raise ValueError('分类保存期间文档身份变化。')
                con.execute('CREATE TABLE IF NOT EXISTS document_label_evidence(root TEXT,document_id TEXT,identity TEXT,PRIMARY KEY(root,document_id))')
                con.execute('INSERT OR REPLACE INTO document_labels VALUES(?,?,?,?)', (root, doc['id'], category, time.time()))
                con.execute('INSERT OR REPLACE INTO document_label_evidence VALUES(?,?,?)', (root, doc['id'], label_identity))
            if name is not None:
                con.execute('INSERT OR REPLACE INTO project_labels VALUES(?,?,?,?)', (root, doc['project_id'], name.strip(), time.time()))
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()
    return _public(_lookup(cfg, doc['id']))
