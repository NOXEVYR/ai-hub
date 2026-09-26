"""Bounded, metadata-only discovery for local skills and interface declarations."""
import json
import os
from pathlib import Path
import re
import stat

from . import config

MAX_ROOTS = 48
MAX_ENTRIES = 4096
MAX_PLUGIN_DEPTH = 8
MAX_SUGGESTIONS = 512
MAX_DECLARATION_FILES = MAX_ROOTS
MAX_DECLARATION_BYTES = 128 * 1024
_SECRET_VALUE = re.compile(
    r'(?:Bearer\s+[A-Za-z0-9._~-]{12,}|sk-[A-Za-z0-9_-]{16,}|'
    r'https?://[^\s/@]+:[^\s/@]+@|[?&](?:api_key|token|secret)=)', re.I)
_DOMAINS = {
    'image': ('image', '图片', '图像', '绘画', '生图', '视觉生成'),
    'video': ('video', '视频', '短片', '动画', '剪辑'),
    'audio': ('audio', '语音', '音频', '配音', '声音', 'tts', 'asr', '音乐'),
    'code': ('code', 'coding', 'software', 'development', 'develop', 'program', 'application',
             '代码', '编码', '编程', '软件', '开发', '修复'),
    'research': ('research', '检索', '搜索', '调研', '研究'),
    'document': ('document', '文档', '报告', '表格', '幻灯片', '办公'),
    'automation': ('automation', '自动化', '流程', '调度'),
}
_CATEGORY_CHINESE = {
    'image': ('图片', '图像', '绘画', '生图', '插画', '画风', '图案', '角色 LoRA'),
    'video': ('视频', '短片', '短剧', '动画', '剪辑', '镜头', '分镜', '影片', '影视'),
    'audio': ('语音', '音频', '配音', '声音', '音乐', '听写', '转写'),
    'code': ('代码', '编码', '编程', '软件', '开发', '调试'),
    'research': ('检索', '搜索', '调研', '研究'),
    'document': ('文档', '报告', '表格', '幻灯片', '办公'),
    'automation': ('自动化', '流程', '调度'),
}
_CATEGORY_ENGLISH = {
    'image': r'(?<![a-z0-9])(?:images?|photos?|pictures?|illustrations?|drawings?|paintings?|lora)(?![a-z0-9])',
    'video': r'(?<![a-z0-9])(?:videos?|films?|cinema|animations?|animated|editing|clips?|seedance|wan)(?![a-z0-9])',
    'audio': r'(?<![a-z0-9])(?:audio|voices?|speech|tts|asr|music|sounds?)(?![a-z0-9])',
    'code': r'(?<![a-z0-9])(?:code|coding|coder|software|develop|developed|developing|development|developer|program|programs|programming|programmer|debug|debugging)(?![a-z0-9])',
    'research': r'(?<![a-z0-9])(?:research|search|retrieval|investigation|survey)(?![a-z0-9])',
    'document': r'(?<![a-z0-9])(?:pdf|documents?|reports?|spreadsheets?|excel|xlsx?|csv|presentations?|powerpoint|pptx?|slides?|word|docx?|office|workbooks?|worksheets?|tables?)(?![a-z0-9])',
    # "workflow" alone is too broad: many creative Skills describe a workflow
    # without providing automation or scheduling capability.
    'automation': r'(?<![a-z0-9])(?:automation|orchestration|scheduling)(?![a-z0-9])',
}
_CLAUSE_BREAK = re.compile(r'[.!?。！？;；\n]+|\b(?:but|however|instead|rather\s+than)\b|但是|然而|不过|相反|而是|但', re.I)
_NEGATED_CHINESE = re.compile(r'(?:不(?:适用于|用于|面向|支持|负责|提供|制作|生成|做|是|属于)|不适合|并非|不是|非)\s*[^.!?。！？;；\n]{0,48}$', re.I)
_NEGATED_ENGLISH = re.compile(r'\b(?:not|never)\s+(?:(?:primarily|specifically)\s+)?(?:for|intended\s+for|used\s+for|designed\s+for|support(?:s)?|provide(?:s)?|create(?:s)?|generate(?:s)?)\b[^.!?。！？;；\n]{0,60}$', re.I)
_NEGATED_DIRECT = re.compile(r'(?:\b(?:not|never)\s+(?:an?\s+|the\s+)?|\bnon[-\s]+|(?:并非|不是|非)\s*)$', re.I)
_MODALITIES = ('image', 'video', 'audio')
_CREDENTIAL_HINTS = {
    'OPENAI_API_KEY': 'openai', 'ANTHROPIC_API_KEY': 'anthropic',
    'GOOGLE_API_KEY': 'google', 'GEMINI_API_KEY': 'google',
    'DASHSCOPE_API_KEY': 'dashscope', 'ALIBABA_CLOUD_ACCESS_KEY_ID': 'aliyun',
    'ALIBABA_CLOUD_ACCESS_KEY_SECRET': 'aliyun', 'ARK_API_KEY': 'volcengine',
    'VOLCENGINE_ACCESS_KEY_ID': 'volcengine', 'VOLCENGINE_ACCESS_KEY_SECRET': 'volcengine',
    'REPLICATE_API_TOKEN': 'replicate', 'FAL_KEY': 'fal',
    'STABILITY_API_KEY': 'stability', 'HF_TOKEN': 'huggingface',
}


