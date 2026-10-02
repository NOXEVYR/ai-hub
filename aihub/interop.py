"""Read-only local interop observations; never exports credentials or executes work."""
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from . import capabilities, collaboration, config, harnesses, service_control

PROTOCOL = 'aihub-interop/1'
MAX_DECLARATION_BYTES = 32768
MAX_RESPONSE_BYTES = 131072
HEARTBEAT_SECONDS = 300
READ_TIMEOUT_SECONDS = 0.25
_HEX64 = re.compile(r'[0-9a-f]{64}')
_HEX32 = re.compile(r'[0-9a-f]{32}')
_IDENTITY_FIELDS = {'app', 'service_instance_id', 'install_root', 'control_protocol', 'port', 'server_version'}


class InteropError(ValueError):
    def __init__(self, code, message, http_status):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _bounded(result):
    try:
        size = len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    except (ValueError, TypeError, UnicodeError):
        raise InteropError('invalid_snapshot', '快照数据格式不受支持。', 422) from None
    if size > MAX_RESPONSE_BYTES:
        raise InteropError('response_too_large', '完整快照超过响应大小上限，未截断或导出。', 413)
    return result


def _identity(public_identity):
    """This argument is Handler-owned; request fields are never a fallback."""
    unavailable = {'status': 'identity_unavailable'}
    if not isinstance(public_identity, dict) or not _IDENTITY_FIELDS <= set(public_identity):
        return unavailable
    value = {key: public_identity[key] for key in _IDENTITY_FIELDS}
    try:
        for key, maximum in (('app', 32), ('service_instance_id', 128), ('install_root', 4096), ('server_version', 128)):
            value[key] = capabilities._text(value[key], key, maximum)
        if value['app'] != 'ai-hub' or not os.path.isabs(value['install_root']) or value['install_root'].startswith(('\\\\', '//')):
            return unavailable
        if type(value['port']) is not int or not 1 <= value['port'] <= 65535:
            return unavailable
        if value['control_protocol'] != service_control.PROTOCOL:
            return unavailable
    except (ValueError, TypeError):
        return unavailable
    return dict(value, status='available')


def _workspace(cfg):
    """Only configured root metadata, without config-file or environment reads."""
    unavailable = {'status': 'workspace_unavailable', 'binding_revision': None}
    root = cfg.get('ai_root')
    if cfg.get('workspace_managed') is not True or not isinstance(root, str) or not root:
        return unavailable
    try:
        if not os.path.isabs(root) or root.startswith(('\\\\', '//')) or any(ord(char) < 32 for char in root):
            return unavailable
        if os.name == 'nt' and (':' in root[2:] or any(char in root for char in '*?<>|"')):
            return unavailable
        root = os.path.abspath(root)
        if os.path.dirname(root) == root:
            return unavailable
        config._check_ancestors(root)
        info = os.lstat(root)
        if config._is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            return unavailable
    except (ValueError, OSError):
        return unavailable
    return {'status': 'available', 'root': root,
            'binding_revision': _digest({'scope': 'local_workspace_directory', 'root': config._key(root),
                                         'device': str(info.st_dev), 'inode': str(info.st_ino)}),
            'revision_scope': 'local_path_and_directory_identity', 'full_configuration_revision': False,
            'cross_machine_uuid': False}


def _connection(identity, workspace):
    if identity['status'] != 'available' or workspace['status'] != 'available':
        return None
    return _digest({'protocol': PROTOCOL, 'app': identity['app'],
                    'service_instance_id': identity['service_instance_id'],
                    'install_root': config._key(identity['install_root']), 'port': identity['port'],
                    'control_protocol': identity['control_protocol'], 'server_version': identity['server_version'],
                    'workspace_binding_revision': workspace['binding_revision']})


def _expected_workspace(cfg, body):
    if '_workspace_root' not in body:
        return
    expected, current = body['_workspace_root'], cfg.get('ai_root') or ''
    if not isinstance(expected, str) or not isinstance(current, str):
        raise InteropError('workspace_changed', '工作区已切换，请重新读取联动描述。', 409)
    if expected == current == '':
        return
    if not expected or not current or not os.path.isabs(expected) or config._key(expected) != config._key(current):
        raise InteropError('workspace_changed', '工作区已切换，请重新读取联动描述。', 409)


def _check_storage(path):
    """Check lexical ancestors and every SQLite sidecar before and after reads."""
    try:
        config._check_ancestors(str(path.parent))
        for suffix in ('', '-wal', '-shm', '-journal'):
            candidate = Path(str(path) + suffix)
            if os.path.lexists(candidate):
                info = candidate.lstat()
                if config._is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise InteropError('unsafe_storage', '联动登记存储使用链接或特殊文件，未读取。', 409)
        if os.path.lexists(Path(str(path) + '-wal')) and not os.path.lexists(Path(str(path) + '-shm')):
            raise InteropError('storage_unavailable', '联动登记 WAL 状态不可安全只读，请稍后重试。', 503)
        return path.lstat() if os.path.lexists(path) else None
    except InteropError:
        raise
    except (ValueError, OSError):
        raise InteropError('unsafe_storage', '联动登记路径不可安全读取。', 409) from None


