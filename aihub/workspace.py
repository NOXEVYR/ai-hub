"""Preview-bound workspace setup. Rules are guidance, never OS isolation."""
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time
import uuid

from . import config

VERSION = 1
TOKEN_SECONDS = 600
_lock = threading.RLock()
_previews = {}
_discovery_cache = {}
ENFORCEMENT = {'mode': 'soft', 'description': '规则约束与来源健康检查；未启用操作系统硬隔离，也不监控其他 AI 的全部文件写入。'}
MANAGED = '00_Management/AIHub'


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _file_state(path):
    path = Path(path)
    config._check_ancestors(str(path.parent))
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {'exists': False}
    if config._is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise ValueError('文件位置被联接或非普通文件占用：' + str(path))
    if info.st_size > 4 * 1024 * 1024:
        raise ValueError('已有管理文件超过 4 MiB，保留并请人工检查：' + str(path))
    return {'exists': True, 'identity': [info.st_dev, info.st_ino], 'size': info.st_size,
            'mtime_ns': str(info.st_mtime_ns), 'sha256': _digest(path.read_bytes())}


def _revision(cfg):
    return _digest(_encoded({'live': cfg, 'file': _file_state(config.CONFIG_PATH)}))


def _root_state(path):
    config._check_ancestors(str(path))
    if not Path(path).exists():
        return {'exists': False}
    info = Path(path).stat()
    return {'exists': True, 'identity': [info.st_dev, info.st_ino]}


def _source(root, path, kind, cfg, future=()):
    result = {'kind': kind, 'path': path, 'status': 'ok', 'reason': '普通目录，可作为来源。'}
    try:
        # This validates the lexical path first, including reparse ancestors.
        validated = config.validate_asset_root(path, must_exist=False)
        if not config._within(validated, root):
            result.update(status='outside', reason='来源必须位于本次 AI 根目录内；不自动接管外部路径。')
            return result
        if kind == 'output' and (config.output_path_excluded(validated, root) or
                config._key(validated) in {config._key(root), config._key(Path(root) / '50_Training'), config._key(Path(root) / '50_Training/Projects')}):
            raise ValueError('请选择具体出图目录；数据集、缓存、全部训练分区和 AI 根目录不能作为图库来源。')
        if not os.path.isdir(validated):
            if config._key(validated) in {config._key(p) for p in future}:
                result['reason'] = '将创建此标准目录后作为来源。'
            else:
                result.update(status='missing', reason='来源目录不存在；请选择已有目录或本轮创建的标准目录。')
                return result
        elif config.scan_excluded(validated, cfg):
            raise ValueError('该目录被当前扫描排除规则略过，不能作为有效来源。')
    except (OSError, TypeError, ValueError) as error:
        result.update(status='rejected', reason=str(error))
        if isinstance(path, str) and os.path.isabs(path) and not path.startswith(('\\\\', '//')):
            canonical = os.path.realpath(path)
            if config._key(canonical) != config._key(path):
                result.update(status='junction', reason='旧联接来源会被扫描器跳过；请明确选择规范实体路径后重新预览。',
                              canonical_path=canonical, canonical_supported=False)
                try:
                    config.validate_asset_root(canonical)
                    result['canonical_supported'] = config._within(canonical, root) and not config.scan_excluded(canonical, cfg) and (kind != 'output' or not config.output_path_excluded(canonical, root))
                except (OSError, ValueError):
                    pass
    return result


def _sources(cfg):
    return {key: list(cfg.get(key) or []) for key in ('scan_roots', 'output_roots')}


def _health(root, sources, cfg, future=()):
    return [_source(root, p, kind, cfg, future) for key, kind in (('scan_roots', 'scan'), ('output_roots', 'output')) for p in sources[key]]


def _discover(root, cfg):
    key = (str(root), tuple(cfg.get('ignore_dirs') or []), tuple(cfg.get('scan_exclude_paths') or []))
    now = time.monotonic()
    cached = _discovery_cache.get(key)
    if cached and now - cached[0] < 20:
        return copy.deepcopy(cached[1])
    discovered = config.discover_output_roots([root], cfg.get('ignore_dirs'))
    discovered['items'] = [row for row in discovered['items'] if not config.scan_excluded(row['path'], cfg)]
    if len(_discovery_cache) > 20:
        _discovery_cache.clear()
    _discovery_cache[key] = (now, copy.deepcopy(discovered))
    return discovered