def known_skill_roots(home=None, codex_home=None):
    """Return conventional per-user skill roots and the Codex plugin cache."""
    home = Path(home) if home is not None else Path.home()
    roots = [
        ('codex', home / '.codex' / 'skills'),
        ('codex', home / '.codex' / 'skills' / '.system'),
        ('shared', home / '.agents' / 'skills'),
        ('zcode', home / '.zcode' / 'skills'),
        ('workbuddy', home / '.workbuddy' / 'skills'),
        ('dsh', home / '.dsh' / 'skills'),
    ]
    codex_home_path = None
    if codex_home:
        try:
            candidate = Path(codex_home).expanduser()
            if candidate.is_absolute():
                codex_home_path = candidate
        except (TypeError, ValueError, OSError):
            pass
    codex_home_path = codex_home_path or (home / '.codex')
    roots.extend((('codex', codex_home_path / 'skills'),
                  ('codex', codex_home_path / 'skills' / '.system'),
                  ('codex', codex_home_path / 'plugins' / 'cache')))
    result, seen = [], set()
    for tool, path in roots:
        key = config._key(str(path))
        if key not in seen and len(result) < MAX_ROOTS:
            seen.add(key)
            result.append((tool, path))
    return result


def configured_skill_roots(cfg):
    """Accept only explicitly supplied local skill roots; never inspect tool config."""
    configured = cfg.get('capability_sources', []) if isinstance(cfg, dict) else []
    if not isinstance(configured, list):
        configured = []
    roots, seen = [], set()
    for source in configured[:MAX_ROOTS]:
        if not isinstance(source, dict) or source.get('kind') != 'skills_root':
            continue
        value = source.get('path')
        if not isinstance(value, str) or not value or not os.path.isabs(value):
            continue
        path = Path(value)
        tool = source.get('tool', 'custom')
        if not isinstance(tool, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}', tool):
            tool = 'custom'
        key = (config._key(str(path)), tool.casefold())
        if key not in seen:
            roots.append((tool, path))
            seen.add(key)
    # Backward-compatible direct roots for callers that predate capability_sources.
    legacy = cfg.get('capability_skill_roots', []) if isinstance(cfg, dict) else []
    if isinstance(legacy, list):
        for value in legacy[:MAX_ROOTS]:
            if isinstance(value, str) and value and os.path.isabs(value):
                path = Path(value)
                key = (config._key(str(path)), 'custom')
                if key not in seen:
                    roots.append(('custom', path))
                    seen.add(key)
    return roots


def validate_source_settings(body):
    """Validate UI-supplied per-computer discovery sources for config persistence."""
    if not isinstance(body, dict) or set(body) != {'sources'} or not isinstance(body['sources'], list):
        raise ValueError('来源设置必须包含 sources 数组。')
    if len(body['sources']) > 32:
        raise ValueError('发现来源最多 32 项。')
    result, seen = [], set()
    sensitive_parts = {'.ssh', '.config', 'secrets', 'credentials', 'history', 'logs'}
    for entry in body['sources']:
        if not isinstance(entry, dict) or set(entry) != {'tool', 'kind', 'path'}:
            raise ValueError('每项来源须包含 tool、kind、path。')
        tool, kind, value = entry['tool'], entry['kind'], entry['path']
        if not isinstance(tool, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}', tool):
            raise ValueError('tool 必须是有效的工具标识。')
        if not isinstance(kind, str) or kind not in {'skills_root', 'capability_manifest'}:
            raise ValueError('kind 只允许 skills_root 或 capability_manifest。')
        if not isinstance(value, str) or len(value) > 2048 or not os.path.isabs(value) or value.startswith(('\\\\', '//')):
            raise ValueError('path 必须是本机绝对路径。')
        path = Path(value)
        if any(part.casefold() in sensitive_parts for part in path.parts):
            raise ValueError('发现来源不能位于凭据、历史或配置敏感目录。')
        config._check_ancestors(str(path if kind == 'skills_root' else path.parent))
        info = path.lstat()
        if config._is_reparse(info):
            raise ValueError('发现来源不能是链接或重解析点。')
        if kind == 'skills_root':
            if not stat.S_ISDIR(info.st_mode) or os.path.dirname(str(path)) == str(path):
                raise ValueError('skills_root 必须是本机专用目录。')
        else:
            if (not path.name.casefold().endswith('.capabilities.json') or
                    not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_size > MAX_DECLARATION_BYTES):
                raise ValueError('capability_manifest 必须是小于 128 KiB 的普通 .capabilities.json 文件。')
        key = (tool.casefold(), kind, config._key(str(path)))
        if key in seen:
            raise ValueError('发现来源不能重复。')
        seen.add(key)
        result.append({'tool': tool, 'kind': kind, 'path': str(path)})
    return result


