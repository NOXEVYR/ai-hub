"""Read-only local tool discovery and proposed project rules; never launch tools.

Detection is executable metadata, not a running session or sandbox health check.
No user configuration, task history, credentials or registry values are read.
"""
import os
from pathlib import Path
import shutil


_TOOLS = {
    'codex': ('Codex', 'AGENTS.md',
              '已核对版本的 CLI 提供 --cd、workspace-write 与审批参数；当前安装版本仍须确认。'),
    'zcode': ('ZCode', 'AGENTS.md',
              '已核对 GLM 引擎的 AGENTS.md 规则约定；当前安装版本仍须确认，GUI 项目参数与系统隔离未验证。'),
    'dsh': ('DeepSeek Harness', 'AGENTS.md',
            '已核对源码版本的项目规则与会话固定工作目录约定；当前安装版本仍须确认，沙箱实际生效状态未验证。'),
    'workbuddy': ('WorkBuddy', 'CODEBUDDY.md',
                  '已核对引擎的 CODEBUDDY.md 规则约定；当前安装版本仍须确认，沙箱组件及实际隔离边界未验证。'),
}


def _candidates(tool_id, cfg):
    """Small fixed candidate set; no disk walk or external config content reads."""
    home = Path.home()
    root = Path(cfg['ai_root']) if cfg.get('ai_root') else None
    appdata = Path(os.environ.get('APPDATA') or home / 'AppData/Roaming')
    local = Path(os.environ.get('LOCALAPPDATA') or home / 'AppData/Local')
    known = {
        'codex': [appdata / 'npm/codex.cmd', appdata / 'npm/codex.ps1'],
        'zcode': [local / 'Programs/ZCode/ZCode.exe'],
        'dsh': [appdata / 'npm/dsh.cmd', appdata / 'npm/dsh.ps1'],
        'workbuddy': [local / 'Programs/WorkBuddy/WorkBuddy.exe'],
    }
    if root is not None and root.is_absolute():
        portable = Path(root.anchor) / 'tool'
        known['zcode'].extend([root / '10_Apps/ZCode/ZCode.exe', portable / 'ZCode/ZCode.exe'])
        known['workbuddy'].extend([root / '10_Apps/WorkBuddy/WorkBuddy.exe',
                                  portable / 'WorkBuddy/WorkBuddy.exe'])
        known['dsh'].append(root / '10_Apps/DeepSeek_Harness_Launcher/Start-DSH-Fast.ps1')
    # PATH is only an additional executable lookup; it is never executed here.
    bounded_path = os.pathsep.join(os.environ.get('PATH', '').split(os.pathsep)[:64])
    found = shutil.which({'dsh': 'dsh', 'codex': 'codex',
                          'zcode': 'ZCode', 'workbuddy': 'WorkBuddy'}[tool_id], path=bounded_path)
    return ([Path(found)] if found else []) + known[tool_id]


def _builtin_status(cfg):
    """Return tool metadata. available means an entry exists, not controlled launch."""
    result = []
    for tool_id, (name, rule, note) in _TOOLS.items():
        executable = None
        for candidate in _candidates(tool_id, cfg):
            try:
                if candidate.is_file():
                    executable = str(candidate)
                    break
            except OSError:
                continue
        detected = executable is not None
        result.append({
            'id': tool_id, 'name': name, 'detected': detected, 'available': detected,
            'launch_mode': 'manual_cli' if tool_id == 'codex' else 'manual_project',
            'rules_support': rule,
            'enforcement': 'soft_rules_only',
            'executable': executable,
            'notes': [note, '程序入口存在不代表正在运行、已载入项目规则或已启用沙箱。',
                      'AI Hub 不会自动启动工具、修改全局配置或隔离既有会话。'
                      if detected else '未找到已知入口；可手动选择项目，安装位置尚未验证。'],
        })
    return result