def status(cfg, discover=True):
    basic = config.workspace_status(cfg)
    root = cfg.get('ai_root') or ''
    sources = _sources(cfg)
    result = {**basic, 'root': root, 'managed': cfg.get('workspace_managed') is True, 'revision': _revision(cfg), 'enforcement': dict(ENFORCEMENT),
              'sources': sources, 'source_health': _health(root, sources, cfg) if root else [],
              'rules': {'path': str(Path(root) / MANAGED / 'WORKSPACE.md') if root else '',
                        'manifest_path': str(Path(root) / MANAGED / 'workspace.json') if root else '',
                        'agents_preserved': bool(root and os.path.lexists(Path(root) / 'AGENTS.md'))},
              'discovery': {'items': [], 'examined': 0, 'truncated': False}}
    if discover and basic['available']:
        result['discovery'] = _discover(root, cfg)
    return result


def _documents(root):
    base = Path(root)
    folder = base / MANAGED
    rules = f'''# AI Hub 工作区规则

适用根目录：`{root}`。这些规则是 AI 可读取的软约束与人工审计依据，不是操作系统硬隔离，也不代表已开启全盘写入监控。

- 开始任务先读最近的 AGENTS.md、项目 README 和本文件。现有用户规则保持有效；更近的项目约定优先。
- 软件与运行环境进 10_Apps；模型进 20_Models；素材进 30_Assets；创作项目进 40_Projects；训练项目进 50_Training/Projects。
- 工作流进 60_Workflows；通用工具出图落地区为 70_Output；可复用知识进 80_Knowledge；归档进 90_Archive；管理记录进 00_Management。
- 每个任务必须先明确所属项目、输入、过程、输出、正式交付目录；无项目时先请用户指定，不在任意磁盘位置产生正式交付。
- 新建创作项目统一使用 40_Projects/<项目>/Inputs、Work、Outputs、Deliverables，项目运行输出写入其 Outputs；已存在的项目布局优先，记录映射，不强制改名。训练样张留在 50_Training/Projects/<项目>/Runs/<版本>/verify_out 或 samples，与 Datasets 分开；通过登记具体来源纳入图库，不搬动原图。
- 临时过程留在项目 Work；最终交付只写项目约定的 Deliverables，并从项目 README 链接。不要把下载、缓存、试验图混进交付目录。
- 模型与数据集保持只读；旧联接与硬链接是兼容入口，不是独立备份，不以重复名称为由删除或合并。
- 写入根目录外、移动/删除既有资产、修改权限、安装环境或下载大文件之前，遵循用户已有授权与项目规则；不得自行扩大任务范围。
- 不输出凭据、私密配置或登录数据。发现路径冲突或无权限时停止该写入，记录原因。

AI Hub 仅管理明确配置的来源；扫描应由用户另行启动。未登记的外部工具仍可能写入其他位置，需要工具自身设置与人工检查。
'''
    template = '''# 项目启动任务模板

项目名称：
项目绝对根目录：
最近的 AGENTS.md：
输入目录（只读）：<项目>/Inputs
过程目录：<项目>/Work
运行输出目录：<项目>/Outputs
正式交付目录：<项目>/Deliverables
项目 README：<项目>/README.md
允许写入范围：仅以上明确列出的过程、输出、交付与 README。
本次目标与验收条件：

执行前核对以上目录；现有项目允许按其既有结构填写映射，不移动或覆盖原件。
新项目需先在 40_Projects/<唯一项目名>（训练用 50_Training/Projects/<唯一项目名>）明确创建目录并放置项目 AGENTS.md。
项目 AGENTS.md 应记录上述允许写入范围、命名与版本、验证步骤、交付位置和禁止覆盖的输入；规则是软约束，不声称系统已阻止越界写入。
结束时在 README 记录本次文件清单、验证结果和交付链接；不要将结果留在无关聊天工作目录或根目录。
'''
    agents = '# AI 工作区入口\n\n先阅读 [AI Hub 工作区规则](00_Management/AIHub/WORKSPACE.md) 与最近的项目 AGENTS.md。\n正式文件仅放在已约定的项目目录；这些规则是软约束，不是系统硬隔离。\n'
    manifest = {'owner': 'AIHub.workspace', 'version': VERSION, 'root': str(base),
                'enforcement': 'soft_rules_and_source_health', 'rules': MANAGED + '/WORKSPACE.md',
                'project_template': MANAGED + '/Templates/PROJECT_TASK.md', 'standard_dirs': list(config.STANDARD_DIRS)}
    return [(folder / 'WORKSPACE.md', 'rules', rules.encode('utf-8')),
            (folder / 'workspace.json', 'manifest', _encoded(manifest)),
            (folder / 'Templates/PROJECT_TASK.md', 'template', template.encode('utf-8')),
            (base / 'AGENTS.md', 'agents', agents.encode('utf-8'))]


