"""Bounded project handoff and document discovery for the workbench and its clients."""
import hashlib
from pathlib import Path
import re

from . import collaboration, config, workcenter


NEXT_STEP_MARKER = '\n\n下一步\n'
TASK_LIMIT = 6
FILE_LIMIT = 6


def _text(value, field, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f'{field} 必须为 1 至 {maximum} 字符。')
    return value.strip()


def _choice(row):
    return {key: row[key] for key in ('id', 'name', 'path')}


def _resolve(cfg, project):
    project = _text(project, '项目名或 ID', 1000)
    if not workcenter.index_ready(cfg):
        return {'state': 'building', 'retry_after_seconds': 3}
    try:
        candidates = workcenter.lookup_indexed_project(cfg, project)
    except workcenter.workcenter_index.IndexPending:
        return {'state': 'building', 'retry_after_seconds': 3}
    if len(candidates) != 1:
        return {'state': 'ambiguous' if candidates else 'not_found',
                'candidates': [_choice(row) for row in candidates]}
    return {'state': 'ready', 'project': _choice(candidates[0])}


def _document_item(row):
    return {key: row.get(key) for key in ('id', 'title', 'path', 'project_id', 'project_name',
            'task_id', 'category', 'status', 'mtime', 'intake_status')}


def search_documents(cfg, params):
    if set(params) - {'query', 'project', 'page', 'page_size'}:
        raise ValueError('文档检索参数不正确。')
    query = params.get('query', '')
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError('检索词最多 500 字符。')
    query = query.strip()
    if not query and not params.get('project'):
        raise ValueError('请提供项目名或检索词。')
    page, size = params.get('page', 1), params.get('page_size', 10)
    if type(page) is not int or not 1 <= page <= 100000 or type(size) is not int or not 1 <= size <= 30:
        raise ValueError('页码或每页数量超出范围。')
    filters = {'query': query, 'page': page, 'page_size': size}
    if params.get('project'):
        resolved = _resolve(cfg, params['project'])
        if resolved['state'] != 'ready':
            return resolved
        filters['project_id'] = resolved['project']['id']
    elif not workcenter.index_ready(cfg):
        return {'state': 'building', 'retry_after_seconds': 3}
    result = workcenter.list_documents(cfg, filters)
    return {'state': 'ready', 'items': [_document_item(row) for row in result['items']],
            'total': result['total'], 'page': page, 'page_size': size, 'index': result.get('index')}


def _task_state(cfg, project_path):
    root = workcenter._workspace(cfg)
    path = Path(project_path)
    empty = {'tasks': [], 'open_task_count': 0, 'latest_handoff': None, 'last_completion': None}
    # Collaboration task IDs live under 40_Projects/<slug>. Never associate
    # a training or tool directory with a task on display name alone.
    if config._key(path.parent) != config._key(Path(root) / '40_Projects'):
        return empty
    try:
        slug = collaboration._segment(path.name, 'project')
    except ValueError:
        return empty
    with workcenter._readonly('collaboration.sqlite3') as con:
        if con is None or 'tasks' not in workcenter._tables(con):
            return empty
        columns = {row[1] for row in con.execute('PRAGMA table_info(tasks)')}
        live = ' AND t.deleted_at IS NULL' if 'deleted_at' in columns else ''
        # Windows paths are case-insensitive, including Unicode spellings.
        con.create_function('casefold', 1, str.casefold)
        where = ' WHERE t.root=? AND casefold(t.project)=?' + live
        args = (config._key(root), slug.casefold())
        fields = ('t.id,t.title,t.status,t.target_tool,t.owner,t.updated_at,'
                  'substr(t.summary,1,1800) AS summary,substr(t.description,1,900) AS description')
        open_where = where + " AND t.status IN ('active','queued')"
        tasks = [dict(row) for row in con.execute('SELECT ' + fields + ' FROM tasks t' + open_where +
                 " ORDER BY (t.status='active') DESC,t.updated_at DESC,t.id LIMIT ?", (*args, TASK_LIMIT))]
        count = con.execute('SELECT count(*) FROM tasks t' + open_where, args).fetchone()[0]
        last = con.execute('SELECT ' + fields + ' FROM tasks t' + where +
                           " AND t.status='completed' ORDER BY t.updated_at DESC,t.id LIMIT 1", args).fetchone()
        handoff = None
        if 'audit' in workcenter._tables(con):
            row = con.execute('SELECT a.object_id AS task_id,t.title,t.status,a.created_at,a.detail,a.actor '
                              'FROM audit a JOIN tasks t ON t.id=a.object_id AND t.root=a.root' + where +
                              " AND a.action='task_handoff' ORDER BY a.id DESC LIMIT 1", args).fetchone()
            if row:
                handoff = dict(row)
                detail = handoff.pop('detail') or ''
                if detail.startswith(NEXT_STEP_MARKER.lstrip()):
                    detail = '\n\n' + detail
                summary, marker, step = detail.rpartition(NEXT_STEP_MARKER)
                handoff['summary'] = (summary if marker else step)[:1800]
                handoff['next_step'] = step[:1200] if marker else ''
        return {'tasks': tasks, 'open_task_count': count, 'latest_handoff': handoff,
                'last_completion': dict(last) if last else None}


