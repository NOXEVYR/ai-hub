"""Create project folders from a short-lived preview; tool rules are guidance only."""
import copy
import os
from pathlib import Path
import threading
import time
import unicodedata
import uuid

from . import config, tool_adapters, workspace

TOOLS = {'codex': 'Codex', 'zcode': 'ZCode', 'dsh': 'DSH', 'workbuddy': 'WorkBuddy'}
TOKEN_SECONDS = 600
_lock = threading.RLock()
_previews = {}


def _name(value):
    reserved = {'con', 'prn', 'aux', 'nul', 'clock$', 'conin$', 'conout$'}
    reserved |= {prefix + str(n) for prefix in ('com', 'lpt') for n in range(1, 10)}
    reserved |= {prefix + n for prefix in ('com', 'lpt') for n in '¹²³'}
    if (not isinstance(value, str) or not value or len(value) > 80 or
            value != value.strip() or value.endswith('.') or value in ('.', '..') or
            any(c in '<>:"/\\|?*`' or unicodedata.category(c).startswith('C') for c in value) or
            value.split('.')[0].casefold() in reserved):
        raise ValueError('项目名须为 1–80 字的单个目录名；不能含路径、保留名称、控制字符或末尾句点、空格。')
    return value


def _documents(root, selected):
    display = '、'.join(TOOLS[key] for key in selected)
    rules = f'''# 项目工作规则

项目根目录：`{root}`
选择工具：{display}

- Codex、ZCode、DSH、WorkBuddy 使用同一目录约定。执行前先读本 AGENTS.md、README.md、TASK_BRIEF.md，以及上级工作区规则。
- Inputs 为只读输入，不修改、删除或覆盖原件；模型、训练数据和外部输入同样保持只读。
- 过程脚本、日志和临时文件只写 Work；运行产物写 Outputs；确认后的正式交付写 Deliverables。
- 允许更新项目 README.md 中的记录和链接；其余管理文件由 AI Hub 管理。新增任务须先在 TASK_BRIEF.md 明确目标与验收条件。
- 不在其他磁盘、聊天默认目录或用户桌面散落本项目文件。超出上述范围的操作先核对用户授权；发现冲突立即停止该写入，不覆盖已有文件。
- 完成时在 README.md 记录产物路径、验证结果与交付链接；保留版本和回退依据。

当前接入级别：guidance_only。以上是可交给 AI 阅读的软约束；尚未实际接入这些工具，没有操作系统硬隔离，也没有全盘写入监控。
'''
    brief = f'''# 项目任务交接

请先读取 `{root / 'AGENTS.md'}` 和 `{root / 'README.md'}`。
项目根目录：`{root}`
适用工具：{display}
输入（只读）：`{root / 'Inputs'}`
过程工作目录：`{root / 'Work'}`
运行输出目录：`{root / 'Outputs'}`
正式交付目录：`{root / 'Deliverables'}`

本次目标：[由用户填写，未明确前不要开展产出]
验收条件：[由用户填写]
仅在上述过程、输出、交付目录内写入，并更新 README.md 记录。不要覆盖输入或将成果留在其他位置。
这些目录规则目前为 guidance_only，需要将本任务文本交给所选工具并核对实际工作目录；AI Hub 尚未自动启动、配置或隔离外部工具。
'''
    readme = f'''# {root.name}

[项目规则](AGENTS.md) · [任务交接](TASK_BRIEF.md)

| 目录 | 用途 |
|---|---|
| [Inputs](Inputs/) | 只读输入 |
| [Work](Work/) | 过程、脚本、日志与临时文件 |
| [Outputs](Outputs/) | 运行输出，已登记为 AI Hub 图库来源 |
| [Deliverables](Deliverables/) | 正式交付 |

所选工具：{display}。当前均为 guidance_only，未自动接入或启用系统隔离。

## 任务与验证记录

由执行工具记录本次目标、产物路径、验证结果和正式交付链接。
'''
    native_rules = {}
    handoffs = []
    for key in selected:
        for relative, content in tool_adapters.project_rules(key, root):
            if not isinstance(content, str):
                raise ValueError('工具规则必须是文本。')
            if relative == 'AGENTS.md':
                continue  # The project has one authoritative common AGENTS.md.
            if relative == f'TOOL_HANDOFF_{key}.md':
                handoffs.append(content)
                continue
            if relative != 'CODEBUDDY.md':
                raise ValueError('工具规则包含未支持的原生文件名。')
            if relative in native_rules and native_rules[relative] != content:
                raise ValueError('多个工具的原生规则内容冲突；请重新预览。')
            native_rules[relative] = content
    if handoffs:
        brief += '\n\n## 所选工具接入说明\n\n' + '\n\n'.join(handoffs)
    manifest = {'owner': 'AIHub.projects', 'version': 1, 'root': str(root),
                'name': root.name, 'tools': {key: {'mode': 'guidance_only'} for key in selected},
                'enforcement': 'guidance_only', 'input_read_only': ['Inputs'],
                'write_directories': ['Work', 'Outputs', 'Deliverables'],
                'prompt_path': 'TASK_BRIEF.md', 'native_rules': ['AGENTS.md'] + list(native_rules)}
    return {str(root / name): content.encode('utf-8') for name, content in
            [('README.md', readme), ('AGENTS.md', rules), ('TASK_BRIEF.md', brief)]} | {
            str(root / '.aihub-project.json'): workspace._encoded(manifest)} | {
            str(root / name): content.encode('utf-8') for name, content in native_rules.items()}