def _plan(cfg, body):
    if not isinstance(body, dict) or set(body) - {'mode', 'root', 'scan_roots', 'output_roots'}:
        raise ValueError('工作区请求字段不合法。')
    mode = body.get('mode')
    if mode not in ('create', 'connect'):
        raise ValueError('请选择 create 或 connect。')
    raw_root = body.get('root')
    result = {'mode': mode, 'root': raw_root, 'can_apply': False, 'directories': [], 'files': [],
              'sources': {}, 'source_health': [], 'warnings': [ENFORCEMENT['description']], 'errors': []}
    try:
        root = config.validate_asset_root(raw_root, must_exist=mode == 'connect')
    except (OSError, TypeError, ValueError) as error:
        result['errors'].append(str(error))
        if isinstance(raw_root, str) and os.path.isabs(raw_root):
            diagnostic = _source(raw_root, raw_root, 'scan', cfg)
            if diagnostic['status'] == 'junction':
                result['source_health'].append(diagnostic)
        return result, {}, {}
    result['root'] = root
    if mode == 'create' and os.path.exists(root):
        result['errors'].append('新建根目录已经存在，请使用接管已有目录模式。')
    directories = [Path(root)] + [Path(root).joinpath(*name.split('/')) for name in config.STANDARD_DIRS]
    directories += [Path(root) / MANAGED, Path(root) / MANAGED / 'Templates']
    states, documents = {}, {}
    for path in directories:
        try:
            states[str(path)] = _root_state(path)
            result['directories'].append({'path': str(path), 'action': 'keep' if path.exists() else 'create'})
        except (OSError, ValueError) as error:
            result['errors'].append(str(error))
    for path, kind, content in _documents(root):
        try:
            current = _file_state(path)
            states[str(path)] = current
            action = 'create'
            if current['exists']:
                action = 'keep' if kind == 'agents' or current['sha256'] == _digest(content) else 'conflict'
            result['files'].append({'path': str(path), 'kind': kind, 'action': action})
            if action == 'conflict':
                result['errors'].append('已有管理文件内容不同，保留原文件，不会覆盖：' + str(path))
            elif action == 'create':
                documents[str(path)] = content
            elif kind == 'agents':
                result['warnings'].append('已有 AGENTS.md 保留；Hub 专用规则单独生成，不重写用户约定。')
        except (OSError, ValueError) as error:
            result['files'].append({'path': str(path), 'kind': kind, 'action': 'conflict'})
            result['errors'].append(str(error))
    same = bool(cfg.get('ai_root')) and config._key(cfg['ai_root']) == config._key(root)
    defaults = {'scan_roots': (cfg.get('scan_roots') if same and 'scan_roots' in cfg else [root]),
                'output_roots': (cfg.get('output_roots') if same and 'output_roots' in cfg else [str(Path(root) / '70_Output')])}
    for key in defaults:
        values = body.get(key, defaults[key])
        if not isinstance(values, list) or len(values) > 100 or any(not isinstance(p, str) or not p for p in values):
            result['errors'].append(key + ' 必须是最多 100 个绝对目录组成的数组。')
            values = []
        # Keep invalid originals in the preview so nothing disappears silently.
        result['sources'][key] = list(dict.fromkeys(values))
        if not values:
            result['warnings'].append('未选择' + ('扫描来源；不会索引资产。' if key == 'scan_roots' else '图库来源；不会索引图片。'))
    result['source_health'] = _health(root, result['sources'], cfg, directories)
    result['errors'] += [r['reason'] + ' ' + str(r['path']) for r in result['source_health'] if r['status'] != 'ok']
    for row in result['source_health']:
        if row['status'] == 'ok':
            states[row['path']] = _root_state(row['path'])
    if same:
        result['warnings'].append('保留现有目录、文件、个人数据和整理开关；不会执行扫描或资产移动。')
    else:
        result['warnings'].append('切换根目录不迁移旧资产或登记；旧根目录绑定的登记需要单独核对。自动整理保持关闭。')
    result['warnings'].append('保留原索引和个人数据，应用后需重新扫描；不是多个数据库独立隔离的工作区。')
    result['can_apply'] = not result['errors']
    return result, states, documents


def preview(cfg, body):
    with _lock:
        now = time.time()
        for token in list(_previews):
            if _previews[token]['expires_at'] <= now:
                del _previews[token]
        if len(_previews) >= 100:
            raise ValueError('待确认预览过多，请稍后再试。')
        revision = _revision(cfg)
        result, states, documents = _plan(cfg, body)
        result.update(token=None, revision=revision, expires_at=now + TOKEN_SECONDS)
        if result['can_apply']:
            token = uuid.uuid4().hex
            result['token'] = token
            _previews[token] = {'version': VERSION, 'expires_at': result['expires_at'], 'revision': revision,
                                'config_path': config._key(config.CONFIG_PATH), 'body': copy.deepcopy(body),
                                'result': copy.deepcopy(result), 'states': states, 'documents': documents}
        return result