def status(cfg):
    from . import harnesses
    return harnesses.list_tools(cfg)


def _ps_literal(value):
    # Single-quoted PowerShell literals do not expand $, backticks or subexpressions.
    return "'" + str(value).replace("'", "''") + "'"


def project_rules(tool_id, project_root, cfg=None):
    """Propose native rule + tool handoff files; the caller owns conflict handling.

    The AGENTS.md content is identical for the three readers, allowing callers to
    deduplicate it. Never write these over existing project rules.
    """
    custom = None
    if cfg is not None:
        from . import harnesses
        registered = harnesses.get(cfg, tool_id)
        if not registered['enabled']:
            raise ValueError('工作端已停用，请先启用或选择其他工作端。')
    if tool_id not in _TOOLS:
        from . import harnesses
        if cfg is None:
            raise ValueError('Unknown tool adapter')
        custom = registered
    root = os.fspath(project_root)
    if not root or any(ord(char) < 32 for char in root):
        raise ValueError('Project path must not contain control characters')
    if not Path(root).is_absolute():
        raise ValueError('Project path must be absolute')
    name, rule, _note = _TOOLS[tool_id] if custom is None else (custom['name'], None, '')
    common = (
        '# AI Hub 项目工作规则\n\n'
        '- 开始任务先读本目录 TASK_BRIEF.md 与现有项目说明；先确认工作目录就是本项目。\n'
        '- Inputs 是输入资料，默认只读；过程文件放 Work，生成结果放 Outputs，最终交付放 Deliverables。\n'
        '- 新增文件只能放到本项目上述目录；不要在桌面、下载目录、应用源码、模型库或工具默认工作区存放项目产物。\n'
        '- 使用项目外资产只读引用；修改外部路径、删除原件、联网下载或修改全局配置前遵循用户明确授权。\n'
        '- 任务结束在 README.md 的交付记录中列出相对路径和实际验证，不把未验证结果写成通过。\n'
        '- 本文件是规则约束；AI Hub 未启用系统硬隔离或全盘写入监控，规则本身不能阻止越界写入。\n'
    )
    handoff = f'# {name} 项目接入\n\n项目目录：`{root}`\n\n'
    if custom is not None:
        handoff += ('本文件是 AI Hub 专属交接说明，不代表工具原生支持 AGENTS.md 或已自动加载规则。\n\n'
                    '请在工具中手动打开本项目，并明确交付目录、授权范围和需读取的任务说明；核对实际工作目录。'
                    '登记程序路径或 MCP 模式不会启动工具、修改其配置或建立系统隔离。\n\n' + common)
        return [(f'AIHUB_HANDOFF_{tool_id}.md', handoff)]
    if tool_id == 'codex':
        command = f"& 'codex.cmd' --cd {_ps_literal(root)} --sandbox workspace-write --ask-for-approval on-request"
        handoff += (
            '在已安装 Codex CLI 的 PowerShell 中复制执行以下命令，另起 CLI 会话：\n\n'
            f'```powershell\n{command}\n```\n\n'
            '该命令不会改变现有桌面任务或其它已启动会话；启动后核实实际工作目录、规则加载与沙箱状态。'
            'workspace-write 仍可能包含工具临时目录，审批可允许额外操作；需要严格边界时另行验证受管策略。'
            '不要使用绕过审批和沙箱的参数，也不要把整个资产根目录添加为可写范围。\n'
        )
    else:
        handoff += (
            f'在工具中手动建立新项目或新会话，将工作目录明确设为上面的项目目录，确认已加载 `{rule}`。'
            '既有会话的工作目录不会随 AI Hub 自动改变。\n\n'
            '本版本没有验证该工具的 GUI 启动参数或当前会话权限；不提供自动启动命令。'
            '保持正常审批，不使用 yolo、bypass 或全访问模式。\n'
        )
    return [(rule, common), (f'TOOL_HANDOFF_{tool_id}.md', handoff)]