def _category(*values):
    text = ' '.join(v for v in values if isinstance(v, str)).casefold()
    def is_negated(start):
        prefix = text[:start]
        boundaries = list(_CLAUSE_BREAK.finditer(prefix))
        if boundaries:
            prefix = prefix[boundaries[-1].end():]
        if _NEGATED_CHINESE.search(prefix) or _NEGATED_ENGLISH.search(prefix):
            return True
        return _NEGATED_DIRECT.search(prefix[-24:]) is not None

    def has_chinese_term(term):
        start = text.find(term.casefold())
        while start >= 0:
            if not is_negated(start):
                return True
            start = text.find(term.casefold(), start + 1)
        return False

    def has_english_term(pattern):
        return any(not is_negated(match.start()) for match in re.finditer(pattern, text, re.I))

    return [name for name in _DOMAINS
            if any(has_chinese_term(term) for term in _CATEGORY_CHINESE[name])
            or has_english_term(_CATEGORY_ENGLISH[name])]


def _clean_text(value, limit):
    if not isinstance(value, str) or len(value) > limit or any(ord(ch) < 32 and ch not in '\r\n\t' for ch in value):
        raise ValueError('invalid metadata')
    value = value.strip()
    if _SECRET_VALUE.search(value):
        raise ValueError('secret-like metadata')
    return value


def _read_skill(path, tool):
    info = path.lstat()
    if config._is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('unsafe skill file')
    with path.open('r', encoding='utf-8-sig') as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or opened.st_nlink != 1:
            raise ValueError('skill identity changed')
        if handle.readline(1025).strip() != '---':
            return None
        meta, total, closed = {}, 0, False
        for _ in range(40):
            line = handle.readline(2049)
            total += len(line)
            if total > 8192 or len(line) > 2048:
                raise ValueError('frontmatter over limit')
            if line.strip() == '---':
                closed = True
                break
            match = re.match(r'^(name|description):\s*(.*)$', line)
            if match:
                raw = match.group(2).strip().strip('"\'')
                if raw not in {'|', '>', '|-', '>-'}:
                    meta[match.group(1)] = _clean_text(raw, 160 if match.group(1) == 'name' else 2000)
        if not closed or not meta.get('name'):
            return None
    categories = _category(meta.get('name', ''), meta.get('description', ''))
    return {
        **meta, 'tool': tool, 'tools': [tool], 'kind': 'skill', 'category': categories[0] if categories else 'other',
        'categories': categories, 'domains': categories, 'path': str(path), 'source': 'local_skill_frontmatter',
        'classification_source': 'metadata_keyword_heuristic',
        'classification_status': 'heuristic' if categories else 'unclassified',
        'classification_note': '根据 Skill 名称和简介关键词推断用途，不代表准确能力判定。',
        'declaration_status': 'discovered', 'verification_status': 'unverified',
        'discovery_only': True, 'published': False,
        'evidence': {
            'source': {'status': 'metadata_found', 'type': 'SKILL.md frontmatter'},
            'configuration': {'status': 'local_file_found', 'evidence': 'No native tool configuration was read.'},
            'callability': {'status': 'not_tested', 'evidence': 'Discovery does not load or execute Skills.'},
            'actual_invocation': {'status': 'not_observed', 'evidence': 'No Skill invocation was performed.'},
        },
    }


def _is_plugin_cache(root):
    parts = [part.casefold() for part in root.parts]
    return len(parts) >= 2 and parts[-1] == 'cache' and parts[-2] == 'plugins'


