"""Workspace-scoped harness metadata; registration never launches or configures tools."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading

from . import config

BUILTIN_IDS = ('codex', 'zcode', 'dsh', 'workbuddy')
MAX_TOOLS = 128
MAX_PATH_DIRS = 64
MAX_DISCOVERY_CHECKS = 1024
_LOCK = threading.RLock()
_FIELDS = {'id', 'name', 'executable', 'work_dir', 'config_path', 'notes',
           'connection_mode', 'enabled', 'revision'}
_SECRETS = re.compile(r'(?:Bearer\s+\S{12,}|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|https?://[^\s/@]+:[^\s/@]+@|[?&](?:api_key|token|secret)=)', re.I)
_SCHEMA = '''CREATE TABLE IF NOT EXISTS harnesses(
    root TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
    metadata TEXT NOT NULL, PRIMARY KEY(root,id))'''


class RevisionConflict(ValueError):
    """The caller must reload and review a newer registration before saving."""


@contextlib.contextmanager
def mutation_guard():
    """Coordinate registry changes and root-owned task admission in this process."""
    with _LOCK:
        yield


def validate_tool_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', value) or value == 'any':
        raise ValueError('工作端 ID 须以小写字母开头，仅含小写字母、数字、下划线或连字符，最长 64 字符；any 为保留值。')
    if value in {'con', 'prn', 'aux', 'nul'} | {'com%d' % n for n in range(1, 10)} | {'lpt%d' % n for n in range(1, 10)}:
        raise ValueError('工作端 ID 不能使用 Windows 保留名称。')
    return value


def _text(value, field, limit, optional=False):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(field + ' 格式或长度不正确，不能包含控制字符。')
    value = value.strip()
    if not optional and not value:
        raise ValueError(field + ' 不能为空。')
    if _SECRETS.search(value):
        raise ValueError('工作端登记不得包含凭据或带凭据的地址。')
    return value


def _workspace(cfg, required=False):
    if cfg.get('workspace_managed') is not True or not cfg.get('ai_root'):
        if required:
            raise ValueError('请先建立或接入托管工作环境，再登记工作端。')
        return None
    return config.validate_asset_root(cfg['ai_root'])


def _check_database():
    data = Path(config.DATA_DIR)
    config._check_ancestors(str(data))
    path = data / 'harnesses.sqlite3'
    for suffix in ('', '-wal', '-shm', '-journal'):
        candidate = Path(str(path) + suffix)
        if os.path.lexists(candidate):
            info = candidate.lstat()
            if config._is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('工作端登记数据库不能使用链接或特殊文件。')
    return path


@contextlib.contextmanager
def _write_store(cfg):
    root = _workspace(cfg, required=True)
    with _LOCK:
        path = _check_database()
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(_SCHEMA)
            yield connection, config._key(root)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _stored(cfg):
    root = _workspace(cfg)
    if root is None:
        return {}
    with _LOCK:
        path = _check_database()
        if not path.exists():
            return {}
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)) as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='harnesses'").fetchone():
                return {}
            return {identifier: dict(json.loads(metadata), revision=revision)
                    for identifier, revision, metadata in connection.execute(
                        'SELECT id,revision,metadata FROM harnesses WHERE root=? ORDER BY id', (config._key(root),))}


def _legacy_usage(cfg):
    """Read actual pre-registry use, never manufacture registrations or create a DB."""
    root = _workspace(cfg)
    if root is None:
        return set()
    path = Path(config.DATA_DIR) / 'collaboration.sqlite3'
    if not os.path.lexists(path):
        return set()
    config._check_ancestors(str(path.parent))
    for suffix in ('', '-wal', '-shm', '-journal'):
        candidate = Path(str(path) + suffix)
        if os.path.lexists(candidate):
            info = candidate.lstat()
            if config._is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('历史协作数据库不能是链接或特殊文件。')
    used = set()
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column in (('clients', 'tool'), ('tasks', 'target_tool')):
            if table not in tables:
                continue
            columns = {row[1] for row in connection.execute('PRAGMA table_info(' + table + ')')}
            if not {'root', column} <= columns:
                continue
            used.update(row[0] for row in connection.execute(
                'SELECT DISTINCT ' + column + ' FROM ' + table + ' WHERE root=?', (config._key(root),))
                if row[0] in BUILTIN_IDS)
    return used


def _registrations(cfg):
    from . import tool_adapters
    legacy = {identifier: dict(_defaults(identifier, tool_adapters._TOOLS[identifier][0]),
                              revision=0, registration_origin='legacy_usage')
              for identifier in _legacy_usage(cfg)}
    legacy.update({identifier: dict(metadata, registration_origin='explicit')
                   for identifier, metadata in _stored(cfg).items()})
    return legacy


def templates(cfg=None):
    """Optional recipes carry no installation, registration or connection claims."""
    from . import tool_adapters
    return tool_adapters.templates(cfg)


def _path_evidence(value):
    """Metadata only: no file contents, executable version probes or native config reads."""
    if not value:
        return {'state': 'not_configured'}
    path = Path(value)
    try:
        cursor = path
        while True:
            if os.path.lexists(cursor) and config._is_reparse(cursor.lstat()):
                return {'state': 'link'}
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
            return {'state': 'special'}
        return {'state': 'directory' if stat.S_ISDIR(info.st_mode) else 'file',
                'device': info.st_dev, 'inode': info.st_ino, 'size': info.st_size,
                'mtime_ns': info.st_mtime_ns}
    except FileNotFoundError:
        return {'state': 'missing'}
    except OSError:
        return {'state': 'inaccessible'}


def _path(value, field):
    value = _text(value, field, 4096, True)
    if not value:
        return ''
    if not os.path.isabs(value) or value.startswith(('\\\\', '//')) or (os.name == 'nt' and ':' in value[2:]):
        raise ValueError(field + ' 须为本机绝对路径，不接受网络路径、地址或命令。')
    if any(char in value for char in '\r\n\0') or (os.name == 'nt' and any(char in value for char in '*?<>|"')):
        raise ValueError(field + ' 路径包含不支持的字符。')
    value = os.path.abspath(value)
    state = _path_evidence(value)['state']
    if state in {'link', 'special'}:
        raise ValueError(field + ' 不能指向链接或特殊文件。')
    if field == 'executable' and state == 'directory':
        raise ValueError('程序入口须为文件路径。')
    if field == 'work_dir' and state == 'file':
        raise ValueError('工作目录不能指向普通文件。')
    return value


def _defaults(identifier, name):
    return {'id': identifier, 'name': name, 'executable': '', 'work_dir': '', 'config_path': '',
            'notes': '', 'connection_mode': 'mcp_stdio' if identifier in BUILTIN_IDS else 'manual',
            'enabled': True}


def _present(cfg, identifier, metadata, builtin=None):
    origin = metadata.get('registration_origin', 'explicit') if metadata else 'legacy_usage'
    configured = origin == 'explicit'
    source = dict(metadata or _defaults(identifier, builtin['name']))
    registered = source.get('executable', '')
    executable = registered or ((builtin or {}).get('executable') or '')
    evidence = {key: _path_evidence(executable if key == 'executable' else source.get(key, ''))
                for key in ('executable', 'work_dir', 'config_path')}
    detected = evidence['executable']['state'] == 'file'
    notes = list((builtin or {}).get('notes', []))
    if source.get('notes'):
        notes.append(source['notes'])
    if not builtin:
        notes.extend(['用户登记的工作端；项目接入采用 AI Hub 专属交接文件，不推断其原生规则支持。',
                      '入口存在、已登记与协议连接是不同状态；不会启动程序或修改原生配置。'])
    root = _workspace(cfg)
    root_info = _path_evidence(root) if root else {'state': 'not_configured'}
    # Directory identity, not changing directory mtime, invalidates connection evidence.
    root_identity = {key: root_info.get(key) for key in ('state', 'device', 'inode')}
    revision = source.get('revision', 0)
    stamp = {'root': config._key(root) if root else None, 'root_identity': root_identity,
             'id': identifier, 'revision': revision, 'enabled': source['enabled'],
             'connection_mode': source['connection_mode'], 'paths': evidence}
    return {**(builtin or {}), 'id': identifier, 'name': source['name'],
            'builtin': identifier in BUILTIN_IDS, 'template': False, 'enabled': source['enabled'],
            'configured': configured, 'registered': True, 'registration_origin': origin, 'revision': revision, 'connection_mode': source['connection_mode'],
            'executable': executable or None, 'registered_executable': registered,
            'work_dir': source.get('work_dir', ''), 'config_path': source.get('config_path', ''),
            'user_notes': source.get('notes', ''), 'notes': notes,
            'detected': detected, 'available': detected and source['enabled'],
            'launch_mode': (builtin or {}).get('launch_mode', 'manual_handoff'),
            'rules_support': (builtin or {}).get('rules_support', 'AIHub handoff only'),
            'enforcement': 'soft_rules_only', 'path_status': {key: item['state'] for key, item in evidence.items()},
            'evidence_key': hashlib.sha256(json.dumps(stamp, sort_keys=True).encode('utf-8')).hexdigest()}


def list_tools(cfg):
    recipes = {item['id']: item for item in templates(cfg)}
    registrations = _registrations(cfg)
    return [_present(cfg, identifier, registrations[identifier], recipes.get(identifier))
            for identifier in sorted(registrations)]


def allowed_ids(cfg, include_disabled=False):
    return {identifier for identifier, record in _registrations(cfg).items()
            if include_disabled or record['enabled']}


def get(cfg, identifier):
    validate_tool_id(identifier)
    for item in list_tools(cfg):
        if item['id'] == identifier:
            return item
    raise ValueError('工作端尚未登记，请先在工作端管理中添加。')


def save(cfg, body):
    if not isinstance(body, dict) or set(body) - _FIELDS:
        raise ValueError('仅可保存工作端元数据；不接受命令、参数、环境变量、凭据或未知字段。')
    identifier = validate_tool_id(body.get('id'))
    revision = body.get('revision')
    if type(revision) is not int or revision < 0:
        raise ValueError('保存须带当前 revision；新增登记使用 0。')
    recipe = next((item for item in templates(cfg) if item['id'] == identifier), None)
    builtin_name = recipe['name'] if recipe else None
    with _write_store(cfg) as (connection, root):
        previous = connection.execute('SELECT revision,metadata FROM harnesses WHERE root=? AND id=?', (root, identifier)).fetchone()
        expected = previous['revision'] if previous else 0
        if revision != expected:
            raise RevisionConflict('工作端登记已被修改，请刷新并核对后再保存。')
        metadata = json.loads(previous['metadata']) if previous else _defaults(identifier, builtin_name or '')
        metadata.update({key: value for key, value in body.items() if key != 'revision'})
        metadata['name'] = _text(metadata.get('name'), '名称', 120)
        metadata['notes'] = _text(metadata.get('notes'), '备注', 2000, True)
        for key in ('executable', 'work_dir', 'config_path'):
            metadata[key] = _path(metadata.get(key), key)
        if metadata.get('connection_mode') not in {'mcp_stdio', 'manual'}:
            raise ValueError('接入方式只能为 mcp_stdio 或 manual。')
        if type(metadata.get('enabled')) is not bool:
            raise ValueError('启用状态须为布尔值。')
        if previous is None:
            existing = {row[0] for row in connection.execute('SELECT id FROM harnesses WHERE root=?', (root,))}
            existing.update(_legacy_usage(cfg))
            if identifier not in existing and len(existing) >= MAX_TOOLS:
                raise ValueError('每个工作环境最多登记 128 个工作端（含实际使用过的兼容登记）。')
        connection.execute('INSERT INTO harnesses(root,id,revision,metadata) VALUES(?,?,?,?) '
                           'ON CONFLICT(root,id) DO UPDATE SET revision=excluded.revision,metadata=excluded.metadata',
                           (root, identifier, expected + 1, json.dumps(metadata, ensure_ascii=False)))
    return _present(cfg, identifier, dict(metadata, revision=expected + 1, registration_origin='explicit'), recipe)


def discover(cfg):
    """Attach registry metadata to independent, read-only installation evidence."""
    from . import harness_discovery
    result = harness_discovery.discover(cfg, max_checks=MAX_DISCOVERY_CHECKS, max_path_dirs=MAX_PATH_DIRS)
    registered = allowed_ids(cfg, include_disabled=True)
    return dict(result, items=[dict(item, registered=(item.get('suggested_id') or item.get('id')) in registered)
                               for item in result.get('items', [])])
