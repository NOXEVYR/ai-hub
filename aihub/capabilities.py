"""Declared capabilities and explicit queue dispatch; never executes providers."""
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading

from . import collaboration, config, capability_discovery

DOMAINS = {'video', 'image', 'audio', 'code', 'research', 'document', 'automation'}
KINDS = {'skill', 'mcp_tool'}
ACTIONS = {'capability_publish', 'capability_list', 'capability_recommend',
           'capability_dispatch', 'capability_discover'}
MAX_CAPABILITIES = 128
MAX_ROOT_CAPABILITIES = 2048
_LOCK = threading.RLock()
_SENSITIVE = re.compile(r'^(?:api[-_]?key|(?:access|refresh|auth|bearer|id|session)[-_]?token|token|(?:client|private|api)[-_]?(?:secret|key)|password|passwd|secret|credentials?|authorization|cookies?|env|headers|command|executable|url|endpoint)$', re.I)
_SECRET_VALUE = re.compile(r'(?:Bearer\s+[A-Za-z0-9._~-]{12,}|sk-[A-Za-z0-9_-]{16,}|https?://[^\s/@]+:[^\s/@]+@|[?&](?:api_key|token|secret)=)', re.I)
_DOMAIN_WORDS = {
    'video': ('视频', '短片', '动画', '剪辑', 'video'),
    'image': ('图片', '图像', '绘画', '生图', 'image'),
    'audio': ('语音', '音频', '配音', '音乐', 'audio'),
    'code': ('代码', '编程', '修复', '开发', 'code'),
    'research': ('检索', '搜索', '调研', '研究', 'research'),
    'document': ('文档', '报告', '表格', '幻灯片', '办公', 'document'),
    'automation': ('自动化', '流程', '调度', 'automation'),
}


def _text(value, name, limit=1000, optional=False):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and c not in '\n\t\r' for c in value):
        raise ValueError(name + ' 格式或长度不正确。')
    value = value.strip()
    if not optional and not value:
        raise ValueError(name + ' 不能为空。')
    if _SECRET_VALUE.search(value):
        raise ValueError('不能提交凭据或带凭据的地址。')
    return value


def _strings(value, name, maximum=20, limit=200):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(name + ' 条数超过限制。')
    result = [_text(v, name, limit) for v in value]
    if len(set(result)) != len(result):
        raise ValueError(name + ' 不能重复。')
    return result


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('JSON 字段不能重复。')
        result[key] = value
    return result


def _json(text, limit):
    text = _text(text, 'JSON', limit)
    try:
        return json.loads(text, object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError('JSON 数值必须有限。')))
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError('JSON 格式不正确。') from error