class _PluginMetadataParser:
    """Parse only public name/description and mcpServers key presence."""
    def __init__(self, text):
        self.text, self.pos = text, 0

    def _ws(self):
        while self.pos < len(self.text) and self.text[self.pos] in ' \t\r\n':
            self.pos += 1

    def _string(self, decode=False):
        self._ws()
        start = self.pos
        if self.pos >= len(self.text) or self.text[self.pos] != '"':
            raise ValueError('expected string')
        self.pos += 1
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == '"':
                self.pos += 1
                token = self.text[start:self.pos]
                return json.loads(token) if decode else None
            if ord(char) < 0x20:
                raise ValueError('invalid string')
            if char == '\\':
                self.pos += 1
                if self.pos >= len(self.text):
                    raise ValueError('invalid escape')
                escape = self.text[self.pos]
                if escape == 'u':
                    if not re.fullmatch(r'[0-9a-fA-F]{4}', self.text[self.pos + 1:self.pos + 5]):
                        raise ValueError('invalid unicode escape')
                    self.pos += 5
                    continue
                if escape not in '"\\/bfnrt':
                    raise ValueError('invalid escape')
            self.pos += 1
        raise ValueError('unterminated string')

    def _value(self, depth=0):
        if depth > 16:
            raise ValueError('manifest too deep')
        self._ws()
        if self.pos >= len(self.text):
            raise ValueError('missing value')
        char = self.text[self.pos]
        if char == '"':
            self._string(False)
            return
        if char == '{':
            self.pos += 1
            self._ws()
            if self.pos < len(self.text) and self.text[self.pos] == '}':
                self.pos += 1
                return
            while True:
                self._string(False)
                self._ws()
                if self.pos >= len(self.text) or self.text[self.pos] != ':':
                    raise ValueError('invalid object')
                self.pos += 1
                self._value(depth + 1)
                self._ws()
                if self.pos < len(self.text) and self.text[self.pos] == '}':
                    self.pos += 1
                    return
                if self.pos >= len(self.text) or self.text[self.pos] != ',':
                    raise ValueError('invalid object separator')
                self.pos += 1
        elif char == '[':
            self.pos += 1
            self._ws()
            if self.pos < len(self.text) and self.text[self.pos] == ']':
                self.pos += 1
                return
            while True:
                self._value(depth + 1)
                self._ws()
                if self.pos < len(self.text) and self.text[self.pos] == ']':
                    self.pos += 1
                    return
                if self.pos >= len(self.text) or self.text[self.pos] != ',':
                    raise ValueError('invalid array separator')
                self.pos += 1
        else:
            start = self.pos
            while self.pos < len(self.text) and self.text[self.pos] not in ' \t\r\n,]}':
                self.pos += 1
            token = self.text[start:self.pos]
            if token not in {'true', 'false', 'null'} and not re.fullmatch(
                    r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?', token):
                raise ValueError('invalid scalar')

    def parse(self):
        self._ws()
        if self.pos >= len(self.text) or self.text[self.pos] != '{':
            raise ValueError('manifest root must be object')
        self.pos += 1
        result, seen = {}, set()
        self._ws()
        if self.pos < len(self.text) and self.text[self.pos] == '}':
            self.pos += 1
        else:
            while True:
                key = self._string(True)
                if key in seen:
                    raise ValueError('duplicate key')
                seen.add(key)
                self._ws()
                if self.pos >= len(self.text) or self.text[self.pos] != ':':
                    raise ValueError('invalid object')
                self.pos += 1
                if key in {'name', 'description'}:
                    result[key] = self._string(True)
                else:
                    if key == 'mcpServers':
                        result['mcpServers_present'] = True
                    self._value()
                self._ws()
                if self.pos < len(self.text) and self.text[self.pos] == '}':
                    self.pos += 1
                    break
                if self.pos >= len(self.text) or self.text[self.pos] != ',':
                    raise ValueError('invalid object separator')
                self.pos += 1
        self._ws()
        if self.pos != len(self.text):
            raise ValueError('trailing data')
        if 'name' in result:
            result['name'] = _clean_text(result['name'], 160)
        if 'description' in result:
            result['description'] = _clean_text(result['description'], 1000)
        return result


def _read_plugin_metadata(package):
    directory = package / '.codex-plugin'
    manifest = directory / 'plugin.json'
    config._check_ancestors(str(directory))
    directory_info = directory.lstat()
    if config._is_reparse(directory_info) or not stat.S_ISDIR(directory_info.st_mode):
        raise ValueError('unsafe plugin metadata directory')
    info = manifest.lstat()
    if config._is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 32768:
        raise ValueError('unsafe plugin manifest')
    with manifest.open('rb') as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or opened.st_nlink != 1:
            raise ValueError('plugin manifest identity changed')
        raw = handle.read(32769)
    if len(raw) > 32768:
        raise ValueError('plugin manifest over limit')
    parsed = _PluginMetadataParser(raw.decode('utf-8-sig')).parse()
    if not parsed.get('name') or not parsed.get('mcpServers_present'):
        return None
    return {'plugin_name': parsed['name'], 'description': parsed.get('description', ''),
            'mcp_servers_declared': True, 'source_path': str(manifest),
            'plugin_package': str(package)}


def _scan_root(tool, root, budget, suggestions):
    source_type = 'codex_plugin_cache' if _is_plugin_cache(root) else 'skill_root'
    source = {'type': source_type, 'tool': tool, 'path': str(root), 'status': 'missing', 'items_found': 0}
    plugin_interfaces = []
    if not os.path.lexists(root):
        return source, 0, plugin_interfaces
    try:
        config._check_ancestors(str(root))
        root_info = root.lstat()
        if config._is_reparse(root_info) or not stat.S_ISDIR(root_info.st_mode):
            raise ValueError('unsafe root')
    except (OSError, ValueError):
        source['status'] = 'skipped_unsafe_root'
        return source, 1, plugin_interfaces
    source['status'] = 'scanned'
    examined = 0

    def visit_package_dir(parent, package_entries):
        nonlocal examined
        for package in package_entries:
            if examined >= budget or len(suggestions) >= MAX_SUGGESTIONS:
                break
            examined += 1
            if package.name.startswith('.'):
                continue
            try:
                if config._is_reparse(package.stat(follow_symlinks=False)) or not package.is_dir(follow_symlinks=False):
                    continue
                path = Path(package.path) / 'SKILL.md'
                config._check_ancestors(str(path.parent))
                item = _read_skill(path, tool)
                if item:
                    item['source_root'] = str(root)
                    if source_type == 'codex_plugin_cache':
                        item['source'] = 'codex_plugin_skill_frontmatter'
                        if parent.name.casefold() == 'skills':
                            item['plugin_package'] = str(parent.parent)
                    suggestions.append(item)
                    source['items_found'] += 1
            except (OSError, UnicodeError, ValueError):
                source['items_skipped'] = source.get('items_skipped', 0) + 1

    if not _is_plugin_cache(root):
        try:
            with os.scandir(root) as entries:
                visit_package_dir(root, entries)
        except OSError:
            source['status'] = 'unreadable'
            return source, max(1, examined), plugin_interfaces
    else:
        stack = [(root, 0)]
        try:
            while stack and examined < budget and len(suggestions) < MAX_SUGGESTIONS:
                current, depth = stack.pop()
                if depth > MAX_PLUGIN_DEPTH:
                    continue
                with os.scandir(current) as entries:
                    for entry in entries:
                        if examined >= budget or len(suggestions) >= MAX_SUGGESTIONS:
                            break
                        examined += 1
                        if entry.name.casefold() == '.codex-plugin':
                            try:
                                if config._is_reparse(entry.stat(follow_symlinks=False)) or not entry.is_dir(follow_symlinks=False):
                                    continue
                                metadata = _read_plugin_metadata(current)
                                if metadata:
                                    metadata.update({'tool': tool, 'tools': [tool], 'provider': '',
                                        'kind': 'plugin_mcp_hint', 'source': 'codex_plugin_manifest',
                                        'declaration_status': 'manifest_declared', 'verification_status': 'unverified',
                                        'discovery_only': True, 'published': False, 'queue_eligible': False,
                                        'mcp_details_included': False,
                                        'next_step': '在 Codex 插件管理中检查并连接此插件；本地发现未读取服务器配置或具体调用端点。',
                                        'evidence': {
                                            'source': {'status': 'public_plugin_manifest', 'type': '.codex-plugin/plugin.json'},
                                            'configuration': {'status': 'manifest_has_mcpServers',
                                                'evidence': 'Only the top-level mcpServers key was checked; registration and connection were not checked.'},
                                            'callability': {'status': 'not_tested',
                                                'evidence': 'Server commands, arguments, environment and endpoint definitions were not inspected.'},
                                            'actual_invocation': {'status': 'not_observed',
                                                'evidence': 'No plugin or provider call was performed.'},
                                        }})
                                    plugin_interfaces.append(metadata)
                            except (OSError, UnicodeError, ValueError, RecursionError):
                                source['plugin_manifests_skipped'] = source.get('plugin_manifests_skipped', 0) + 1
                            continue
                        if entry.name.startswith('.'):
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if config._is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                                continue
                            if entry.name.casefold() == 'skills':
                                with os.scandir(entry.path) as packages:
                                    visit_package_dir(Path(entry.path), packages)
                            elif depth < MAX_PLUGIN_DEPTH:
                                stack.append((Path(entry.path), depth + 1))
                        except (OSError, ValueError):
                            source['items_skipped'] = source.get('items_skipped', 0) + 1
        except OSError:
            source['status'] = 'partially_readable'
    for interface in plugin_interfaces:
        package_path = interface['plugin_package']
        domains = set()
        for item in suggestions:
            if item.get('plugin_package') == package_path:
                domains.update(item.get('domains', []))
        interface['domains'] = [domain for domain in _DOMAINS if domain in domains]
        interface['categories'] = interface['domains']
        interface['category'] = interface['domains'][0] if interface['domains'] else 'other'
        interface['classification_source'] = 'inferred_from_plugin_skills' if interface['domains'] else 'unknown'
    source['plugin_manifests_found'] = len(plugin_interfaces)
    return source, examined, plugin_interfaces


def _safe_declaration_file(value):
    if not isinstance(value, str) or not os.path.isabs(value):
        return None
    path = Path(value)
    if not path.name.casefold().endswith('.capabilities.json'):
        return None
    sensitive_parts = {'.ssh', '.config', 'secrets', 'credentials', 'history', 'logs'}
    if any(part.casefold() in sensitive_parts for part in path.parts):
        return None
    try:
        config._check_ancestors(str(path.parent))
        info = path.lstat()
        if config._is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        if info.st_size > MAX_DECLARATION_BYTES:
            return None
        return path, info
    except (OSError, ValueError):
        return None


def _declared_interfaces(cfg):
    found, sources = [], []
    configured = cfg.get('capability_sources', []) if isinstance(cfg, dict) else []
    files = [(entry.get('tool', ''), entry.get('path')) for entry in configured if isinstance(entry, dict) and
             entry.get('kind') == 'capability_manifest'] if isinstance(configured, list) else []
    legacy = cfg.get('capability_definition_files', []) if isinstance(cfg, dict) else []
    if isinstance(legacy, list):
        files.extend(('', value) for value in legacy)
    for tool, value in files[:MAX_DECLARATION_FILES]:
        path_info = _safe_declaration_file(value)
        source = {'type': 'explicit_capability_manifest', 'path': str(value) if isinstance(value, str) else '',
                  'status': 'skipped', 'items_found': 0}
        sources.append(source)
        if not path_info:
            continue
        path, info = path_info
        try:
            with path.open('rb') as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or opened.st_nlink != 1:
                    continue
                raw = handle.read(MAX_DECLARATION_BYTES + 1)
            if len(raw) > MAX_DECLARATION_BYTES:
                continue
            manifest = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(manifest, dict) or set(manifest) - {'schema_version', 'name', 'capabilities'}:
                continue
            records = manifest.get('capabilities', [])
            if not isinstance(records, list):
                continue
            source['status'] = 'scanned'
            for record in records[:128]:
                if not isinstance(record, dict) or set(record) - {
                        'name', 'description', 'domain', 'domains', 'provider', 'operation_id', 'tags', 'env_vars'}:
                    continue
                try:
                    name = _clean_text(record.get('name'), 160)
                    description = _clean_text(record.get('description', ''), 1000)
                    provider = _clean_text(record.get('provider', ''), 120)
                    operation_id = _clean_text(record.get('operation_id', ''), 120)
                    domains = record.get('domains', [record.get('domain', '')])
                    if not isinstance(domains, list) or len(domains) > 7:
                        continue
                    domains = [v for v in domains if isinstance(v, str) and v in _DOMAINS]
                    domains = list(dict.fromkeys(domains))
                    tags = record.get('tags', [])
                    if not isinstance(tags, list) or len(tags) > 20:
                        continue
                    tags = [_clean_text(tag, 100) for tag in tags]
                    env_vars = record.get('env_vars', [])
                    if not isinstance(env_vars, list) or len(env_vars) > 32:
                        continue
                    if any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', name)
                           for name in env_vars) or len(set(env_vars)) != len(env_vars):
                        continue
                except (ValueError, TypeError):
                    continue
                found.append({
                    'name': name, 'description': description, 'provider': provider,
                    'operation_id': operation_id, 'tags': tags, 'kind': 'public_definition',
                    'category': domains[0] if domains else 'other', 'categories': domains, 'domains': domains,
                    'tools': [tool] if tool else [], 'tool': tool, 'target_tool': tool,
                    'required_env_vars': env_vars,
                    'source': 'explicit_local_capability_manifest', 'source_path': str(path),
                    'declaration_status': 'explicit_definition', 'verification_status': 'unverified',
                    'discovery_only': True, 'published': False,
                    'evidence': {
                        'source': {'status': 'explicit_manifest', 'type': 'local .capabilities.json'},
                        'configuration': {'status': 'manifest_configured', 'evidence': 'File path and environment variable names were explicitly declared in capability_sources.'},
                        'callability': {'status': 'definition_only', 'evidence': 'A public metadata declaration is not an executable endpoint.'},
                        'actual_invocation': {'status': 'not_observed', 'evidence': 'No interface was called.'},
                    },
                })
                source['items_found'] += 1
        except (OSError, UnicodeError, ValueError, RecursionError):
            source['status'] = 'invalid_or_unreadable'
    if len(files) > MAX_DECLARATION_FILES:
        sources.append({'type': 'explicit_capability_manifest', 'status': 'truncated_budget',
                        'items_found': 0, 'files_skipped': len(files) - MAX_DECLARATION_FILES})
    return found, sources


