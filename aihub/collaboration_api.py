"""One local API facade shared by the workbench and restricted MCP bridge."""
from pathlib import Path
import copy
import sys

from . import collaboration, collaboration_maintenance as maintenance, collaboration_resources as resources, config, harnesses

MAINTENANCE_ACTIONS = {'source_list', 'source_candidates', 'source_inventory', 'source_add',
                       'source_scan', 'source_reconfirm_preview', 'source_reconfirm_apply',
                       'retention_preview', 'retention_policy', 'retention_run'}
CAPABILITY_ACTIONS = {'capability_publish', 'capability_list', 'capability_recommend', 'capability_dispatch'}
MCP_ACTIONS = {'client_heartbeat', 'task_create', 'task_list', 'task_claim', 'task_finish',
               'task_handoff', 'artifact_write', 'artifact_register', 'artifact_list', 'report_submit',
               'memory_propose', 'memory_search', 'retention_preview', 'source_list', 'submission_schema', 'submission_receipt'} | CAPABILITY_ACTIONS | resources.ACTIONS


def integration_config(cfg, tool='codex', client_id=None):
    executable = Path(sys.executable)
    if executable.name.casefold() == 'pythonw.exe' and executable.with_name('python.exe').exists():
        executable = executable.with_name('python.exe')
    return {'mcpServers': {'aihub': {'command': str(executable),
            'args': [str(Path(config.APP_DIR) / 'tools/aihub_mcp.py'), '--install-root',
                     str(Path(config.APP_DIR).resolve()), '--tool', tool,
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
        result['resources'] = resource_status(cfg)
        all_sources = maintenance.list_sources(cfg, include_deleted=True)['items']
        removed_sources = [source for source in all_sources if source.get('deleted_at')]
        result.setdefault('deleted_records', {})['sources'] = removed_sources[:collaboration.LIMIT]
        result.setdefault('record_counts', {})['sources'] = {'active': len(all_sources) - len(removed_sources), 'deleted': len(removed_sources)}
        result.update(policy=maintenance.policy(cfg), sources=maintenance.list_sources(cfg)['items'],
                      inventory=maintenance.inventory(cfg), source_candidates=maintenance.source_candidates(cfg)['items'])
    return result


def resource_status(cfg, body=None, actor='ui'):
    import sqlite3
    try:
        return resources.execute(cfg, 'resource_list', {**(body or {}), '_workspace_root': cfg.get('ai_root', '')}, actor)
    except (ValueError, OSError, sqlite3.Error) as error:
        return {'available': False, 'items': [], 'counts': {}, 'error': str(error),
                'automatic_control': False, 'host_verified': False}


INTEROP_ACTIONS = frozenset({'interop_describe', 'interop_capability_snapshot'})


def execute(cfg, action, body, actor='ui', *, public_identity=None):
    with harnesses.mutation_guard():
        if action in INTEROP_ACTIONS:
            from . import interop
            if actor not in ('ui', 'mcp'):
                raise PermissionError('未知接入方式。')
            if not isinstance(body, dict):
                raise ValueError('请求须为 JSON 对象。')
            # This new public read path never initializes the collaboration
            # store or records heartbeat/invocation evidence. client_id is only
            # the bridge's transport field, not authentication of the caller.
            payload = {key: value for key, value in body.items() if key != 'client_id'}
            return interop.execute(copy.deepcopy(cfg), action, payload,
                                   public_identity=public_identity)
        return _execute(cfg, action, body, actor)


def _execute(cfg, action, body, actor='ui'):
    cfg = copy.deepcopy(cfg)
    if actor not in ('ui', 'mcp'):
        raise PermissionError('未知接入方式。')
    if not isinstance(body, dict):
        raise ValueError('请求须为 JSON 对象。')
    body = dict(body)
    expected_root = body.pop('_workspace_root', None)
    if action in {'source_reconfirm_preview', 'source_reconfirm_apply', 'record_delete_preview', 'record_delete_apply', 'record_restore'} | resources.ACTIONS and expected_root is None:
        raise ValueError('此操作必须绑定当前工作环境，请刷新页面。')
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
    elif action in resources.ACTIONS:
        result = resources.execute(cfg, action, dict(body, _workspace_root=expected_root), actor)
    else:
        effective = dict(cfg)
        effective['collaboration_retention_days'] = maintenance.policy(cfg)['days']
        if action in {'record_delete_preview', 'record_delete_apply', 'record_restore'}:
            body['_workspace_root'] = expected_root
        result = collaboration.execute(effective, action, body, actor=actor)
        if action in {'task_finish', 'task_handoff'}:
            result['resource_cleanup'] = resource_status(cfg, {key: body[key] for key in ('task_id', 'client_id') if key in body}, actor)
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