@contextlib.contextmanager
def _readonly(name, required):
    path = Path(config.DATA_DIR) / name
    before = _check_storage(path)
    if before is None:
        yield None
        return
    try:
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=READ_TIMEOUT_SECONDS)) as con:
            con.row_factory = sqlite3.Row
            deadline = time.monotonic() + READ_TIMEOUT_SECONDS
            con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            con.execute('PRAGMA query_only=ON')
            after = _check_storage(path)
            if after is None or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise InteropError('storage_changed', '联动登记文件身份已改变，请重新读取。', 409)
            for table, columns in required.items():
                definition = con.execute('SELECT type,substr(sql,1,128) AS sql FROM sqlite_master WHERE name=? LIMIT 1', (table,)).fetchone()
                if (definition is None or definition['type'] != 'table' or not isinstance(definition['sql'], str)
                        or 'VIRTUAL TABLE' in definition['sql'].upper()):
                    raise InteropError('storage_schema_unavailable', '联动登记结构不可读取，未进行迁移。', 503)
                found = {row[1] for row in con.execute('PRAGMA table_info(' + table + ')')}
                if not columns <= found:
                    raise InteropError('storage_schema_unavailable', '联动登记结构不可读取，未进行迁移。', 503)
            yield con
            after = _check_storage(path)
            if after is None or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise InteropError('storage_changed', '联动登记文件身份已改变，请重新读取。', 409)
    except (sqlite3.Error, OSError):
        raise InteropError('storage_unavailable', '联动登记存储暂时不可只读。', 503) from None


def _read_one(name, table, columns, sql, args):
    with _readonly(name, {table: columns}) as con:
        if con is None:
            return None, False
        row = con.execute(sql, args).fetchone()
        return dict(row) if row else None, True


def _harness_and_heartbeat(root_key, client_id, tool):
    row, exists = _read_one('harnesses.sqlite3', 'harnesses', {'root', 'id', 'revision', 'metadata'},
        '''SELECT CASE WHEN typeof(revision)='integer' THEN revision ELSE NULL END AS revision,
        length(CAST(metadata AS BLOB)) AS bytes,
        CASE WHEN length(CAST(metadata AS BLOB))<=? THEN metadata ELSE NULL END AS metadata
        FROM harnesses WHERE root=? AND id=? LIMIT 1''',
        (MAX_DECLARATION_BYTES, root_key, tool))
    harness = {'status': 'registration_unavailable' if not exists else 'not_explicitly_registered',
               'id': tool, 'revision': None, 'enabled': None, 'connection_mode': None, 'origin': None}
    if row:
        try:
            if row['bytes'] > MAX_DECLARATION_BYTES or type(row['revision']) is not int or row['revision'] < 1:
                raise ValueError()
            metadata = json.loads(row['metadata'], object_pairs_hook=capabilities._pairs)
            if not isinstance(metadata, dict) or metadata.get('id') != tool:
                raise ValueError()
            enabled, mode = metadata.get('enabled'), metadata.get('connection_mode')
            if type(enabled) is not bool or mode not in ('mcp_stdio', 'manual'):
                raise ValueError()
        except (ValueError, TypeError, RecursionError):
            raise InteropError('invalid_harness_metadata', '工作端登记元数据无效，未导出。', 422) from None
        harness.update(status='registered', revision=row['revision'], enabled=enabled,
                       connection_mode=mode, origin='explicit')
    client, clients_exist = _read_one('collaboration.sqlite3', 'clients', {'root', 'id', 'tool', 'last_seen'},
        '''SELECT CASE WHEN length(CAST(tool AS BLOB))<=80 THEN tool ELSE NULL END AS tool,
        CASE WHEN length(CAST(last_seen AS BLOB))<=80 THEN last_seen ELSE NULL END AS last_seen
        FROM clients WHERE root=? AND id=? LIMIT 1''', (root_key, client_id))
    heartbeat = {'status': 'client_unavailable' if not clients_exist else 'not_observed',
                 'recent': False, 'last_seen': None, 'window_seconds': HEARTBEAT_SECONDS,
                 'source_client_id': client_id}
    if client:
        if client['tool'] is None or client['last_seen'] is None:
            raise InteropError('invalid_client_metadata', '声明来源客户端元数据无效或超过上限，未导出。', 422)
        if client['tool'] != tool:
            raise InteropError('source_conflict', '声明来源与客户端工作端登记不一致，请重新核对。', 409)
        try:
            stamp = client['last_seen']
            if not isinstance(stamp, str) or len(stamp) > 80:
                raise ValueError()
            moment = dt.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            if moment.tzinfo is None:
                raise ValueError()
            age = (dt.datetime.now(dt.timezone.utc) - moment).total_seconds()
            recent = 0 <= age <= HEARTBEAT_SECONDS
            heartbeat.update(status='recent_heartbeat' if recent else 'not_recently_seen', recent=recent, last_seen=stamp)
        except (ValueError, TypeError, OverflowError):
            heartbeat['status'] = 'invalid_timestamp'
        if not row and tool in harnesses.BUILTIN_IDS:
            # Actual client usage supports the existing compatibility origin,
            # but no explicit revision or current enabled setting is invented.
            harness['origin'] = 'legacy_usage'
    return harness, heartbeat