def _declared_capabilities(items):
    interfaces = []
    for item in items:
        if item.get('kind') != 'mcp_tool':
            continue
        domains = [d for d in item.get('domains', []) if isinstance(d, str) and d in _DOMAINS]
        if not domains:
            continue
        enabled = item.get('tool_enabled') is True
        interfaces.append({
            'name': item.get('name', ''), 'description': item.get('description', ''),
            'provider': item.get('provider', ''), 'server': item.get('server', ''),
            'kind': item.get('kind', 'mcp_tool'), 'category': domains[0], 'categories': domains,
            'domains': domains, 'capability_id': item.get('id'), 'tool': item.get('target_tool'),
            'tools': [item.get('target_tool')] if item.get('target_tool') else [],
            'client_id': item.get('client_id'), 'client_online': item.get('client_online', False),
            'queue_eligible': enabled, 'source': 'registered_capability_catalog',
            'declaration_status': 'declared', 'verification_status': 'unverified',
            'discovery_only': True, 'published': False,
            'evidence': {
                'source': {'status': 'client_declared', 'type': 'capability_publish'},
                'configuration': {'status': 'harness_enabled' if enabled else 'harness_unavailable',
                                  'evidence': 'Tool registration status; no provider configuration was read.'},
                'callability': {'status': 'queue_eligible' if enabled else 'not_queue_eligible',
                               'evidence': 'Queue routing only; native provider callability is unverified.'},
                'actual_invocation': {'status': 'not_observed', 'evidence': 'A declaration or heartbeat is not a provider invocation.'},
            },
        })
    return interfaces