def _plan(cfg, body):
    result = {'root': '', 'can_apply': False, 'directories': [], 'files': [],
              'warnings': ['规则为 guidance_only；未启动或接入外部工具，未启用操作系统硬隔离。'], 'errors': []}
    states, documents = {}, {}
    try:
        if not isinstance(body, dict) or set(body) - {'name', 'tools'}:
            raise ValueError('项目请求字段不合法。')
        if cfg.get('workspace_managed') is not True:
            raise ValueError('请先在 AI Hub 中建立或接管工作区。')
        base = Path(config.validate_asset_root(cfg.get('ai_root')))
        name = _name(body.get('name'))
        selected = body.get('tools')
        if (not isinstance(selected, list) or not selected or len(selected) > len(TOOLS) or
                any(not isinstance(key, str) or key not in TOOLS for key in selected) or
                len(set(selected)) != len(selected)):
            raise ValueError('请选择不重复的 Codex、ZCode、DSH、WorkBuddy 工具列表。')
        project = base / '40_Projects' / name
        result.update(root=str(project), tools=list(selected))
        config.validate_asset_root(str(project), must_exist=False)
        output = project / 'Outputs'
        excluded = list(cfg.get('scan_exclude_paths') or []) + [config.APP_DIR, config.DATA_DIR]
        ignored = {str(item).casefold() for item in (cfg.get('ignore_dirs', config.DEFAULT_IGNORE_DIRS) or [])}
        if (config.output_path_excluded(str(output), str(base)) or
                any(part.casefold() in ignored for part in output.parts) or
                any(config._within(str(output), path) for path in excluded if isinstance(path, str) and path)):
            raise ValueError('项目输出目录被当前扫描排除规则排除，请更换项目名称或修复工作区设置。')
        if os.path.lexists(project):
            raise ValueError('项目名称已存在，请填写唯一的新项目名；不会覆盖已有内容。')
        for path in [base, project.parent, project] + [project / n for n in ('Inputs', 'Work', 'Outputs', 'Deliverables')]:
            states[str(path)] = workspace._root_state(path)
            if path != base:
                result['directories'].append({'path': str(path), 'action': 'keep' if path.exists() else 'create'})
        documents = _documents(project, selected)
        result['files'] = [{'path': path, 'action': 'create'} for path in documents]
        outputs = cfg.get('output_roots', [])
        if not isinstance(outputs, list) or any(not isinstance(p, str) or not p for p in outputs):
            raise ValueError('现有图库来源格式无效，请先修复工作区设置。')
    except (OSError, ValueError, TypeError) as exc:
        result['errors'].append(str(exc))
    result['can_apply'] = not result['errors']
    return result, states, documents


def preview(cfg, body):
    with _lock:
        now = time.time()
        for token in list(_previews):
            if _previews[token]['expires_at'] <= now:
                del _previews[token]
        if len(_previews) >= 100:
            raise ValueError('待确认预览过多，请稍后重试。')
        result, states, documents = _plan(cfg, body)
        result.update(token=None, expires_at=now + TOKEN_SECONDS)
        if result['can_apply']:
            token = uuid.uuid4().hex
            _previews[token] = {'expires_at': result['expires_at'], 'revision': workspace._revision(cfg),
                                'config_path': config._key(config.CONFIG_PATH), 'body': copy.deepcopy(body),
                                'states': states, 'documents': documents}
            result['token'] = token
        return result


def _rollback(files, directories):
    for path, expected in reversed(files):
        try:
            if workspace._file_state(path) == expected:
                path.unlink()
        except (OSError, ValueError):
            pass
    for path, expected in reversed(directories):
        try:
            if workspace._root_state(path) == expected:
                path.rmdir()  # Never recurse; retain any new contents from another process.
        except (OSError, ValueError):
            pass


