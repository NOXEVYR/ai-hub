"""Task-owned resource ledger; records client evidence, never controls resources."""
import collections
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import uuid

from . import collaboration as core, config

ACTIONS = {'resource_register', 'resource_list', 'resource_cleanup_report'}
TYPES = {'browser_tab', 'service_process', 'listener', 'temp_directory', 'output'}
OWNERSHIP = {'task_exclusive', 'shared', 'user_owned'}
STATES = {'running', 'cleanup_pending', 'closed', 'cleanup_failed', 'manual_required'}
_COLUMNS = {'id', 'root', 'task_id', 'client_id', 'lease_hash', 'resource_type',
            'identity', 'identity_key', 'ownership', 'ownership_declared', 'temporary',
            'label', 'state', 'evidence', 'sample', 'created_at', 'updated_at'}
_DDL = '''CREATE TABLE task_resources(
 id TEXT PRIMARY KEY, root TEXT NOT NULL, task_id TEXT NOT NULL,
 client_id TEXT NOT NULL, lease_hash TEXT NOT NULL, resource_type TEXT NOT NULL,
 identity TEXT NOT NULL, identity_key TEXT NOT NULL, ownership TEXT NOT NULL,
 ownership_declared INTEGER NOT NULL, temporary INTEGER NOT NULL,
 label TEXT NOT NULL, state TEXT NOT NULL, evidence TEXT NOT NULL,
 sample TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(root,resource_type,identity_key), FOREIGN KEY(task_id) REFERENCES tasks(id))'''


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


@contextlib.contextmanager
def _store(cfg, create=True):
    # The outer lock makes bootstrap, committed backup, and business transaction
    # one serialized operation. Backup must precede BEGIN IMMEDIATE.
    with core._LOCK:
        with core.store(cfg):
            pass
        path = Path(config.DATA_DIR) / 'collaboration.sqlite3'
        con = sqlite3.connect(str(path), timeout=30)
        try:
            columns = {r[1] for r in con.execute('PRAGMA table_info(task_resources)')}
            if columns and not _COLUMNS <= columns:
                raise ValueError('任务资源登记库版本不兼容，保留原库并停止操作。')
            if not columns and create:
                directory = path.parent / 'collaboration-schema-backups'
                config._check_ancestors(str(directory))
                directory.mkdir(exist_ok=True)
                backup = sqlite3.connect(str(directory / ('resources-' + uuid.uuid4().hex + '.sqlite3')))
                try:
                    con.backup(backup)
                finally:
                    backup.close()
                con.execute('BEGIN IMMEDIATE')
                con.execute(_DDL)
                con.execute('CREATE INDEX task_resource_owner ON task_resources(root,task_id,client_id)')
                con.commit()
        finally:
            con.close()
        with core.store(cfg) as value:
            yield value


def _keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError('资源字段缺失或含不支持的字段。')


def _stamp(value):
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError('时间必须是带时区的 ISO 格式。')
    # _now supports naive timestamps elsewhere; this protocol requires timezone.
    import datetime as dt
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('资源时间必须包含时区。')
        return core._now(parsed)
    except (ValueError, OverflowError) as exc:
        raise ValueError('资源时间须为有效的带时区 ISO 格式。') from exc


def _pid(value):
    if type(value) is not int or not 1 <= value <= 2 ** 32 - 1:
        raise ValueError('PID 必须是正整数。')
    return value