def _credentials(environ=None, associations=None):
    names = {name.casefold() for name in (os.environ if environ is None else environ)}
    by_name, manifest_associations = {}, set()
    for name, provider in _CREDENTIAL_HINTS.items():
        by_name[name] = {'name': name, 'provider': provider, 'provider_hint': provider,
                         'domains': [], 'tools': [], 'tool': '', 'target_tool': '',
                         'present': name.casefold() in names, 'value_included': False,
                         'capability_inferred': False, 'source': 'environment_variable_name_only',
                         'discovery_only': True}
    for association in associations or []:
        name = association['name']
        manifest_associations.add(name)
        item = by_name.setdefault(name, {'name': name, 'provider': association.get('provider', ''),
            'provider_hint': association.get('provider', ''), 'domains': [], 'tools': [],
            'tool': '', 'target_tool': '', 'present': name.casefold() in names,
            'value_included': False, 'capability_inferred': False,
            'source': 'explicit_manifest_variable_name', 'discovery_only': True})
        item['domains'] = sorted(set(item['domains']) | set(association.get('domains', [])))
        item['tools'] = sorted(set(item['tools']) | set(association.get('tools', [])))
    result = list(by_name.values())
    for item in result:
        item['next_step'] = '在对应工具设置中确认服务与权限；变量存在本身不代表接口已配置或可调用。'
        associated = item['name'] in manifest_associations
        item['evidence'] = {
            'source': {'status': 'manifest_declared' if associated else 'variable_name_candidate',
                       'type': 'explicit_public_manifest' if associated else 'environment_variable_name_only'},
            'configuration': {'status': 'required_by_manifest' if associated else 'not_established',
                              'evidence': 'The manifest declares this variable name; its value was not read.' if associated
                              else 'Only the environment variable name was checked.'},
            'callability': {'status': 'not_established',
                            'evidence': 'Variable presence does not establish provider configuration or callability.'},
            'actual_invocation': {'status': 'not_observed', 'evidence': 'No call was performed or inferred.'},
        }
    return result