def _safe_data(value, depth=0):
    if depth > 8:
        raise ValueError('数据嵌套过深。')
    if isinstance(value, dict):
        if len(value) > 64:
            raise ValueError('对象字段过多。')
        for key, item in value.items():
            _text(key, '字段名', 100)
            if _SENSITIVE.fullmatch(key):
                raise ValueError('不能提交凭据、执行命令或连接配置。')
            _safe_data(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 128:
            raise ValueError('数组条数过多。')
        for item in value:
            _safe_data(item, depth + 1)
    elif isinstance(value, str):
        _text(value, '字段值', 12000, True)
    elif value is not None and type(value) not in (int, float, bool):
        raise ValueError('不支持的数据类型。')
    elif type(value) is float and not math.isfinite(value):
        raise ValueError('数值必须有限。')


def _schema(value, depth=0):
    if not isinstance(value, dict) or depth > 5:
        raise ValueError('inputs 必须是有限深度的 JSON Schema 对象。')
    allowed = {'type', 'description', 'properties', 'required', 'additionalProperties', 'items', 'enum',
               'minLength', 'maxLength', 'minimum', 'maximum', 'minItems', 'maxItems'}
    if set(value) - allowed:
        raise ValueError('inputs 含不支持的 Schema 关键字；不能解析外部引用。')
    kind = value.get('type')
    if kind not in {'object', 'array', 'string', 'integer', 'number', 'boolean', 'null'}:
        raise ValueError('Schema 必须声明单一 type。')
    if 'description' in value:
        _text(value['description'], 'description', 1000, True)
    if 'enum' in value:
        if not isinstance(value['enum'], list) or not 1 <= len(value['enum']) <= 32:
            raise ValueError('enum 条数不正确。')
        _safe_data(value['enum'])
    for key in ('minLength', 'maxLength', 'minItems', 'maxItems'):
        if key in value and (type(value[key]) is not int or not 0 <= value[key] <= 12000):
            raise ValueError('Schema 长度限制无效。')
    for key in ('minimum', 'maximum'):
        if key in value and (type(value[key]) not in (int, float) or not math.isfinite(value[key])):
            raise ValueError('Schema 数值限制无效。')
    for low, high in (('minLength', 'maxLength'), ('minItems', 'maxItems'), ('minimum', 'maximum')):
        if low in value and high in value and value[low] > value[high]:
            raise ValueError('Schema 上下界矛盾。')
    if kind == 'object':
        props = value.get('properties', {})
        if not isinstance(props, dict) or len(props) > 32:
            raise ValueError('Schema 对象字段过多。')
        required = _strings(value.get('required', []), 'required', 32, 100)
        if set(required) - set(props):
            raise ValueError('required 必须引用 properties。')
        if value.get('additionalProperties', False) is not False:
            raise ValueError('必须禁止未知输入字段。')
        for key, child in props.items():
            _text(key, '输入字段', 100)
            if _SENSITIVE.fullmatch(key):
                raise ValueError('输入不能要求凭据或连接配置。')
            _schema(child, depth + 1)
    elif kind == 'array':
        _schema(value.get('items'), depth + 1)
    if kind != 'object' and set(value) & {'properties', 'required', 'additionalProperties'}:
        raise ValueError('对象 Schema 字段只能用于 object。')
    if kind != 'array' and 'items' in value:
        raise ValueError('items 只能用于 array。')
    return value


def _input(value, schema):
    kind = schema['type']
    ok = {'object': isinstance(value, dict), 'array': isinstance(value, list),
          'string': isinstance(value, str), 'integer': type(value) is int,
          'number': type(value) in (int, float), 'boolean': type(value) is bool,
          'null': value is None}[kind]
    if not ok or ('enum' in schema and not any(type(value) is type(v) and value == v for v in schema['enum'])):
        raise ValueError('输入类型或枚举不符合声明。')
    if kind == 'object':
        props = schema.get('properties', {})
        if set(schema.get('required', [])) - set(value) or set(value) - set(props):
            raise ValueError('缺少必填输入或存在未知输入。')
        for key, item in value.items():
            _input(item, props[key])
    elif kind == 'array':
        for item in value:
            _input(item, schema['items'])
    if kind in {'array', 'string'}:
        low, high = ('minItems', 'maxItems') if kind == 'array' else ('minLength', 'maxLength')
        if len(value) < schema.get(low, 0) or len(value) > schema.get(high, 12000):
            raise ValueError('输入长度不符合声明。')
    if kind in {'integer', 'number'} and (value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf)):
        raise ValueError('输入数值不符合声明。')


def _capability(item):
    fields = {'key', 'name', 'kind', 'provider', 'server', 'domains', 'description',
              'tags', 'inputs', 'outputs', 'constraints', 'hints'}
    if not isinstance(item, dict) or set(item) - fields:
        raise ValueError('能力字段不支持；不可覆盖执行端、验证状态或命令配置。')
    _safe_data(item)
    key = _text(item.get('key'), 'key', 100)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}', key):
        raise ValueError('能力 key 格式无效。')
    kind = item.get('kind')
    if kind not in KINDS:
        raise ValueError('能力 kind 须为 skill 或 mcp_tool。')
    domains = _strings(item.get('domains', []), 'domains', 7, 30)
    if not domains or set(domains) - DOMAINS:
        raise ValueError('能力须声明有效场景 domains。')
    schema = _schema(item.get('inputs', {'type': 'object', 'properties': {}}))
    if schema['type'] != 'object':
        raise ValueError('能力 inputs 顶层必须是 object。')
    hints = item.get('hints', {})
    if not isinstance(hints, dict) or set(hints) - {'cost', 'speed', 'quality'}:
        raise ValueError('hints 仅允许 cost、speed、quality。')
    return dict(key=key, name=_text(item.get('name'), 'name', 160), kind=kind,
                provider=_text(item.get('provider', ''), 'provider', 160, True),
                server=_text(item.get('server', ''), 'server', 160, True), domains=domains,
                description=_text(item.get('description', ''), 'description', 2000, True),
                tags=_strings(item.get('tags', []), 'tags'), inputs=schema,
                outputs=_strings(item.get('outputs', []), 'outputs'),
                constraints=_strings(item.get('constraints', []), 'constraints', 20, 500),
                hints={k: _text(v, k, 300, True) for k, v in hints.items()})