def _identity(kind, value, task, verify_path=True):
    if kind == 'browser_tab':
        fields = {'browser_id', 'session_id', 'tab_id'}
        _keys(value, fields, fields)
        return {k: core._text(value[k], k, 200) for k in fields}
    if kind in {'service_process', 'listener'}:
        fields = {'pid', 'process_started_at'} | ({'host', 'port'} if kind == 'listener' else set())
        _keys(value, fields, fields)
        result = {'pid': _pid(value['pid']), 'process_started_at': _stamp(value['process_started_at'])}
        if kind == 'listener':
            if not isinstance(value['host'], str) or value['host'] not in {'127.0.0.1', '::1'} or type(value['port']) is not int or not 1 <= value['port'] <= 65535:
                raise ValueError('监听资源只接受明确的本机回环地址与有效端口。')
            result.update(host=value['host'], port=value['port'])
        return result
    if kind not in {'temp_directory', 'output'}:
        raise ValueError('资源类型不受支持。')
    if not verify_path:
        _keys(value, {'path', 'device', 'inode'}, {'path', 'device', 'inode'})
        return {k: core._text(value[k], k, 2048) for k in value}
    _keys(value, {'path'}, {'path'})
    raw = core._text(value['path'], 'path', 2048)
    if not os.path.isabs(raw) or '..' in raw.replace('\\', '/').split('/') or raw.startswith(('\\\\', '//')):
        raise ValueError('资源路径必须是无跳转的本机绝对路径。')
    paths = json.loads(task['paths'])
    parent = paths['temp'] if kind == 'temp_directory' else paths['outputs']
    if not config._within(raw, parent) or config._key(raw) == config._key(parent):
        raise ValueError('资源必须位于本任务 Temp 或 Outputs 子目录，不能登记整个目录。')
    path = os.path.abspath(raw)
    config._check_ancestors(path if kind == 'temp_directory' else os.path.dirname(path))
    info = os.lstat(path)
    if config._is_reparse(info) or (not stat.S_ISDIR(info.st_mode) if kind == 'temp_directory' else
                                  not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise ValueError('资源不能是重解析点、硬链接或特殊文件。')
    return {'path': path, 'device': str(info.st_dev), 'inode': str(info.st_ino)}


def _sample(value):
    if value is None:
        return {}
    allowed = {'sampled_at', 'ram_available_bytes', 'vram_available_bytes', 'resource_ram_bytes'}
    _keys(value, allowed, {'sampled_at'})
    result = {'sampled_at': _stamp(value['sampled_at'])}
    for key in set(value) - {'sampled_at'}:
        number = value[key]
        if type(number) not in (int, float) or number < 0 or number > 2 ** 60 or not math.isfinite(number):
            raise ValueError('资源采样必须是有界非负字节数。')
        result[key] = number
    return result


def _eligible(row):
    return bool(row['ownership_declared'] and row['ownership'] == 'task_exclusive'
                and row['temporary'] and row['resource_type'] != 'output')


def _public(row, task):
    result = {k: row[k] for k in row.keys() if k not in {'root', 'lease_hash', 'identity_key'}}
    for key in ('identity', 'evidence', 'sample'):
        result[key] = json.loads(result[key])
    result['temporary'] = bool(result['temporary'])
    result['ownership_declared'] = bool(result['ownership_declared'])
    result['cleanup_eligible'] = _eligible(row)
    result['task_status'] = task['status']
    result['task_title'] = task['title']
    result['project'] = task['project']
    result['title'] = result['label'] or result['resource_type']
    result['last_evidence_at'] = result['evidence'].get('observed_at')
    result['message'] = result['evidence'].get('detail', '')
    result['task_removed'] = bool(task['deleted_at']) if 'deleted_at' in task.keys() else False
    ended = task['status'] != 'active' or task['owner'] != row['client_id'] or task['lease_hash'] != row['lease_hash'] or result['task_removed']
    if result['state'] == 'running' and ended and result['cleanup_eligible']:
        result['state'] = 'cleanup_pending'
    result['evidence_source'] = 'client_report' if result['evidence'] else 'registration_only'
    result['host_verified'] = False
    result['automatic_control'] = False
    return result


def execute(cfg, action, body, actor='ui'):
    if action not in ACTIONS or actor not in {'ui', 'mcp'}:
        raise ValueError('资源操作不受支持。')
    if not isinstance(body, dict):
        raise ValueError('请求须为 JSON 对象。')
    p = dict(body)
    root = core.root_path(cfg)
    expected = p.pop('_workspace_root', None)
    if not isinstance(expected, str) or config._key(expected) != config._key(root):
        raise ValueError('资源操作必须绑定当前工作环境。')
    with _store(cfg, create=action != 'resource_list') as (con, root_key):
        if action == 'resource_list':
            _keys(p, {'task_id', 'client_id', 'offset'})
            offset = p.get('offset', 0)
            if type(offset) is not int or not 0 <= offset <= 5000:
                raise ValueError('资源分页参数不正确。')
            if p.get('task_id'):
                core._get(con, 'tasks', root_key, p['task_id'], include_deleted=True)
            if actor == 'mcp':
                core._get(con, 'clients', root_key, p.get('client_id'))
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_resources'").fetchone():
                return {'available': True, 'items': [], 'total': 0, 'counts': {},
                        'automatic_control': False, 'host_verified': False, 'workspace_root': str(root)}
            sql, args = 'SELECT * FROM task_resources WHERE root=?', [root_key]
            if actor == 'mcp':
                core._get(con, 'clients', root_key, p.get('client_id'))
                sql += ' AND client_id=?'
                args.append(p['client_id'])
            elif p.get('client_id'):
                sql += ' AND client_id=?'
                args.append(core._segment(p['client_id'], 'client_id'))
            if p.get('task_id'):
                core._get(con, 'tasks', root_key, p['task_id'], include_deleted=True)
                sql += ' AND task_id=?'
                args.append(p['task_id'])
            rows = con.execute(sql + ' ORDER BY created_at,id', args).fetchall()
            items = [_public(row, core._get(con, 'tasks', root_key, row['task_id'], include_deleted=True)) for row in rows]
            return {'items': items[offset:offset + core.LIMIT], 'total': len(items),
                    'counts': dict(collections.Counter(item['state'] for item in items)),
                    'available': True, 'automatic_control': False, 'host_verified': False,
                    'workspace_root': str(root)}
        common = {'task_id', 'client_id', 'lease_token'}
        if action == 'resource_register':
            _keys(p, common | {'resource_type', 'identity', 'ownership', 'temporary', 'label', 'sample'},
                  common | {'resource_type', 'identity'})
            task = core._lease(con, root_key, p)
            core._get(con, 'clients', root_key, p['client_id'])
            kind = p['resource_type']
            if not isinstance(kind, str) or kind not in TYPES:
                raise ValueError('资源类型不受支持。')
            ownership = p.get('ownership', 'user_owned')
            if not isinstance(ownership, str) or ownership not in OWNERSHIP:
                raise ValueError('资源归属不受支持。')
            temporary = p.get('temporary', False)
            if type(temporary) is not bool or (kind == 'output' and temporary):
                raise ValueError('临时标记须为布尔值；正式输出不能作为临时清理项。')
            identity = _identity(kind, p['identity'], task)
            key_identity = dict(identity)
            if 'path' in key_identity:
                key_identity['path'] = config._key(key_identity['path'])
            identity_key = hashlib.sha256(_json(key_identity).encode()).hexdigest()
            values = {'id': str(uuid.uuid4()), 'root': root_key, 'task_id': task['id'],
                      'client_id': p['client_id'], 'lease_hash': task['lease_hash'], 'resource_type': kind,
                      'identity': _json(identity), 'identity_key': identity_key, 'ownership': ownership,
                      'ownership_declared': int('ownership' in p), 'temporary': int(temporary),
                      'label': core._text(p.get('label', ''), 'label', 200, False),
                      'state': 'running', 'evidence': '{}', 'sample': _json(_sample(p.get('sample'))),
                      'created_at': core._now(), 'updated_at': core._now()}
            if not _eligible(values):
                values['state'] = 'manual_required'
            existing = con.execute('SELECT * FROM task_resources WHERE root=? AND resource_type=? AND identity_key=?',
                                   (root_key, kind, identity_key)).fetchone()
            if existing:
                compared = {'task_id', 'client_id', 'lease_hash', 'ownership', 'ownership_declared', 'temporary', 'label'}
                if any(existing[k] != values[k] for k in compared):
                    raise ValueError('资源已经登记给其他任务、领取批次或归属；不能覆盖。')
                return {**_public(existing, task), 'deduplicated': True}
            if con.execute('SELECT count(*) FROM task_resources WHERE root=?', (root_key,)).fetchone()[0] >= 5000:
                raise ValueError('任务资源登记达到本工作区上限。')
            con.execute('INSERT INTO task_resources(' + ','.join(values) + ') VALUES(' + ','.join('?' for _ in values) + ')', tuple(values.values()))
            core._audit(con, root_key, action, values['id'], p['client_id'], _json({'task_id': task['id'], 'resource_type': kind}))
            return {**_public(core._get(con, 'task_resources', root_key, values['id']), task), 'deduplicated': False}
        _keys(p, common | {'resource_id', 'state', 'evidence'}, common | {'resource_id', 'state', 'evidence'})
        row = core._get(con, 'task_resources', root_key, p['resource_id'])
        token = p['lease_token']
        if row['task_id'] != p['task_id'] or row['client_id'] != p['client_id'] or not isinstance(token, str) or not secrets.compare_digest(row['lease_hash'], hashlib.sha256(token.encode()).hexdigest()):
            raise ValueError('仅原登记客户端及领取凭据可报告此资源收尾。')
        task = core._get(con, 'tasks', root_key, row['task_id'], include_deleted=True)
        state = p['state']
        if not isinstance(state, str) or state not in STATES - {'running'}:
            raise ValueError('收尾状态不正确。')
        if not _eligible(row) and state not in {'manual_required', 'cleanup_failed'}:
            raise ValueError('共享、用户资源及正式输出不能声明自动收尾。')
        evidence = p['evidence']
        _keys(evidence, {'identity', 'observed_at', 'outcome', 'detail'}, {'identity', 'observed_at', 'outcome'})
        normalized = _identity(row['resource_type'], evidence['identity'], task, verify_path=False)
        if _json(normalized) != row['identity']:
            raise ValueError('收尾证据的 tab/session、PID 启动时间或目录身份与登记不符。')
        if not isinstance(evidence['outcome'], str) or evidence['outcome'] not in {'absent', 'present', 'not_checked'}:
            raise ValueError('收尾证据结果不受支持。')
        if state == 'closed' and evidence['outcome'] != 'absent':
            raise ValueError('没有不存在的观测证据，不能声明已收尾。')
        normalized_evidence = {'identity': normalized, 'observed_at': _stamp(evidence['observed_at']),
                               'outcome': evidence['outcome'],
                               'detail': core._text(evidence.get('detail', ''), 'detail', 1000, False)}
        if normalized_evidence['observed_at'] < row['created_at']:
            raise ValueError('收尾观测不能早于资源登记。')
        encoded = _json(normalized_evidence)
        if row['state'] == 'closed':
            if state != 'closed' or encoded != row['evidence']:
                raise ValueError('已收尾资源不能改写；新的资源应重新登记。')
            return {**_public(row, task), 'deduplicated': True}
        if row['state'] == state and row['evidence'] == encoded:
            return {**_public(row, task), 'deduplicated': True}
        previous = json.loads(row['evidence'])
        if previous and normalized_evidence['observed_at'] <= previous['observed_at']:
            raise ValueError('收尾观测已陈旧，请重新检查。')
        con.execute('UPDATE task_resources SET state=?,evidence=?,updated_at=? WHERE root=? AND id=?',
                    (state, encoded, core._now(), root_key, row['id']))
        core._audit(con, root_key, action, row['id'], p['client_id'], _json({'task_id': row['task_id'], 'state': state, 'evidence_source': 'client_report'}))
        return {**_public(core._get(con, 'task_resources', root_key, row['id']), task), 'deduplicated': False}
