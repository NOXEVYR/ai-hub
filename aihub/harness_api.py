"""Workbench-only harness registration and evidence; never executes external tools."""
import contextlib
import datetime as dt
import os
from pathlib import Path
import re
import sqlite3
import stat

from . import collaboration, config, harnesses

HEARTBEAT_SECONDS = 300


def _read_evidence(cfg):
    """Read existing collaboration evidence without creating any state on discovery."""
    path = Path(config.DATA_DIR) / 'collaboration.sqlite3'
    if not os.path.lexists(path):
        return [], []
    root = config._key(collaboration.root_path(cfg))
    config._check_ancestors(str(path.parent))
    for suffix in ('', '-wal', '-shm', '-journal'):
        candidate = Path(str(path) + suffix)
        if os.path.lexists(candidate):
            info = candidate.lstat()
            if config._is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('协作证据数据库不能是链接或特殊文件。')
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        clients = [dict(r) for r in con.execute('SELECT id,tool,name,last_seen FROM clients WHERE root=?', (root,))] if 'clients' in tables else []
        proofs = [dict(r) for r in con.execute('SELECT client_id,tool,evidence_key,last_success,action FROM harness_invocations WHERE root=?', (root,))] if 'harness_invocations' in tables else []
    return clients, proofs


def list_tools(cfg):
    items = harnesses.list_tools(cfg)
    available = config.workspace_status(cfg).get('available', False) and cfg.get('workspace_managed') is True
    clients, proofs = _read_evidence(cfg) if available else ([], [])
    now = dt.datetime.now(dt.timezone.utc)
    for item in items:
        matching = [c for c in clients if c['tool'] == item['id']]
        recent = []
        for client in matching:
            try:
                age = (now - dt.datetime.fromisoformat(client['last_seen'])).total_seconds()
                if 0 <= age <= HEARTBEAT_SECONDS:
                    recent.append(client)
            except (ValueError, TypeError):
                continue
        active_protocol = item['enabled'] and item['connection_mode'] == 'mcp_stdio'
        valid = [p for p in proofs if p['tool'] == item['id'] and p['evidence_key'] == item.get('evidence_key')]
        proof = max(valid, key=lambda p: p['last_success']) if valid else None
        item.update(client_count=len(matching), recent_heartbeat=bool(recent) and active_protocol,
                    heartbeat_window_seconds=HEARTBEAT_SECONDS,
                    last_seen=max((c['last_seen'] for c in matching), default=None),
                    invocation_verified=bool(proof) and active_protocol,
                    last_invocation_at=proof['last_success'] if proof else None,
                    last_invocation_action=proof['action'] if proof else None,
                    clients=matching,
                    verification_scope='successful_aihub_protocol_call_only')
    root = cfg.get('ai_root') or ''
    return {'items': items, 'templates': [v for v in items if v.get('builtin')],
            'available': available, 'root': root, 'workspace_root': root}


def check(cfg, identifier):
    harnesses.validate_tool_id(identifier)
    item = next((v for v in list_tools(cfg)['items'] if v['id'] == identifier), None)
    if item is None:
        raise ValueError('工作端未登记。')
    return dict(item, check_mode='recorded_evidence_only',
                message='检查现有心跳与成功调用记录；没有启动第三方软件或执行模型。',
                workspace_root=cfg.get('ai_root') or '', root=cfg.get('ai_root') or '')


def _active_tasks(cfg, tool):
    path = Path(config.DATA_DIR) / 'collaboration.sqlite3'
    if not os.path.lexists(path):
        return 0
    with collaboration.store(cfg) as (con, root):
        return con.execute("SELECT count(*) FROM tasks t JOIN clients c ON t.root=c.root AND t.owner=c.id WHERE t.root=? AND t.status='active' AND c.tool=?", (root, tool)).fetchone()[0]


def save(cfg, body):
    with harnesses.mutation_guard():
        if isinstance(body, dict) and (body.get('enabled') is False or body.get('connection_mode') == 'manual'):
            if _active_tasks(cfg, body.get('id')):
                raise harnesses.RevisionConflict('该工作端仍有已领取任务，请先完成、交接或释放后再停用协议接入。')
        record = harnesses.save(cfg, body)
        return check(cfg, record['id'])


def configuration(cfg, identifier, client_id=None):
    from . import collaboration_api
    item = harnesses.get(cfg, identifier)
    if not item or not item['enabled']:
        raise ValueError('工作端未登记或已停用。')
    client_id = client_id or identifier + '-local'
    if not isinstance(client_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', client_id):
        raise ValueError('客户端 ID 须为 1–80 位字母、数字、点、下划线或连字符。')
    if item['connection_mode'] == 'manual':
        return {'config': None, 'command': None, 'args': [], 'tool_id': identifier, 'client_id': client_id,
                'instructions': '该工作端选择手动交接；请在工具中打开项目并读取交接文件。切换为 MCP stdio 后才生成协议配置。',
                'workspace_root': cfg.get('ai_root') or ''}
    value = collaboration_api.integration_config(cfg, identifier, client_id)
    server = value['mcpServers']['aihub']
    return {'config': value, 'command': server['command'], 'args': server['args'], 'tool_id': identifier,
            'client_id': client_id, 'workspace_root': cfg.get('ai_root') or '',
            'instructions': '将此通用 stdio 配置填入支持 MCP 的客户端；原生配置格式由该工具决定。重载连接并调用 aihub_task_list 后，再检查心跳与调用记录。登记路径不会自动写入原生配置或启动软件。'}


def record_invocation(cfg, client, evidence_key, action):
    with collaboration.store(cfg) as (con, root):
        con.execute('INSERT INTO harness_invocations(root,client_id,tool,evidence_key,last_success,action) VALUES(?,?,?,?,?,?) '
                    'ON CONFLICT(root,client_id) DO UPDATE SET tool=excluded.tool,evidence_key=excluded.evidence_key,last_success=excluded.last_success,action=excluded.action',
                    (root, client['id'], client['tool'], evidence_key, collaboration._now(), action))