def _snapshot(cfg, body, identity, workspace, connection):
    if identity['status'] != 'available':
        raise InteropError('identity_unavailable', '可信服务实例身份不可用，不能导出绑定快照。', 503)
    if workspace['status'] != 'available':
        raise InteropError('workspace_unavailable', '当前没有可用托管工作区。', 409)
    if body['connection_revision'] != connection:
        raise InteropError('connection_changed', '服务实例或工作区绑定已改变，请重新读取联动描述。', 409)
    root_key = config._key(workspace['root'])
    row, exists = _read_one('capabilities.sqlite3', 'capabilities',
        {'root', 'client_id', 'key', 'id', 'tool', 'payload', 'updated_at'},
        '''SELECT CASE WHEN length(CAST(client_id AS BLOB))<=480 THEN client_id ELSE NULL END AS client_id,
        CASE WHEN length(CAST(key AS BLOB))<=100 THEN key ELSE NULL END AS key,id,
        CASE WHEN length(CAST(tool AS BLOB))<=80 THEN tool ELSE NULL END AS tool,
        CASE WHEN length(CAST(updated_at AS BLOB))<=80 THEN updated_at ELSE NULL END AS updated_at,
        length(CAST(payload AS BLOB)) AS bytes,
        CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload ELSE NULL END AS payload
        FROM capabilities WHERE root=? AND id=? LIMIT 1''',
        (MAX_DECLARATION_BYTES, root_key, body['capability_id']))
    if not exists:
        raise InteropError('capability_catalog_missing', '当前工作区尚无能力登记库。', 404)
    if row is None:
        raise InteropError('capability_not_found', '当前工作区没有对应的已登记能力。', 404)
    if type(row['bytes']) is not int or row['bytes'] > MAX_DECLARATION_BYTES:
        raise InteropError('declaration_too_large', '完整能力声明超过 32768 字节，未截断或导出。', 413)
    try:
        text = row['payload']
        if not isinstance(text, str):
            raise ValueError()
        raw = text.encode('utf-8')
        parsed = capabilities._json(text, MAX_DECLARATION_BYTES)
        validated = capabilities._capability(parsed)
        if harnesses._SECRETS.search(json.dumps(parsed, ensure_ascii=False)):
            raise ValueError()
        capabilities._text(row['client_id'], 'client_id', 120)
        collaboration._segment(row['client_id'], 'client_id')
        harnesses.validate_tool_id(row['tool'])
        if validated['key'] != row['key']:
            raise ValueError()
        identifier = hashlib.sha256(json.dumps([root_key, row['client_id'], row['key']], ensure_ascii=False).encode()).hexdigest()[:32]
        if identifier != row['id']:
            raise ValueError()
        updated = capabilities._text(row['updated_at'], 'updated_at', 80)
        if any(harnesses._SECRETS.search(value) for value in
               (row['client_id'], row['key'], row['tool'], updated)):
            raise ValueError()
        if dt.datetime.fromisoformat(updated.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise InteropError('invalid_declaration', '已存能力声明不符合安全分类或输入契约，未导出。', 422) from None
    if len(raw) != row['bytes'] or len(raw) > MAX_DECLARATION_BYTES:
        raise InteropError('invalid_declaration', '已存能力声明的 UTF-8 数据不一致，未导出。', 422)
    declaration_hash = hashlib.sha256(raw).hexdigest()
    if body.get('expected_declaration_sha256') is not None and body['expected_declaration_sha256'] != declaration_hash:
        raise InteropError('declaration_changed', '能力声明已重发或改变，旧摘要不匹配；原导出对象应由调用方保留。', 409)
    harness, heartbeat = _harness_and_heartbeat(root_key, row['client_id'], row['tool'])
    result = {'protocol': PROTOCOL, 'schema_version': 1, 'workspace_root': workspace['root'],
              'identity': identity, 'workspace': workspace, 'connection_revision': connection,
              'snapshot_id': _digest({'namespace': PROTOCOL, 'workspace_binding': workspace['binding_revision'],
                                     'capability_id': row['id'], 'declaration_sha256': declaration_hash}),
              'selected': {'id': row['id'], 'key': row['key'], 'client_id': row['client_id'],
                           'target_tool': row['tool'], 'updated_at': updated},
              'declaration': {'origin': 'stored_normalized_declaration', 'encoding': 'utf-8',
                              'text': text, 'bytes': len(raw), 'sha256': declaration_hash, 'parsed': parsed},
              'harness': harness,
              'evidence': {'declaration': {'status': 'declared', 'original_publisher_bytes_preserved': False},
                           'heartbeat': heartbeat,
                           'actual_invocation': {'status': 'unverified', 'scope': 'native_capability_execution', 'observed': False}},
              'execution': {'mode': 'harness_queue', 'worker_required': True, 'direct_execution': False,
                            'routing': 'target_tool_not_declared_client', 'dispatch_supports_declaration_cas': False},
              'limitations': ['这是已存归一化声明，不是发布者原始输入字节。',
                              '调用方须冻结已导出对象；服务器不保存声明旧版本。',
                              '快照不授予执行权限或保证随后派单使用同一版本；已排队任务有各自请求快照。',
                              '输入采用曜核受限验证规则，未知字段仍拒绝，不能当成任意 JSON Schema 执行。']}
    if _connection(identity, _workspace(cfg)) != connection:
        raise InteropError('connection_changed', '读取期间工作区身份已改变，请重新读取联动描述。', 409)
    return _bounded(result)


def execute(cfg, action, body=None, *, public_identity=None):
    if not isinstance(cfg, dict) or action not in ('interop_describe', 'interop_capability_snapshot'):
        raise InteropError('unsupported_operation', '不支持此联动操作。', 400)
    body = {} if body is None else body
    allowed = {'_workspace_root'} if action == 'interop_describe' else {'_workspace_root', 'capability_id', 'connection_revision', 'expected_declaration_sha256'}
    if not isinstance(body, dict) or set(body) - allowed:
        raise InteropError('invalid_request', '联动请求含不支持字段；服务身份只能由可信 Handler 提供。', 400)
    _expected_workspace(cfg, body)
    identity, workspace = _identity(public_identity), _workspace(cfg)
    connection = _connection(identity, workspace)
    if action == 'interop_describe':
        return _bounded({'protocol': PROTOCOL, 'schema_version': 1, 'identity': identity, 'workspace': workspace,
            'workspace_root': workspace.get('root', ''), 'connection_revision': connection,
            'operations': {'read_only': ['interop_describe', 'interop_capability_snapshot', 'task_list', 'artifact_list', 'submission_receipt'],
                           'queue_and_claim': ['capability_dispatch', 'task_claim', 'report_submit', 'task_finish', 'task_handoff'],
                           'execution_mode': 'harness_queue', 'worker_required': True, 'direct_execution': False,
                           'native_cancel': False, 'cloud_upload': False, 'dispatch_supports_declaration_cas': False,
                           'routing': 'target_tool_not_declared_client', 'completion_requires_current_claim': True},
            'limits': {'declaration_bytes': MAX_DECLARATION_BYTES, 'response_bytes': MAX_RESPONSE_BYTES,
                       'report_bytes': collaboration.MAX_REPORT_BYTES, 'output_bytes': collaboration.MAX_ARTIFACT_BYTES},
            'limitations': ['工作区绑定仅覆盖本机路径与普通根目录身份，不是全配置修订或跨电脑 UUID。',
                            '服务身份、声明、心跳与真实业务执行分别记录；目录约定不是操作系统隔离。',
                            '只读接口不登记心跳、不创建数据库、不读取原生私有配置。']})
    if not {'_workspace_root', 'capability_id', 'connection_revision'} <= set(body):
        raise InteropError('invalid_request', '快照须带能力 ID、连接修订及当前工作区绑定。', 400)
    if not isinstance(body['capability_id'], str) or not _HEX32.fullmatch(body['capability_id']):
        raise InteropError('invalid_request', '能力 ID 格式不正确。', 400)
    if not isinstance(body['connection_revision'], str) or not _HEX64.fullmatch(body['connection_revision']):
        raise InteropError('invalid_request', '连接修订格式不正确。', 400)
    if 'expected_declaration_sha256' in body and (not isinstance(body['expected_declaration_sha256'], str) or not _HEX64.fullmatch(body['expected_declaration_sha256'])):
        raise InteropError('invalid_request', '声明摘要格式不正确。', 400)
    return _snapshot(cfg, body, identity, workspace, connection)