def _merge_tool_scoped(items):
    """Merge duplicate source declarations while retaining compatibility singular fields."""
    merged, positions = [], {}
    for item in items:
        if item.get('capability_id'):
            key = ('capability', item['capability_id'])
        else:
            key = (item.get('source'), item.get('source_path') or item.get('path'),
                   item.get('name'), item.get('operation_id'))
        if key not in positions:
            positions[key] = len(merged)
            copy = dict(item)
            copy['tools'] = list(dict.fromkeys(item.get('tools', [])))
            merged.append(copy)
            continue
        current = merged[positions[key]]
        current['tools'] = sorted(set(current.get('tools', [])) | set(item.get('tools', [])))
        current['domains'] = sorted(set(current.get('domains', [])) | set(item.get('domains', [])))
        current['categories'] = current['domains']
        if not current.get('tool') and item.get('tool'):
            current['tool'] = item['tool']
        if not current.get('target_tool') and item.get('target_tool'):
            current['target_tool'] = item['target_tool']
    return merged


def discover(cfg, roots, capability_loader, environ=None):
    """Return compatible Skill suggestions and separated interface/credential evidence."""
    suggestions, sources, skipped, examined, plugin_interfaces = [], [], 0, 0, []
    normalized = list(roots or []) + configured_skill_roots(cfg)
    seen = set()
    for tool, root in normalized[:MAX_ROOTS]:
        try:
            root = Path(root)
            key = (config._key(str(root)), str(tool))
        except (TypeError, ValueError, OSError):
            skipped += 1
            continue
        if key in seen:
            continue
        seen.add(key)
        budget = max(0, MAX_ENTRIES - examined)
        if not budget or len(suggestions) >= MAX_SUGGESTIONS:
            break
        source, used, root_plugins = _scan_root(str(tool), root, budget, suggestions)
        examined += used
        skipped += source.get('items_skipped', 0)
        sources.append(source)
        plugin_interfaces.extend(root_plugins)

    declared, declaration_sources = _declared_interfaces(cfg)
    sources.extend(declaration_sources)
    env_associations = []
    for interface in declared:
        for name in interface.get('required_env_vars', []):
            env_associations.append({'name': name, 'provider': interface.get('provider', ''),
                                     'domains': interface.get('domains', []),
                                     'tools': interface.get('tools', [])})
    registered = []
    has_workspace = bool(isinstance(cfg, dict) and cfg.get('workspace_managed') is True and cfg.get('ai_root'))
    catalog_status = 'unavailable_no_workspace'
    if has_workspace:
        catalog_status = 'unavailable'
        try:
            catalog = capability_loader(cfg)
            if isinstance(catalog, dict):
                registered = catalog.get('items', [])
                catalog_status = 'scanned_readonly'
            else:
                catalog_status = 'unavailable_no_catalog'
        except Exception:
            # Discovery remains useful when a configured catalog is unavailable.
            catalog_status = 'unavailable'
    sources.append({'type': 'registered_capability_catalog', 'status': catalog_status,
                    'items_found': len(registered)})
    suggestions = _merge_tool_scoped(suggestions)
    interfaces = _merge_tool_scoped(_declared_capabilities(registered) + declared + plugin_interfaces)
    return {
        'suggestions': suggestions, 'interfaces': interfaces,
        'credentials': _credentials(environ, env_associations),
        'sources': sources, 'skipped': skipped,
        'truncated': (len(normalized) > MAX_ROOTS or examined >= MAX_ENTRIES or
                      len(suggestions) >= MAX_SUGGESTIONS or
                      any(source.get('status') == 'truncated_budget' for source in declaration_sources)),
        'published': False,
        'limitations': [
            '本机发现只读元数据，不执行 Skill、接口或模型调用。',
            '环境变量状态只表示变量名是否存在；不会读取或返回变量值，也不代表接口可调用。',
            '配置、可排队路由和实际调用证据分别显示；发现结果不会自动发布或进入派单。',
        ],
    }
