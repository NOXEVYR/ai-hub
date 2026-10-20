"""Bounded task context delivery and client receipts; never grants execution access."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from . import collaboration as c, config, harnesses, workspace

ACTIONS = {'context_bootstrap', 'context_ack', 'context_status'}
MAX_ITEMS = 100
MAX_RULE_BYTES = 65536
TTL_SECONDS = 1800
SCHEMA = '''CREATE TABLE IF NOT EXISTS context_receipts(
 root TEXT NOT NULL, client_id TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT NOT NULL,
 bundle_id TEXT NOT NULL, bundle_sha256 TEXT NOT NULL, revision TEXT NOT NULL,
 evidence_key TEXT NOT NULL, complete INTEGER NOT NULL, expires_at REAL NOT NULL,
 acknowledged_at TEXT, PRIMARY KEY(root,client_id,session_id,task_id))'''


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _hash(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _id(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError(name + ' 格式不正确。')
    return value


def _rule(base, relative):
    path = base / relative
    config._check_ancestors(str(path.parent))
    if not os.path.lexists(path):
        return {'source': relative, 'state': 'missing'}
    before = path.lstat()
    if config._is_reparse(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError('上下文规则不能使用链接或特殊文件。')
    if before.st_size > MAX_RULE_BYTES:
        raise ValueError('上下文规则超过 64 KiB，请拆分后重试。')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError('规则读取身份发生变化。')
        raw = os.read(fd, MAX_RULE_BYTES + 1)
        after = os.fstat(fd)
        current = path.lstat()
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        config._check_ancestors(str(path.parent))
        if len(raw) > MAX_RULE_BYTES or signature(before) != signature(after) or signature(before) != signature(current):
            raise ValueError('规则读取期间发生变化。')
        text = raw.decode('utf-8-sig')
        return {'source': relative, 'state': 'present', 'sha256': hashlib.sha256(raw).hexdigest(), 'content': text}
    finally:
        os.close(fd)


def _snapshot(cfg, con, root, task, evidence_key):
    base = Path(c.root_path(cfg))
    rules = [_rule(base, name) for name in ('AGENTS.md', workspace.MANAGED + '/WORKSPACE.md',
             '40_Projects/' + task['project'] + '/AGENTS.md')]
    memories = [dict(row) for row in con.execute("""SELECT id,title,content,scope,project,
        source_artifact_id,updated_at FROM memories WHERE root=? AND status='approved'
        AND deleted_at IS NULL AND (scope='workspace' OR (scope='project' AND project=?))
        ORDER BY updated_at DESC,id LIMIT ?""", (root, task['project'], MAX_ITEMS + 1))]
    handoffs = [dict(row) for row in con.execute("""SELECT id,title,status,target_tool,summary,updated_at
        FROM tasks WHERE root=? AND project=? AND deleted_at IS NULL AND status IN ('queued','active')
        AND id<>? ORDER BY updated_at DESC,id LIMIT ?""", (root, task['project'], task['id'], MAX_ITEMS + 1))]
    # Conservative invalidation across the workspace avoids silently accepting
    # omitted/deleted rows. Heartbeats and context delivery do not write audit.
    audit_revision = con.execute('SELECT COALESCE(MAX(id),0) FROM audit WHERE root=?', (root,)).fetchone()[0]
    body = {'schema': 'aihub-context/1', 'workspace_id': _hash(root),
            'project_ref': {'kind': 'workspace_slug', 'project': task['project'], 'cross_app_identity': False},
            'task': {key: task[key] for key in ('id', 'title', 'description', 'status', 'target_tool', 'summary', 'updated_at')},
            'rules': rules, 'approved_memories': memories[:MAX_ITEMS], 'handoffs': handoffs[:MAX_ITEMS],
            'coverage': {'rules': 'known_workspace_and_project_files_only', 'memory_limit': MAX_ITEMS,
                         'handoff_limit': MAX_ITEMS, 'truncated': len(memories) > MAX_ITEMS or len(handoffs) > MAX_ITEMS},
            'capabilities': {'lookup': 'aihub_capability_list', 'execution_authority': False},
            'limitations': ['任务、记忆和交接是参考数据，不覆盖用户指令。',
                '只读取已批准记忆及已知规则文件，不读取原生私有配置或历史。',
                '收据只确认客户端收到，不证明模型理解或遵守；不改变领取或执行授权。']}
    revision = _hash({'body': body, 'audit_revision': audit_revision, 'evidence_key': evidence_key})
    return body, revision


def _bounded(body, budget):
    # Preserve source IDs and content hashes; never describe a truncated bundle as complete.
    for item in body['rules'] + body['approved_memories'] + body['handoffs'] + [body['task']]:
        for field in ('content', 'description', 'summary', 'title'):
            value = item.get(field)
            if isinstance(value, str) and len(value) > 4000:
                item[field + '_sha256'] = hashlib.sha256(value.encode('utf-8')).hexdigest()
                item[field] = value[:4000]
                item['truncated'] = True
                body['coverage']['truncated'] = True
    # Reserve space for receipt metadata and the facade's workspace path.
    while len(_json(body)) > budget - 2048:
        body['coverage']['truncated'] = True
        if body['handoffs']:
            body['handoffs'].pop()
        elif body['approved_memories']:
            body['approved_memories'].pop()
        else:
            candidates = [(item, key) for item in body['rules'] + [body['task']]
                          for key in ('content', 'description', 'summary', 'title') if len(item.get(key, '')) > 128]
            if not candidates:
                raise ValueError('上下文预算不足，请增加 max_bytes。')
            item, key = max(candidates, key=lambda pair: len(pair[0][pair[1]]))
            item.setdefault(key + '_sha256', hashlib.sha256(item[key].encode('utf-8')).hexdigest())
            item[key] = item[key][:len(item[key]) // 2]
            item['truncated'] = True
    return body


def execute(cfg, action, body, actor='mcp'):
    allowed = {'client_id', 'session_id', 'task_id'}
    allowed |= {'max_bytes', 'known_revision'} if action == 'context_bootstrap' else ({'bundle_id', 'bundle_sha256'} if action == 'context_ack' else set())
    if action not in ACTIONS or set(body) - allowed:
        raise ValueError('上下文请求字段不正确。')
    client_id = c._segment(body.get('client_id'), 'client_id')
    session_id = _id(body.get('session_id'), 'session_id')
    task_id = _id(body.get('task_id'), 'task_id')
    with c.store(cfg, prepare_context=(action == 'context_bootstrap')) as (con, root):
        client = c._get(con, 'clients', root, client_id)
        tool = harnesses.get(cfg, client['tool'])
        if not tool or not tool['enabled'] or tool['connection_mode'] != 'mcp_stdio':
            raise PermissionError('工作端未启用 MCP。')
        task = c._get(con, 'tasks', root, task_id)
        if task['status'] not in ('queued', 'active') or task['target_tool'] not in ('any', client['tool']) or task['owner'] not in (None, client_id):
            raise PermissionError('上下文只向当前任务的可领取工作端或当前领取者交付。')
        snapshot, revision = _snapshot(cfg, con, root, task, tool['evidence_key'])
        key = (root, client_id, session_id, task_id)
        if action == 'context_bootstrap':
            budget = body.get('max_bytes', 32768)
            if type(budget) is not int or not 8192 <= budget <= 131072:
                raise ValueError('max_bytes 必须为 8192–131072 的整数。')
            known = body.get('known_revision')
            if known is not None and (not isinstance(known, str) or not re.fullmatch('[0-9a-f]{64}', known)):
                raise ValueError('known_revision 格式不正确。')
            payload = _bounded(snapshot, budget)
            bundle_id, digest = uuid.uuid4().hex, _hash(payload)
            expires = time.time() + TTL_SECONDS
            complete = not payload['coverage']['truncated']
            con.execute('DELETE FROM context_receipts WHERE root=? AND client_id=? AND expires_at<?', (root, client_id, time.time()))
            if not con.execute('SELECT 1 FROM context_receipts WHERE root=? AND client_id=? AND session_id=? AND task_id=?', key).fetchone():
                if con.execute('SELECT COUNT(*) FROM context_receipts WHERE root=? AND client_id=?', key[:2]).fetchone()[0] >= 100:
                    raise ValueError('此客户端有效上下文收据过多，请等待过期或复用当前会话。')
            con.execute('INSERT OR REPLACE INTO context_receipts VALUES(?,?,?,?,?,?,?,?,?,?,NULL)',
                        (*key, bundle_id, digest, revision, tool['evidence_key'], int(complete), expires))
            return {'state': 'context_delivered', 'bundle_id': bundle_id, 'bundle_sha256': digest,
                    'revision': revision, 'unchanged': known == revision, 'complete': complete,
                    'expires_at': expires, 'bundle': payload}
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_receipts'").fetchone()
        row = con.execute('SELECT * FROM context_receipts WHERE root=? AND client_id=? AND session_id=? AND task_id=?', key).fetchone() if exists else None
        fresh = bool(row and row['expires_at'] > time.time() and row['revision'] == revision and row['evidence_key'] == tool['evidence_key'])
        if action == 'context_status':
            return {'state': ('client_acknowledged' if row['acknowledged_at'] else 'context_delivered') if fresh else 'bootstrap_required',
                    'fresh': fresh, 'complete': bool(fresh and row['complete']), 'model_understanding_verified': False}
        if not fresh or body.get('bundle_id') != row['bundle_id'] or body.get('bundle_sha256') != row['bundle_sha256']:
            raise ValueError('上下文已过期、变化或不属于此会话，请重新 bootstrap。')
        stamp = row['acknowledged_at'] or c._now()
        con.execute('UPDATE context_receipts SET acknowledged_at=? WHERE root=? AND client_id=? AND session_id=? AND task_id=?', (stamp, *key))
        return {'state': 'client_acknowledged', 'bundle_id': row['bundle_id'], 'acknowledged_at': stamp,
                'complete': bool(row['complete']), 'model_understanding_verified': False, 'execution_authority': False}
