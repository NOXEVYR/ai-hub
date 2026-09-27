"""Read-only document federation; user labels live separately from source files.

An index entry is discovery evidence, never permission to run, move or delete it.
Document IDs are resolved against the current root-scoped catalog on every action.
"""
import collections
import contextlib
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time

from . import config, management, collaboration_maintenance as maintenance

CATEGORIES = {'report': '报告', 'plan': '计划与方案', 'requirement': '需求',
              'delivery': '交付资料', 'reference': '参考与规范',
              'other_text': '待分类文档', 'temp_candidate': '临时资料候选'}
INTAKES = {'registered': '协作登记', 'indexed': '来源盘点', 'legacy': '既有资料库'}
TEXT = {'.md', '.txt', '.rst', '.html', '.htm'}
PREVIEW_BYTES = 400 * 1024
_LOCK = threading.RLock()


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
        labels = {r['document_id']: r['category'] for r in con.execute(
            'SELECT document_id,category FROM document_labels WHERE root=?', (root,))} if 'document_labels' in tables else {}
        names = {r['project_id']: r['name'] for r in con.execute(
            'SELECT project_id,name FROM project_labels WHERE root=?', (root,))} if 'project_labels' in tables else {}
        return labels, names


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


def _catalog(cfg):
    root = _workspace(cfg)
    root_key = config._key(root)
    projects = [{'path': p['path'], 'name': p['name'],
                 'origin': 'registered' if p.get('registered') else 'discovered'}
                for p in management.projects(cfg)['items']
                if not any('.pre-update-' in part.casefold() or '.pre-migration-' in part.casefold()
                           for part in Path(p['path']).parts)]
    rows, source_rows = [], []
    with _readonly('collaboration-maintenance.sqlite3') as con:
        if con is not None and {'sources', 'inventory'} <= _tables(con):
            source_rows = [dict(r) for r in con.execute('SELECT * FROM sources WHERE root=? ORDER BY created_at,id', (root_key,))]
            indexed = [dict(r) for r in con.execute(
                'SELECT i.*,s.path AS source_path,s.label,s.tool FROM inventory i JOIN sources s ON s.id=i.source_id WHERE s.root=? ORDER BY s.created_at,s.id,i.path', (root_key,))]
        else:
            indexed = []
    counts, excluded = collections.Counter(), collections.Counter()
    sources = {s['id']: s for s in source_rows}
    invalid_sources = set()
    project_discovery_partial = False
    for source in source_rows:
        try:
            maintenance._source_path(source['path'])
        except (ValueError, OSError):
            invalid_sources.add(source['id'])
            continue
        # Date/task directories are candidates, not registry entries or progress evidence.
        if source['tool'] == 'codex':
            discovered_count = 0
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
                            break
                    if project_discovery_partial:
                        break
            except OSError:
                project_discovery_partial = True
    for entry in indexed:
        source = sources[entry['source_id']]
        if not maintenance.inventory_path_allowed(entry['path'], source['path']) or not config._within(entry['path'], source['path']):
            excluded[source['id']] += 1
            continue
        counts[source['id']] += 1
        rows.append((entry, {**source, '_invalid': source['id'] in invalid_sources}, 'indexed', None))
    for entry in management.reports(cfg, config.REPORTS_DIR):
        path = Path(entry['path'])
        if path.suffix.casefold() not in maintenance.TEXT_EXTENSIONS:
            continue
        source = {'path': str(path.parent), 'label': entry.get('group', '既有资料库'), 'tool': 'any'}
        rows.append((entry, source, 'legacy', None))
    with _readonly('collaboration.sqlite3') as con:
        if con is not None and {'tasks', 'artifacts'} <= _tables(con):
            artifacts = con.execute('SELECT a.*,t.project,t.target_tool FROM artifacts a JOIN tasks t ON a.task_id=t.id AND a.root=t.root WHERE a.root=? AND a.status<>?', (root_key, 'recycled')).fetchall()
            for raw in artifacts:
                entry = dict(raw)
                if entry['kind'] not in ('report', 'output', 'temp') or Path(entry['path']).suffix.casefold() not in maintenance.TEXT_EXTENSIONS:
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
                entry['mtime'] = int(entry['mtime_ns']) / 1e9
                rows.append((entry, {'path': str(boundary), 'label': '协作产物登记', 'tool': entry['target_tool']}, 'registered', entry['kind']))
    documents = {}
    project_index = _project_index(projects)
    ranks = {'legacy': 0, 'indexed': 1, 'registered': 2}
    for entry, source, intake, kind in rows:
        path = entry['path']
        project = _project_for(path, source, project_index, root)
        ident = _identifier(root, path)
        doc = {'id': ident, 'title': entry.get('title') or entry.get('name') or Path(path).name, 'path': path,
               'project_id': _identifier(root, project['path'], 'project'),
               'project_name': project['name'], 'project_path': project['path'], 'project_origin': project['origin'],
               'tool': source.get('tool', 'any'), 'category': _category(os.path.relpath(path, source['path']), kind), 'intake_status': intake,
               'status': 'rejected' if source.get('_invalid') else _path_status(path, source['path']), 'size': entry.get('size', 0),
               'mtime': entry.get('mtime', 0), 'source_labels': [source['label']], '_boundary': source['path'], '_invalid': source.get('_invalid', False)}
        previous = documents.get(ident)
        if previous:
            labels = sorted(set(previous['source_labels'] + doc['source_labels']))
            if (ranks[intake] < ranks[previous['intake_status']]
                    or (ranks[intake] == ranks[previous['intake_status']] and previous['tool'] != 'any' and doc['tool'] == 'any')):
                previous['source_labels'] = labels
                continue
            doc['source_labels'] = labels
        documents[ident] = doc
    labels, names = _overrides(root_key)
    for doc in documents.values():
        if labels.get(doc['id']) in CATEGORIES:
            doc['category'] = labels[doc['id']]
            doc['category_manual'] = True
        else:
            doc['category_manual'] = False
        if doc['project_id'] in names:
            doc['project_name'] = names[doc['project_id']]
    coverage = []
    for source in source_rows:
        row = {key: source.get(key) for key in ('id', 'path', 'label', 'tool', 'scanned_at', 'file_count', 'truncated')}
        try:
            maintenance._source_path(source['path'])
            row['status'] = 'unscanned' if not source['scanned_at'] else ('partial' if source['truncated'] else 'complete')
        except (ValueError, OSError):
            row['status'] = 'unavailable'
        row.update(visible_documents=counts[source['id']], excluded_documents=excluded[source['id']])
        coverage.append(row)
    return root, list(documents.values()), projects, {'sources': coverage,
        'partial_sources': sum(r['status'] == 'partial' for r in coverage),
        'unscanned_sources': sum(r['status'] == 'unscanned' for r in coverage),
        'unavailable_sources': sum(r['status'] == 'unavailable' for r in coverage),
        'indexed_documents': len(indexed), 'excluded_documents': sum(excluded.values()),
        'project_discovery_partial': project_discovery_partial,
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
    keys = ('project_id', 'tool', 'category', 'intake_status')
    for key in keys:
        if params.get(key) is not None and not isinstance(params[key], str):
            raise ValueError('筛选项格式不正确。')
    return [d for d in documents if all(not params.get(key) or d[key] == params[key] for key in keys)
            and (not query or query.casefold() in '\n'.join((d['title'], d['path'], d['project_name'])).casefold())]


def _facets(documents):
    result = {}
    for key, plural in (('project_id', 'projects'), ('tool', 'tools'), ('category', 'categories'), ('intake_status', 'intake_statuses')):
        counts = collections.Counter(d[key] for d in documents)
        labels = {d[key]: d['project_name'] if key == 'project_id' else (CATEGORIES if key == 'category' else INTAKES if key == 'intake_status' else {}).get(d[key], d[key]) for d in documents}
        result[plural] = [{'value': value, 'label': labels[value], 'count': count} for value, count in sorted(counts.items(), key=lambda item: (labels[item[0]].casefold(), item[0]))]
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
    selected = _filtered(documents, params)
    grouped = {}
    for doc in selected:
        p = grouped.setdefault(doc['project_id'], {'id': doc['project_id'], 'name': doc['project_name'],
            'path': doc['project_path'], 'origin': doc['project_origin'], 'tool': doc['tool'],
            'document_count': 0, 'report_count': 0, 'status': 'available'})
        p['document_count'] += 1
        p['report_count'] += doc['category'] == 'report'
        if p['tool'] != doc['tool']:
            p['tool'] = 'any'
    # Preserve known physical projects even when no reports have been indexed yet.
    if not any(params.get(k) for k in ('category', 'intake_status')):
        _, names = _overrides(config._key(root))
        for project in discovered:
            ident = _identifier(root, project['path'], 'project')
            name = names.get(ident, project['name'])
            if ((params.get('project_id') and params['project_id'] != ident)
                    or (params.get('tool') and params['tool'] != project.get('tool', 'any'))
                    or (params.get('query') and params['query'].casefold() not in (name + '\n' + project['path']).casefold())):
                continue
            grouped.setdefault(ident, {'id': ident, 'name': name,
                'path': project['path'], 'origin': project['origin'], 'tool': project.get('tool', 'any'),
                'document_count': 0, 'report_count': 0, 'status': 'unscanned'})
    result = sorted(grouped.values(), key=lambda p: (p['name'].casefold(), p['id']))
    start = (page - 1) * page_size
    return {'items': result[start:start + page_size], 'total': len(result), 'page': page,
            'page_size': page_size, 'workspace_root': root, 'coverage': coverage}


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
    return Path(doc['path'])


def read_document(cfg, document_id):
    doc = _lookup(cfg, document_id)
    path = Path(doc['path'])
    if doc['_invalid'] or _path_status(path, doc['_boundary']) != 'available':
        raise ValueError('文档已丢失、不可读或变成链接，不能预览。')
    result = {'id': doc['id'], 'title': doc['title'], 'path': str(path), 'content': '', 'truncated': False}
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
            content = handle.read(PREVIEW_BYTES + 1)
        if identity(before) != identity(os.fstat(fd)) or identity(before) != identity(path.lstat()) or _path_status(path, doc['_boundary']) != 'available':
            raise ValueError('预览期间文档发生变化。')
    finally:
        os.close(fd)
    return {**result, 'content': content[:PREVIEW_BYTES].decode('utf-8-sig', errors='replace'),
            'preview_supported': True, 'truncated': len(content) > PREVIEW_BYTES, 'format': 'text'}


def classify(cfg, body):
    if not isinstance(body, dict):
        raise ValueError('分类请求须为对象。')
    doc = _lookup(cfg, body.get('document_id'))
    root = config._key(_workspace(cfg))
    category, name = body.get('category'), body.get('project_name')
    if category is None and name is None:
        raise ValueError('请提供类别或项目显示名称。')
    if category is not None and (not isinstance(category, str) or category not in CATEGORIES):
        raise ValueError('文档类别不受支持。')
    if name is not None and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or any(ord(c) < 32 for c in name)):
        raise ValueError('项目显示名称须为 1–120 个可显示字符。')
    with _LOCK:
        path = _database_path('workcenter.sqlite3')
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path), timeout=10)
        try:
            con.execute('BEGIN IMMEDIATE')
            con.execute('CREATE TABLE IF NOT EXISTS document_labels(root TEXT,document_id TEXT,category TEXT,updated_at REAL,PRIMARY KEY(root,document_id))')
            con.execute('CREATE TABLE IF NOT EXISTS project_labels(root TEXT,project_id TEXT,name TEXT,updated_at REAL,PRIMARY KEY(root,project_id))')
            if category is not None:
                con.execute('INSERT OR REPLACE INTO document_labels VALUES(?,?,?,?)', (root, doc['id'], category, time.time()))
            if name is not None:
                con.execute('INSERT OR REPLACE INTO project_labels VALUES(?,?,?,?)', (root, doc['project_id'], name.strip(), time.time()))
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()
    return _public(_lookup(cfg, doc['id']))
