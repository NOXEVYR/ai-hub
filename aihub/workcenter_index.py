"""Disposable, workspace-scoped metadata index for large workcenter catalogs.

The index never opens a document or authorizes an action. Small catalogs retain
their live path checks. Large catalogs rebuild on authority changes and refresh
filesystem observations in the background while a consistent snapshot is read.
"""
from collections import Counter, defaultdict
import concurrent.futures
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from . import config

MIN_RECORDS = 256
MAX_AGE = 300
VERSION = 2
_lock = threading.RLock()
_jobs = {}
_fallback = {}
_large_workspaces = set()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def live_status():
    return {'state': 'live', 'built_at': None, 'max_age_seconds': 0, 'persistent': False}


def _path(root):
    digest = hashlib.sha256(config._key(root).encode('utf-8')).hexdigest()[:24]
    return Path(config.DATA_DIR) / ('workcenter-index-v%d-%s.sqlite3' % (VERSION, digest))


def observe_size(root, docs, projects):
    if len(docs) + len(projects) >= MIN_RECORDS:
        with _lock:
            _large_workspaces.add(_path(root))


def _stamp(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    return [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size]


def _source_state(cfg):
    from . import workcenter as wc
    root = wc._workspace(cfg)
    root_key = config._key(root)
    path = _path(root)
    with _lock:
        large = path in _large_workspaces or path in _fallback
    large = large or path.exists()
    sources = []
    with wc._readonly('collaboration-maintenance.sqlite3') as con:
        if con is not None and 'sources' in wc._tables(con):
            sources = [dict(row) for row in con.execute('SELECT * FROM sources WHERE root=? ORDER BY id', (root_key,))]
    count = sum(s.get('file_count') or 0 for s in sources if not s.get('deleted_at'))
    with wc._readonly('collaboration.sqlite3') as con:
        audit, clients = None, []
        if con is not None:
            tables = wc._tables(con)
            if 'artifacts' in tables:
                count += con.execute('SELECT count(*) FROM artifacts WHERE root=?', (root_key,)).fetchone()[0]
            if 'tasks' in tables:
                count += con.execute('SELECT count(*) FROM tasks WHERE root=?', (root_key,)).fetchone()[0]
            if large or count >= MIN_RECORDS:
                # The skills bridge updates last_seen, context receipts and invocation
                # evidence frequently. Only catalog events and visible client names matter.
                if 'audit' in tables:
                    audit = con.execute('SELECT max(id) FROM audit WHERE root=? AND (action=\'task_create\' OR '
                                        'action LIKE \'artifact_%\' OR action LIKE \'%apply\' OR action IN '
                                        '(\'recycled\',\'recycle_error\',\'report_submit\',\'record_restore\','
                                        '\'execution_accept\',\'capability_dispatch\'))', (root_key,)).fetchone()[0]
                if ('source_client_id' in {r[1] for r in con.execute('PRAGMA table_info(artifacts)')}
                        and {'id', 'name', 'tool', 'root'} <= {r[1] for r in con.execute('PRAGMA table_info(clients)')}):
                    clients = [tuple(row) for row in con.execute(
                        'SELECT id,name,tool FROM clients WHERE root=? AND id IN '
                        '(SELECT source_client_id FROM artifacts WHERE root=? AND source_client_id IS NOT NULL) ORDER BY id',
                        (root_key, root_key))]
    if not large and count < MIN_RECORDS:
        return None
    for row in sources:
        if row.get('deleted_at'):
            continue
        try:
            row['available_identity'] = wc.maintenance.source_identity_matches(row)
        except (ValueError, OSError):
            row['available_identity'] = False
    data_dir = Path(config.DATA_DIR)
    # Snapshots are scoped to both the workspace and the installation's database directory.
    labels = wc._database_path('workcenter.sqlite3')
    root_info = os.stat(root)
    version = [VERSION, root_key, root_info.st_dev, root_info.st_ino, config._key(config.REPORTS_DIR),
               cfg.get('catalog_dir'), sources, count, audit, clients, _stamp(data_dir / 'registry.json'),
               _stamp(labels), _stamp(str(labels) + '-wal')]
    signature = hashlib.sha256(_json(version).encode('utf-8')).hexdigest()
    return root, path, signature


def _checked_path(path):
    from . import workcenter
    checked = workcenter._database_path(path.name)
    if checked != path:
        raise ValueError('目录索引所属的数据目录已变化。')
    return checked


@contextlib.contextmanager
def _reader(path):
    with _lock:
        memory = _fallback.get(path)
    if memory is not None:
        lock, connection = memory
        with lock:
            yield connection, False
        return
    _checked_path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
        con.execute('PRAGMA query_only=ON')
        con.execute('BEGIN')
        yield con, True


def _meta(con, name):
    row = con.execute('SELECT value FROM meta WHERE name=?', (name,)).fetchone()
    return json.loads(row[0]) if row else None


def _age(path, signature):
    try:
        with _reader(path) as (con, _):
            return _meta(con, 'built_at') if _meta(con, 'signature') == signature else None
    except (OSError, ValueError, sqlite3.Error):
        return None


_DOC_FILTERS = ('project_id', 'tool', 'category', 'intake_status', 'classification_status', 'task_id', 'source_client_id')


def _document_row(doc):
    from . import workcenter
    search = '\n'.join(filter(None, (doc['title'], doc['path'], doc['project_name'], doc.get('task_title'),
                                     doc.get('task_id'), doc.get('source_client_name'), doc.get('source_client_id')))).casefold()
    return (doc['id'], *(doc.get(k) or '' for k in _DOC_FILTERS), doc.get('mtime') or 0,
            doc['path'].casefold(), search, _json(workcenter._public(doc)))


def _fill(con, cfg, signature):
    from . import workcenter as wc
    root, docs, found, coverage = wc._catalog(cfg, metadata_only=True)
    projects, _, counts, project_facets = wc._group_projects(root, docs, found)
    con.executescript('''
        CREATE TABLE meta(name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE docs(id TEXT PRIMARY KEY, project_id TEXT, tool TEXT, category TEXT, intake_status TEXT,
            classification_status TEXT, task_id TEXT, source_client_id TEXT, mtime REAL, path_key TEXT, search TEXT, payload TEXT);
        CREATE TABLE projects(id TEXT PRIMARY KEY, kind TEXT, name_key TEXT, path_key TEXT, basename TEXT,
            has_tasks INTEGER, search TEXT, task_search TEXT, artifact_tools TEXT, payload TEXT);
        CREATE TABLE tools(project_id TEXT, tool TEXT, PRIMARY KEY(project_id, tool));
        CREATE TABLE tasks(project_id TEXT, task_id TEXT, PRIMARY KEY(project_id, task_id));
    ''')
    con.executemany('INSERT INTO docs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (_document_row(doc) for doc in docs))
    for project in projects.values():
        ident = project['id']
        con.execute('INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (ident, project['entry_kind'], project['name'].casefold(), config._key(project['path']),
                     Path(project['path']).name.casefold(), int(bool(project['task_count'])),
                     (project['name'] + '\n' + project['path']).casefold(), _json(project['_task_search']),
                     _json(sorted(project['_artifact_tools'])),
                     _json(wc._public(project))))
        con.executemany('INSERT INTO tools VALUES(?,?)', ((ident, tool) for tool in project['source_types']))
        con.executemany('INSERT INTO tasks VALUES(?,?)', ((ident, task) for task in project['_task_ids']))
    meta = {'signature': signature, 'coverage': coverage, 'root': root, 'document_facets': wc._facets(docs),
            'project_facets': project_facets, 'counts': counts, 'catalog_total': len(projects), 'built_at': time.time()}
    con.executemany('INSERT INTO meta VALUES(?,?)', ((name, _json(value)) for name, value in meta.items()))
    con.executescript('''
        CREATE INDEX document_order ON docs(mtime DESC, path_key, id);
        CREATE INDEX document_project ON docs(project_id, mtime DESC, path_key, id);
        CREATE INDEX document_task ON docs(task_id);
        CREATE INDEX project_order ON projects(kind, name_key, id);
        CREATE INDEX project_name ON projects(name_key);
        CREATE INDEX project_path ON projects(path_key);
        CREATE INDEX project_basename ON projects(basename);
    ''')
    con.commit()


def _build(cfg, state):
    root, path, signature = state
    con = sqlite3.connect(':memory:', check_same_thread=False)
    try:
        _fill(con, cfg, signature)
        if _source_state(cfg) != state:
            return False  # A newer registry/source version appeared during the build.
        try:
            _checked_path(path)
            with contextlib.closing(sqlite3.connect(str(path), timeout=5)) as target:
                deadline = time.monotonic() + 5
                def bounded_wait(status, remaining, total):
                    if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() >= deadline:
                        raise sqlite3.OperationalError('目录索引存储当前繁忙。')
                con.backup(target, pages=512, progress=bounded_wait, sleep=0.025)
        except (OSError, ValueError, sqlite3.Error):
            # Read-only installations and damaged disposable files can still serve
            # the complete index without changing any authoritative store.
            with _lock:
                _fallback[path] = (threading.RLock(), con)
            con = None
        else:
            with _lock:
                _fallback.pop(path, None)
        return True
    finally:
        if con is not None:
            con.close()


def _schedule(cfg, state, *, background):
    path = state[1]
    with _lock:
        existing = _jobs.get(path)
        if existing is not None:
            return existing
        job = concurrent.futures.Future()
        _jobs[path] = job

    def run():
        try:
            job.set_result(_build(cfg, state))
        except Exception as exc:
            job.set_exception(exc)
        finally:
            with _lock:
                if _jobs.get(path) is job:
                    del _jobs[path]

    if background:
        threading.Thread(target=run, name='workcenter-index', daemon=True).start()
    else:
        run()
    return job


def _prepared(cfg, *, force=False, wait=True):
    for _ in range(3):
        state = _source_state(cfg)
        if state is None:
            return None, 'live'
        _, path, signature = state
        built_at = _age(path, signature)
        if built_at is not None and not force:
            if time.time() - built_at > MAX_AGE:
                _schedule(cfg, state, background=True)
                return state, 'refreshing'
            return state, 'ready'
        job = _schedule(cfg, state, background=not wait)
        if not wait:
            return state, 'building'
        if job.result() and _source_state(cfg) == state and _age(path, signature) is not None:
            return state, 'ready'
        force = False
    raise ValueError('资料来源持续变化，目录索引正在更新，请稍后重试。')


def ready(cfg):
    _, state = _prepared(cfg, wait=False)
    return state != 'building'


def _doc_conditions(params, *, project=False):
    fields = _DOC_FILTERS[2:] if project else _DOC_FILTERS
    where, args = [], []
    for key in fields:
        value = params.get(key)
        if value:
            where.append('d.' + key + '=?')
            args.append('' if key in ('task_id', 'source_client_id') and value == ':unknown:' else value)
    return where, args


def _page(con, table, where, args, order, page, page_size):
    clause = ' FROM ' + table + (' WHERE ' + ' AND '.join(where) if where else '')
    total = con.execute('SELECT count(*)' + clause, args).fetchone()[0]
    items = con.execute('SELECT payload' + clause + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?',
                        [*args, page_size, (page - 1) * page_size])
    return {'items': [json.loads(row[0]) for row in items], 'total': total, 'page': page, 'page_size': page_size}


def _projects_page(con, params, page, page_size):
    kind = params.get('entry_kind', 'all' if params.get('project_id') else 'project')
    where, args = [], []
    if kind != 'all':
        where.append('p.kind=?')
        args.append(kind)
    if params.get('project_id'):
        where.append('p.id=?')
        args.append(params['project_id'])
    for key in ('source_type', 'tool'):
        if params.get(key):
            where.append('EXISTS (SELECT 1 FROM tools WHERE project_id=p.id AND tool=?)')
            args.append(params[key])
    filters, values = _doc_conditions(params, project=True)
    doc_sql = 'SELECT 1 FROM docs d WHERE d.project_id=p.id' + ''.join(' AND ' + condition for condition in filters)
    if filters:
        document_match = 'EXISTS (' + doc_sql + ')'
        args.extend(values)
        if filters == ['d.task_id=?']:
            document_match += ' OR EXISTS (SELECT 1 FROM tasks WHERE project_id=p.id AND task_id=?)'
            args.append(params['task_id'])
        where.append('(' + document_match + ')')
    query = (params.get('query') or '').casefold()
    if query:
        where.append('(instr(p.search,?) OR task_matches(p.task_search,?) OR EXISTS (' + doc_sql + ' AND instr(d.search,?)))')
        args.extend([query, query, *values, query])
        con.create_function('task_matches', 2, lambda raw, q: any(q in t.casefold() for t in json.loads(raw)))
    result = _page(con, 'projects p', where, args, 'p.name_key, p.id', page, page_size)
    result.update(entry_kind=kind, catalog_total=_meta(con, 'catalog_total'), counts=_meta(con, 'counts'),
                  facets=_meta(con, 'project_facets'))
    return result


def _task_rows(root, task_ids):
    from . import workcenter
    current = {}
    if not task_ids:
        return current
    with workcenter._readonly('collaboration.sqlite3') as auth:
        if auth is None or 'tasks' not in workcenter._tables(auth):
            return current
        columns = {row[1] for row in auth.execute('PRAGMA table_info(tasks)')}
        if not {'id', 'root', 'title', 'status', 'target_tool'} <= columns:
            return current
        owner = 't.owner' if 'owner' in columns else 'NULL'
        clients = {r[1] for r in auth.execute('PRAGMA table_info(clients)')}
        if {'root', 'id', 'name', 'tool'} <= clients:
            names = 'c.name AS owner_name,c.tool AS owner_tool'
            join = ' LEFT JOIN clients c ON c.root=t.root AND c.id=' + owner
        else:
            names, join = 'NULL AS owner_name,NULL AS owner_tool', ''
        ids = sorted(task_ids)
        for i in range(0, len(ids), 500):
            group = ids[i:i + 500]
            sql = ('SELECT t.id,t.title,t.status,t.target_tool,' + owner + ' AS owner,' + names + ' FROM tasks t' + join
                   + ' WHERE t.root=? AND t.id IN (' + ','.join('?' for _ in group) + ')')
            if 'deleted_at' in columns:
                sql += ' AND t.deleted_at IS NULL'
            current.update((row['id'], row) for row in auth.execute(sql, (config._key(root), *group)))
    return current


def _live_tasks(con, root, items, *, projects):
    """Overlay only task rows on this page. Ownership changes never rebuild the catalog."""
    if not projects:
        current = _task_rows(root, {doc['task_id'] for doc in items if doc.get('task_id')})
        for doc in items:
            task = current.get(doc.get('task_id'))
            if task is not None:
                owner = task['owner'] if task['status'] == 'active' else None
                matches = owner and task['target_tool'] in ('any', task['owner_tool'])
                doc.update(task_title=task['title'], task_status=task['status'], task_target_tool=task['target_tool'],
                           task_owner_id=owner, task_owner_name=task['owner_name'] if matches else None,
                           task_owner_tool=task['owner_tool'] if matches else None)
        return
    projects_with_tasks = {p['id']: p for p in items if p['task_count']}
    if not projects_with_tasks:
        return
    placeholders = ','.join('?' for _ in projects_with_tasks)
    groups = defaultdict(list)
    for project_id, task_id in con.execute('SELECT project_id,task_id FROM tasks WHERE project_id IN (' + placeholders
                                           + ') ORDER BY project_id,task_id',
                                           tuple(projects_with_tasks)):
        groups[project_id].append(task_id)
    current = _task_rows(root, {task_id for group in groups.values() for task_id in group})
    for ident, raw_tools in con.execute('SELECT id,artifact_tools FROM projects WHERE id IN (' + placeholders + ')',
                                        tuple(projects_with_tasks)):
        project = projects_with_tasks[ident]
        tasks = [current[tid] for tid in groups[ident] if tid in current]
        tools = set(json.loads(raw_tools))
        tools.update(task['target_tool'] for task in tasks if task['target_tool'] != 'any')
        project.update(task_count=len(tasks), task_status_counts=dict(Counter(task['status'] for task in tasks)),
                       tasks=[{key: t[key] for key in ('id', 'title', 'status', 'target_tool')}
                              for t in tasks[:project['task_summary_limit']]], linked_tools=sorted(tools))


def listing(cfg, table, params, page, page_size):
    force = str(params.get('refresh', '')).lower() in ('1', 'true')
    for _ in range(3):
        state, status = _prepared(cfg, force=force)
        if state is None:
            return None
        _, path, signature = state
        with _reader(path) as (con, persistent):
            if _meta(con, 'signature') != signature:
                force = False
                continue
            if table == 'documents':
                where, args = _doc_conditions(params)
                if params.get('query'):
                    where.append('instr(d.search,?)')
                    args.append(params['query'].casefold())
                result = _page(con, 'docs d', where, args, 'd.mtime DESC, d.path_key, d.id', page, page_size)
                result['facets'] = _meta(con, 'document_facets')
            else:
                result = _projects_page(con, params, page, page_size)
            _live_tasks(con, state[0], result['items'], projects=table == 'projects')
            result.update(workspace_root=_meta(con, 'root'), coverage=_meta(con, 'coverage'),
                          index={'state': status, 'built_at': _meta(con, 'built_at'), 'max_age_seconds': MAX_AGE,
                                 'persistent': persistent})
            return result
    raise ValueError('目录索引正在切换版本，请稍后重试。')


def lookup(cfg, project):
    state, status = _prepared(cfg, wait=False)
    if state is None:
        return None
    if status == 'building':
        raise IndexPending('目录索引正在建立，请稍后重试。')
    _, path, signature = state
    with _reader(path) as (con, _):
        if _meta(con, 'signature') != signature:
            raise IndexPending('目录索引正在更新，请稍后重试。')
        key = project.casefold()
        path_key = config._key(project) if os.path.isabs(project) else ''
        rows = con.execute('SELECT payload FROM projects WHERE (kind=\'project\' OR has_tasks=1) AND '
                           '(id=? OR name_key=? OR basename=? OR path_key=?) ORDER BY name_key,id LIMIT 12',
                           (key, key, key, path_key))
        result = [json.loads(row[0]) for row in rows]
        _live_tasks(con, state[0], result, projects=True)
        return result


class IndexPending(ValueError):
    """A nonblocking project lookup can be retried once its index is ready."""


def document_path_hint(cfg, document_id):
    """Use the list index only to locate a candidate for a fresh authority check."""
    state = _source_state(cfg)
    if state is None:
        return None
    try:
        with _reader(state[1]) as (con, _):
            row = con.execute('SELECT payload FROM docs WHERE id=?', (document_id,)).fetchone()
            return json.loads(row[0])['path'] if row else None
    except (OSError, ValueError, KeyError, sqlite3.Error):
        return None