@contextlib.contextmanager
def store(cfg):
    root = config._key(collaboration.root_path(cfg))
    with _LOCK:
        directory = Path(config.DATA_DIR)
        config._check_ancestors(str(directory))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'capabilities.sqlite3'
        for suffix in ('', '-wal', '-shm', '-journal'):
            candidate = Path(str(path) + suffix)
            if os.path.lexists(candidate):
                info = candidate.lstat()
                if config._is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('能力数据库不能使用链接或特殊文件。')
        con = sqlite3.connect(str(path), timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute('BEGIN IMMEDIATE')
            con.execute('''CREATE TABLE IF NOT EXISTS capabilities(
                root TEXT NOT NULL, client_id TEXT NOT NULL, key TEXT NOT NULL, id TEXT NOT NULL,
                tool TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(root,client_id,key), UNIQUE(root,id))''')
            yield con, root
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()


def _clients(cfg):
    with collaboration.store(cfg) as (con, root):
        return {r['id']: dict(r) for r in con.execute('SELECT id,tool,last_seen FROM clients WHERE root=?', (root,))}


def _client(cfg, identifier):
    identifier = collaboration._segment(identifier, 'client_id')
    found = _clients(cfg).get(identifier)
    if not found:
        raise ValueError('请先在当前工作环境登记客户端心跳。')
    collaboration._tool(found['tool'], False, cfg=cfg, protocol=True)
    return found


def publish(cfg, body):
    client = _client(cfg, body.get('client_id'))
    collaboration._tool(client['tool'], False, cfg=cfg, protocol=True)
    items = _json(body.get('capabilities_json'), 256000)
    if not isinstance(items, list) or len(items) > MAX_CAPABILITIES:
        raise ValueError('能力列表不能超过 128 条。')
    validated = [_capability(item) for item in items]
    if len({i['key'] for i in validated}) != len(validated):
        raise ValueError('同一客户端能力 key 不能重复。')
    stamp = collaboration._now()
    with store(cfg) as (con, root):
        count = con.execute('SELECT COUNT(*) FROM capabilities WHERE root=? AND client_id<>?', (root, client['id'])).fetchone()[0]
        if count + len(validated) > MAX_ROOT_CAPABILITIES:
            raise ValueError('工作环境能力总数超过限制。')
        con.execute('DELETE FROM capabilities WHERE root=? AND client_id=?', (root, client['id']))
        for item in validated:
            identifier = hashlib.sha256(json.dumps([root, client['id'], item['key']], ensure_ascii=False).encode()).hexdigest()[:32]
            con.execute('INSERT INTO capabilities VALUES(?,?,?,?,?,?,?)',
                        (root, client['id'], item['key'], identifier, client['tool'], json.dumps(item, ensure_ascii=False), stamp))
    return {'published': len(validated), 'client_id': client['id'], 'target_tool': client['tool'],
            'declaration_status': 'declared', 'verification_status': 'unverified', 'replaced_client_snapshot': True}


def catalog(cfg, body=None):
    body = body or {}
    clients = _clients(cfg)
    query = _text(body.get('query', ''), 'query', 2000, True).casefold()
    if body.get('tool') and body.get('target_tool') and body['tool'] != body['target_tool']:
        raise ValueError('tool 与 target_tool 筛选不一致。')
    selected_tool = body.get('tool') or body.get('target_tool')
    for field in ('tool', 'target_tool'):
        if body.get(field):
            collaboration._tool(body[field], cfg=cfg, include_disabled=True)
    for field, options in (('domain', DOMAINS), ('kind', KINDS)):
        if body.get(field) and body[field] not in options:
            raise ValueError('能力筛选无效。')
    with store(cfg) as (con, root):
        records = [dict(r) for r in con.execute('SELECT * FROM capabilities WHERE root=? ORDER BY client_id,key', (root,))]
    from . import harnesses
    enabled_tools = {v['id'] for v in harnesses.list_tools(cfg) if v['enabled'] and v['connection_mode'] == 'mcp_stdio'}
    now = dt.datetime.now(dt.timezone.utc)
    items = []
    for record in records:
        item = json.loads(record['payload'])
        if query and query not in ' '.join([item['name'], item['description'], item['provider'],
                                           item['server'], *item['tags']]).casefold():
            continue
        if body.get('domain') and body['domain'] not in item['domains']:
            continue
        if body.get('kind') and body['kind'] != item['kind']:
            continue
        if selected_tool and selected_tool != record['tool']:
            continue
        client = clients.get(record['client_id'])
        last_seen = client['last_seen'] if client else None
        age = (now - dt.datetime.fromisoformat(last_seen)).total_seconds() if last_seen else None
        enabled = record['tool'] in enabled_tools
        recent = enabled and age is not None and 0 <= age <= 300
        item.update(tool_enabled=enabled, id=record['id'], client_id=record['client_id'], target_tool=record['tool'],
                    updated_at=record['updated_at'], declaration_status='declared', verification_status='unverified',
                    client_online=recent, client_status='recent_heartbeat' if recent else 'not_recently_seen',
                    last_seen=last_seen, heartbeat_window_seconds=300, execution_mode='harness_queue', worker_required=True)
        items.append(item)
    return {'items': items, 'total': len(items), 'worker_required': True,
            'limitations': ['能力来自客户端声明，未执行验证。', '近期心跳不是实时在线保证。', '调度只排队，由工作端领取并执行。']}


def catalog_readonly(cfg):
    """Read existing declarations without creating directories, DBs, tables, or sidecars."""
    root = collaboration.root_path(cfg)
    directory = Path(config.DATA_DIR)
    config._check_ancestors(str(directory))
    path = directory / 'capabilities.sqlite3'
    if not os.path.lexists(path):
        return None
    for suffix in ('', '-wal', '-shm', '-journal'):
        candidate = Path(str(path) + suffix)
        if os.path.lexists(candidate):
            info = candidate.lstat()
            if config._is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return None
    wal, shm = Path(str(path) + '-wal'), Path(str(path) + '-shm')
    if os.path.lexists(wal) and not os.path.lexists(shm):
        return None
    try:
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
            con.row_factory = sqlite3.Row
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'capabilities' not in tables:
                return None
            columns = {row[1] for row in con.execute('PRAGMA table_info(capabilities)')}
            required = {'root', 'client_id', 'key', 'id', 'tool', 'payload', 'updated_at'}
            if not required <= columns:
                return None
            records = [dict(r) for r in con.execute(
                'SELECT * FROM capabilities WHERE root=? ORDER BY client_id,key', (config._key(root),))]
    except (OSError, sqlite3.Error, ValueError):
        return None

    clients = {}
    collab_path = directory / 'collaboration.sqlite3'
    if os.path.lexists(collab_path):
        safe = True
        for suffix in ('', '-wal', '-shm', '-journal'):
            candidate = Path(str(collab_path) + suffix)
            if os.path.lexists(candidate):
                info = candidate.lstat()
                if config._is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    safe = False
                    break
        if safe and not (os.path.lexists(Path(str(collab_path) + '-wal')) and
                         not os.path.lexists(Path(str(collab_path) + '-shm'))):
            try:
                with contextlib.closing(sqlite3.connect(
                        collab_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
                    con.row_factory = sqlite3.Row
                    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if 'clients' in tables:
                        columns = {row[1] for row in con.execute('PRAGMA table_info(clients)')}
                        if {'root', 'id', 'last_seen'} <= columns:
                            clients = {r['id']: r['last_seen'] for r in con.execute(
                                'SELECT id,last_seen FROM clients WHERE root=?', (config._key(root),))}
            except (OSError, sqlite3.Error, ValueError):
                clients = {}
    try:
        from . import harnesses
        enabled_tools = {item['id'] for item in harnesses.list_tools(cfg)
                         if item.get('enabled') and item.get('connection_mode') == 'mcp_stdio'}
    except (OSError, sqlite3.Error, ValueError, TypeError):
        enabled_tools = set()
    now = dt.datetime.now(dt.timezone.utc)
    items = []
    for record in records:
        try:
            item = _capability(json.loads(record['payload']))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        last_seen = clients.get(record['client_id'])
        try:
            age = (now - dt.datetime.fromisoformat(last_seen)).total_seconds() if last_seen else None
        except (ValueError, TypeError):
            age = None
        enabled = record['tool'] in enabled_tools
        recent = enabled and age is not None and 0 <= age <= 300
        item.update(tool_enabled=enabled, id=record['id'], client_id=record['client_id'], target_tool=record['tool'],
                    updated_at=record['updated_at'], declaration_status='declared', verification_status='unverified',
                    client_online=recent, client_status='recent_heartbeat' if recent else 'not_recently_seen',
                    last_seen=last_seen, heartbeat_window_seconds=300, execution_mode='harness_queue', worker_required=True)
        items.append(item)
    return {'items': items, 'total': len(items), 'worker_required': True,
            'limitations': ['能力来自客户端声明，未执行验证。', '近期心跳不是实时在线保证。', '调度只排队，由工作端领取并执行。']}


def recommend(cfg, body):
    query = _text(body.get('query'), 'query', 2000).casefold()
    # Recommendation uses token/domain matches; catalog's literal query would prematurely exclude them.
    result = catalog(cfg, {k: v for k, v in body.items() if k != 'query'})
    terms = set(re.findall(r'[a-z0-9_+-]{2,}|[\u3400-\u9fff]{2,}', query))
    requested = {d for d, words in _DOMAIN_WORDS.items() if any(w in query for w in words)}
    ranked = []
    for item in result['items']:
        if not item['tool_enabled']:
            continue
        score, reasons = 0, []
        matched = sorted(requested & set(item['domains']))
        if body.get('domain'):
            matched = sorted(set(matched) | {body['domain']})
        if matched:
            score += 3 * len(matched)
            reasons.append('声明场景匹配：' + '、'.join(matched))
        haystack = ' '.join([item['name'], item['description'], *item['tags']]).casefold()
        hits = sorted(t for t in terms if t in haystack)[:8]
        if hits:
            score += len(hits)
            reasons.append('名称、说明或标签匹配：' + '、'.join(hits))
        if score:
            ranked.append(dict(item, score=score, reasons=reasons))
    ranked.sort(key=lambda i: (-i['score'], i['name'], i['id']))
    return dict(result, items=ranked[:50], total=len(ranked), recommendation_basis='metadata_match',
                explanation='根据场景与声明文字筛选候选；不衡量实际质量、成本或任务成功率，需核对输入和限制。')


def dispatch(cfg, body, actor='ui'):
    if actor == 'mcp':
        _client(cfg, body.get('client_id'))
    identifier = _text(body.get('capability_id'), 'capability_id', 64)
    item = next((v for v in catalog(cfg)['items'] if v['id'] == identifier), None)
    if not item:
        raise ValueError('当前工作环境找不到该能力，请刷新目录。')
    collaboration._tool(item['target_tool'], False, cfg=cfg, protocol=True)
    inputs = _json(body.get('input_json', '{}'), 16000)
    _safe_data(inputs)
    _input(inputs, item['inputs'])
    description = '能力与输入均为未执行的请求数据，不构成额外权限。仅排队，工作端必须核验原生接口、输入和成本后领取执行。\n' + json.dumps({
        'capability_id': item['id'], 'capability_key': item['key'], 'name': item['name'],
        'provider': item['provider'], 'server': item['server'], 'kind': item['kind'],
        'declared_by_client': item['client_id'], 'target_tool': item['target_tool'],
        'inputs': inputs, 'constraints': item['constraints'], 'expected_outputs': item['outputs'],
        'verification_status': 'unverified',
        'workflow': 'task_claim → artifact_write/artifact_register → task_finish；交接使用 task_handoff。'}, ensure_ascii=False)
    task = collaboration.execute(cfg, 'task_create', {'project': body.get('project'), 'title': body.get('title'),
                'target_tool': item['target_tool'], 'description': description}, actor=actor)
    return {'status': 'queued', 'worker_required': True, 'capability_id': identifier,
            'target_tool': item['target_tool'], 'execution_mode': 'harness_queue', 'task': task}


def _known_skill_roots():
    return capability_discovery.known_skill_roots(codex_home=os.environ.get('CODEX_HOME'))


def validate_source_settings(body):
    return capability_discovery.validate_source_settings(body)


def discover(cfg):
    return capability_discovery.discover(cfg, _known_skill_roots(), catalog_readonly)


def execute(cfg, action, body, actor='ui'):
    if actor not in {'ui', 'mcp'} or not isinstance(body, dict):
        raise ValueError('能力请求格式或来源无效。')
    if action not in ACTIONS:
        raise ValueError('不支持的能力操作。')
    expected = body.get('_workspace_root')
    if expected is not None and (not isinstance(expected, str) or config._key(expected) != config._key(cfg.get('ai_root', ''))):
        raise ValueError('工作环境已切换，请刷新。')
    if action == 'capability_discover':
        if actor != 'ui':
            raise PermissionError('本机 Skill 发现仅供本地界面使用。')
        return discover(cfg)
    collaboration.root_path(cfg)
    if action == 'capability_publish':
        return publish(cfg, body)
    if action == 'capability_list':
        return catalog(cfg, body)
    if action == 'capability_recommend':
        return recommend(cfg, body)
    return dispatch(cfg, body, actor)
