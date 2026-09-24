"""One local API facade shared by the workbench and restricted MCP bridge."""
from pathlib import Path
import copy
import sys

from . import collaboration, collaboration_maintenance as maintenance, config

MAINTENANCE_ACTIONS = {'source_list', 'source_candidates', 'source_inventory', 'source_add',
                       'source_scan', 'retention_preview', 'retention_policy', 'retention_run'}
MCP_ACTIONS = {'client_heartbeat', 'task_create', 'task_list', 'task_claim', 'task_finish',
               'task_handoff', 'artifact_write', 'artifact_register', 'artifact_list',
               'memory_propose', 'memory_search', 'retention_preview', 'source_list'}


def integration_config(cfg, tool='codex'):
    executable = Path(sys.executable)
    if executable.name.casefold() == 'pythonw.exe' and executable.with_name('python.exe').exists():
        executable = executable.with_name('python.exe')
    return {'mcpServers': {'aihub': {'command': str(executable),
            'args': [str(Path(config.APP_DIR) / 'tools/aihub_mcp.py'), '--port',
                     str(cfg.get('server', {}).get('port', 8765)), '--tool', tool,
                     '--client-id', tool + '-local']}}}


def status(cfg):
    cfg = copy.deepcopy(cfg)
    result = collaboration.status(cfg)
    result.update(mcp_config=integration_config(cfg),
                  integrations=[{'tool': tool, 'mode': 'manual_mcp', 'verified': False,
                    'mcp_config': integration_config(cfg, tool),
                    'instructions': '将此 stdio 配置加入支持 MCP 的工具。实际接入以客户端心跳为准；现有会话不会自动切换目录。'}
                    for tool in ('codex', 'zcode', 'workbuddy', 'dsh')])
    if result.get('available'):
        result.update(policy=maintenance.policy(cfg), sources=maintenance.list_sources(cfg)['items'],
                      inventory=maintenance.inventory(cfg), source_candidates=maintenance.source_candidates(cfg)['items'])
    return result


def execute(cfg, action, body, actor='ui'):
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
    if action in MAINTENANCE_ACTIONS:
        return maintenance.execute(cfg, action, body, actor)
    effective = dict(cfg)
    effective['collaboration_retention_days'] = maintenance.policy(cfg)['days']
    return collaboration.execute(effective, action, body, actor=actor)