def apply(cfg, token):
    with _lock:
        pending = _previews.get(token) if isinstance(token, str) else None
        if not pending or pending['expires_at'] <= time.time():
            raise ValueError('预览不存在或已过期，请重新预览。')
        if pending['config_path'] != config._key(config.CONFIG_PATH) or pending['revision'] != workspace._revision(cfg):
            raise ValueError('配置已变化，请重新预览。')
        fresh, states, documents = _plan(cfg, pending['body'])
        if not fresh['can_apply'] or states != pending['states'] or documents != pending['documents']:
            raise ValueError('项目目录或工作区已变化，请重新预览；未写入。')
        data_dir = Path(config.CONFIG_PATH).parent
        config._check_ancestors(str(data_dir))
        data_dir.mkdir(parents=True, exist_ok=True)
        lock_path = data_dir / '.workspace-apply.lock'
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ValueError('另一个工作区保存正在进行，请稍后重新预览。') from None
        try:
            lock_info = os.fstat(fd)
            lock_state = {'exists': True, 'identity': [lock_info.st_dev, lock_info.st_ino],
                          'size': lock_info.st_size, 'mtime_ns': str(lock_info.st_mtime_ns),
                          'sha256': workspace._digest(b'')}
        finally:
            os.close(fd)
        created_files, created_dirs = [], []
        try:
            if pending['revision'] != workspace._revision(cfg):
                raise ValueError('配置在保存前已变化，请重新预览。')
            backup_dir = data_dir / 'workspace-backups'
            config._check_ancestors(str(backup_dir))
            backup_dir.mkdir(exist_ok=True)
            backup = backup_dir / ('project-config-' + uuid.uuid4().hex + '.json')
            config_file = Path(config.CONFIG_PATH)
            prior = config_file.read_bytes() if config_file.exists() else workspace._encoded(cfg)
            if config_file.exists():
                config._read_config(str(config_file))
            workspace._exclusive_file(backup, prior)
            for row in fresh['directories']:
                path = Path(row['path'])
                config._check_ancestors(str(path))
                if row['action'] == 'create':
                    path.mkdir()
                    created_dirs.append((path, workspace._root_state(path)))
                elif workspace._root_state(path) != states[str(path)]:
                    raise ValueError('项目上级目录身份已变化，停止创建。')
            for raw_path, content in documents.items():
                path = Path(raw_path)
                config._check_ancestors(str(path.parent))
                with open(path, 'xb', buffering=0) as stream:
                    try:
                        remaining = memoryview(content)
                        while remaining:
                            written = stream.write(remaining)
                            if not written:
                                raise OSError('项目文件写入未完成。')
                            remaining = remaining[written:]
                        stream.flush()
                        os.fsync(stream.fileno())
                    finally:
                        # Track a partial file too, but only if the path still names our handle.
                        opened = os.fstat(stream.fileno())
                        actual = workspace._file_state(path)
                        if actual.get('identity') == [opened.st_dev, opened.st_ino]:
                            created_files.append((path, actual))
            for path, expected in created_dirs:
                if workspace._root_state(path) != expected:
                    raise ValueError('新目录身份在保存期间变化，配置未保存。')
            for path, expected in created_files:
                if workspace._file_state(path) != expected:
                    raise ValueError('项目规则在保存期间变化，配置未保存。')
            workspace_root = config.validate_asset_root(cfg['ai_root'])
            if workspace._root_state(workspace_root) != states[workspace_root] or pending['revision'] != workspace._revision(cfg):
                raise ValueError('工作区或配置在保存期间变化，未覆盖。')
            output = str(Path(fresh['root']) / 'Outputs')
            config.validate_asset_root(output)
            if config.scan_excluded(output, cfg):
                raise ValueError('项目输出目录当前不可被扫描，配置未保存。')
            updated = copy.deepcopy(cfg)
            added = config._key(output) not in {config._key(p) for p in updated.get('output_roots', [])}
            if added:
                updated['output_roots'] = list(updated.get('output_roots', [])) + [output]
            config.save_config(updated)
            cfg.clear()
            cfg.update(updated)
            del _previews[token]
        except Exception:
            _rollback(created_files, created_dirs)
            raise
        finally:
            try:
                if workspace._file_state(lock_path) == lock_state:
                    lock_path.unlink()
            except (OSError, ValueError):
                # Do not delete a replacement or mask the original apply failure.
                pass
        return {'applied': True, 'root': fresh['root'], 'prompt_path': str(Path(fresh['root']) / 'TASK_BRIEF.md'),
                'tools': fresh['tools'], 'output_root_added': added, 'config_backup': str(backup),
                'enforcement': 'guidance_only'}