def _exclusive_file(path, content):
    config._check_ancestors(str(Path(path).parent))
    with open(path, 'xb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return _file_state(path)


def apply(cfg, token):
    with _lock:
        pending = _previews.get(token) if isinstance(token, str) else None
        if not pending or pending['expires_at'] <= time.time() or pending['version'] != VERSION:
            raise ValueError('预览不存在或已过期，请重新预览。')
        if pending['config_path'] != config._key(config.CONFIG_PATH) or pending['revision'] != _revision(cfg):
            raise ValueError('配置已被其他操作修改，请重新预览。')
        fresh, states, documents = _plan(cfg, pending['body'])
        if not fresh['can_apply'] or states != pending['states'] or documents != pending['documents'] or fresh['sources'] != pending['result']['sources']:
            raise ValueError('目录、规则或来源已变化，请重新预览；未写入。')
        data_dir = Path(config.CONFIG_PATH).parent
        config._check_ancestors(str(data_dir))
        data_dir.mkdir(parents=True, exist_ok=True)
        lock_path = data_dir / '.workspace-apply.lock'
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ValueError('另一个工作区保存正在进行；若进程已退出，请先检查保留的锁文件。') from None
        try:
            lock_info = os.fstat(handle)
            lock_state = {'exists': True, 'identity': [lock_info.st_dev, lock_info.st_ino],
                          'size': lock_info.st_size, 'mtime_ns': str(lock_info.st_mtime_ns),
                          'sha256': _digest(b'')}
        finally:
            os.close(handle)
        created_files, created_dirs = [], []
        try:
            if pending['revision'] != _revision(cfg):
                raise ValueError('配置在保存前已变化，请重新预览。')
            backup_dir = data_dir / 'workspace-backups'
            config._check_ancestors(str(backup_dir))
            backup_dir.mkdir(exist_ok=True)
            backup = backup_dir / ('config-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.json')
            existing = Path(config.CONFIG_PATH)
            prior = existing.read_bytes() if existing.exists() else _encoded(cfg)
            if existing.exists():
                config._read_config(str(existing))
            _exclusive_file(backup, prior)
            for row in fresh['directories']:
                if row['action'] == 'create':
                    target = Path(row['path'])
                    missing = []
                    cursor = target
                    while not cursor.exists():
                        missing.append(cursor)
                        cursor = cursor.parent
                    for path in reversed(missing):
                        config._check_ancestors(str(path))
                        path.mkdir()
                        created_dirs.append((path, _root_state(path)))
            for path, content in documents.items():
                saved_state = _exclusive_file(path, content)
                created_files.append((Path(path), saved_state))
            # Recheck all retained files and all source boundaries before config commit.
            for path, expected in states.items():
                if expected['exists']:
                    actual = _file_state(path) if 'sha256' in expected else _root_state(path)
                    if actual != expected:
                        raise ValueError('已有文件或目录在保存期间改变，配置未保存。')
            invalid = [r for r in _health(fresh['root'], fresh['sources'], cfg) if r['status'] != 'ok']
            if invalid:
                raise ValueError('来源在保存期间失效，配置未保存。')
            if pending['revision'] != _revision(cfg):
                raise ValueError('配置在保存期间改变，未覆盖。')
            updated = copy.deepcopy(cfg)
            same = bool(cfg.get('ai_root')) and config._key(cfg['ai_root']) == config._key(fresh['root'])
            updated.update(ai_root=fresh['root'], workspace_managed=True, **fresh['sources'], catalog_dir=str(Path(fresh['root']) / '00_Management/Catalogs'))
            if not same:
                updated.update(aliases={}, organizer={'enabled': False, 'root': '', 'on_startup': False})
            excludes = list(updated.get('scan_exclude_paths') or [])
            for path in (config.APP_DIR, str(Path(fresh['root']) / '00_AIHub_Library')):
                if path not in excludes:
                    excludes.append(path)
            updated['scan_exclude_paths'] = excludes
            config.save_config(updated)
            cfg.clear()
            cfg.update(updated)
            del _previews[token]
        except Exception:
            # Only undo our unchanged new files/empty directories, never existing assets.
            for path, expected in reversed(created_files):
                try:
                    if _file_state(path) == expected:
                        path.unlink()
                except (OSError, ValueError):
                    pass
            for path, expected in reversed(created_dirs):
                try:
                    if _root_state(path) == expected:
                        path.rmdir()
                except (OSError, ValueError):
                    pass
            raise
        finally:
            try:
                if _file_state(lock_path) == lock_state:
                    lock_path.unlink()
            except (OSError, ValueError):
                # Cleanup must neither remove a replacement nor mask the apply error.
                pass
        return {'applied': True, 'root': fresh['root'], 'created_paths': [str(p) for p, _ in created_dirs] + [str(p) for p, _ in created_files],
                'config_backup': str(backup), 'scan_required': True, 'status': status(cfg, discover=False)}
