"""One local API facade shared by the workbench and restricted MCP bridge."""
from pathlib import Path
import copy
import sys

from . import collaboration, collaboration_maintenance as maintenance, config, harnesses

MAINTENANCE_ACTIONS = {'source_list', 'source_candidates', 'source_inventory', 'source_add',
                       'source_scan', 'retention_preview', 'retention_policy', 'retention_run'}
CAPABILITY_ACTIONS = {'capability_publish', 'capability_list', 'capability_recommend', 'capability_dispatch'}
MCP_ACTIONS = {'client_heartbeat', 'task_create', 'task_list', 'task_claim', 'task_finish',
               'task_handoff', 'artifact_write', 'artifact_register', 'artifact_list',
               'memory_propose', 'memory_search', 'retention_preview', 'source_list'} | CAPABILITY_ACTIONS


def integration_config(cfg, tool='codex', client_id=None):
    executable = Path(sys.executable)
    if executable.name.casefold() == 'pythonw.exe' and executable.with_name('python.exe').exists():
        executable = executable.with_name('python.exe')
    return {'mcpServers': {'aihub': {'command': str(executable),
            'args': [str(Path(config.APP_DIR) / 'tools/aihub_mcp.py'), '--port',
                     str(cfg.get('server', {}).get('port', 8765)), '--tool', tool,
                     '--client-id', client_id or tool + '-local']}}}


def status(cfg):
    from . import harness_api
    cfg = copy.deepcopy(cfg)
    result = collaboration.status(cfg)
    tools = harness_api.list_tools(cfg)['items']
    connected = [item for item in tools if item['enabled'] and item['connection_mode'] == 'mcp_stdio']
    result.update(mcp_config=integration_config(cfg, connected[0]['id']) if connected else None, tools=tools,
                  integrations=[{'tool': item['id'], 'mode': 'manual_mcp', 'verified': False,
                    'mcp_config': integration_config(cfg, item['id']),
                    'instructions': '将此 stdio 配置加入支持 MCP 的工具。实际接入以客户端心跳为准；现有会话不会自动切换目录。'}
                    for item in tools if item['enabled'] and item['connection_mode'] == 'mcp_stdio'])
    if result.get('available'):
        result.update(policy=maintenance.policy(cfg), sources=maintenance.list_sources(cfg)['items'],
                      inventory=maintenance.inventory(cfg), source_candidates=maintenance.source_candidates(cfg)['items'])
    return result


def execute(cfg, action, body, actor='ui'):
    with harnesses.mutation_guard():
        return _execute(cfg, action, body, actor)


def _execute(cfg, action, body, actor='ui'):
    cfg = copy.deepcopy(cfg)
    if actor not in ('ui', 'mcp'):
        raise PermissionError('未知接入方式。')
    if not isinstance(body, dict):
        raise ValueError('请求须为 JSON 对象。')
    body = dict(body)
    expected_root = body.pop('_workspace_root', None)
    if expected_root is not None and (not isinstance(expected_root, str) or
            config._key(expected_root) != config._key(cfg.get('ai_root') or '')):
        raise ValueError('工作环境已切换，请刷新协作页面后重新操作。')
    if actor == 'mcp' and action not in MCP_ACTIONS:
        raise PermissionError('MCP 接口不允许审核长期记忆、改变来源或执行清理。')
    client, evidence_key = None, None
    if actor == 'mcp' and action != 'client_heartbeat':
        with collaboration.store(cfg) as (con, root):
            client = dict(collaboration._get(con, 'clients', root, body.get('client_id')))
        tool = harnesses.get(cfg, client['tool'])
        if not tool or not tool['enabled'] or tool['connection_mode'] != 'mcp_stdio':
            raise PermissionError('工作端未登记、已停用或仅启用手动交接。')
        evidence_key = tool['evidence_key']
    if action in CAPABILITY_ACTIONS:
        from . import capabilities
        result = capabilities.execute(cfg, action, body, actor)
    elif action in MAINTENANCE_ACTIONS:
        result = maintenance.execute(cfg, action, body, actor)
    else:
        effective = dict(cfg)
        effective['collaboration_retention_days'] = maintenance.policy(cfg)['days']
        result = collaboration.execute(effective, action, body, actor=actor)
    if client is not None:
        from . import harness_api
        import sqlite3
        try:
            harness_api.record_invocation(cfg, client, evidence_key, action)
        except (OSError, ValueError, sqlite3.Error):
            # Business effects have already committed: missing status evidence must
            # not turn success into an ambiguous failure that encourages retries.
            result = dict(result, invocation_evidence_recorded=False)
    return {**result, 'workspace_root': cfg.get('ai_root', '')}