def read_excerpt(cfg, params):
    if set(params) - {'document_id', 'offset', 'max_chars', 'preview_sha256'}:
        raise ValueError('文档读取参数不正确。')
    ident = params.get('document_id')
    if not isinstance(ident, str) or not re.fullmatch(r'doc_[0-9a-f]{32}', ident):
        raise ValueError('请使用检索结果中的文档 ID。')
    offset, limit = params.get('offset', 0), params.get('max_chars', 4000)
    if type(offset) is not int or not 0 <= offset <= workcenter.PREVIEW_BYTES or type(limit) is not int or not 1 <= limit <= 12000:
        raise ValueError('文档读取范围不正确。')
    expected = params.get('preview_sha256')
    if (offset or expected is not None) and (not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected)):
        raise ValueError('续读须提供上一页的预览摘要。')
    if not workcenter.index_ready(cfg):
        return {'state': 'building', 'retry_after_seconds': 3}
    doc = workcenter.read_document(cfg, ident)
    metadata = _document_item(doc)
    if not doc['preview_supported']:
        return {'state': 'metadata_only', 'document': metadata, 'reason': doc['reason']}
    content = doc['content']
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    if expected is not None and expected != digest:
        return {'state': 'changed', 'document': metadata, 'restart_offset': 0}
    end = min(offset + limit, len(content))
    return {'state': 'ready', 'document': metadata, 'content': content[offset:end], 'offset': offset,
            'next_offset': end if end < len(content) else None, 'preview_sha256': digest,
            'preview_truncated': doc['truncated']}


def resume(cfg, params):
    if set(params) != {'project'}:
        raise ValueError('请提供项目名、目录或项目 ID。')
    result = _resolve(cfg, params['project'])
    if result['state'] != 'ready':
        return result
    project = result['project']
    progress = _task_state(cfg, project['path'])
    files = workcenter.list_documents(cfg, {'project_id': project['id'], 'page_size': FILE_LIMIT})
    handoff = progress['latest_handoff']
    if handoff and handoff['status'] in ('queued', 'active') and handoff['next_step']:
        step = {'source': 'handoff', 'task_id': handoff['task_id'], 'text': handoff['next_step']}
    elif progress['tasks']:
        task = progress['tasks'][0]
        step = {'source': 'task', 'task_id': task['id'], 'text': '核对任务说明和领取状态：' + task['title']}
    else:
        step = {'source': 'none', 'task_id': None, 'text': '暂无待领取或执行中的任务。'}
    focus = step['task_id'] or (progress['last_completion'] or {}).get('id')
    related = []
    if focus and files['total']:
        related = workcenter.list_documents(cfg, {'project_id': project['id'], 'task_id': focus,
                                                  'intake_status': 'registered', 'page_size': FILE_LIMIT})['items']
    # Keep the supporting evidence for the next step ahead of unrelated recent files.
    selected = {}
    for row in related + files['items']:
        selected.setdefault(row['id'], _document_item(row))
    return {**result, **progress, 'next_step': step, 'files': list(selected.values())[:FILE_LIMIT],
            'document_count': files['total'], 'index': files.get('index')}
