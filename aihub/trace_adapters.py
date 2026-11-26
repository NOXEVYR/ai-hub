"""Read native local history without executing native tools or opening referenced user files."""
import contextlib
import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from urllib.parse import unquote, urlparse

from . import config

MAX_IO = 8 * 1024 * 1024
MAX_LINE = 768 * 1024
MAX_FILES = 128
SESSION_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$')
SECRET = re.compile(r'(?i)(\b(?:authorization\s*[:=]\s*bearer|bearer|(?:[a-z][a-z0-9_]*_)?(?:api[_ -]?key|access[_ -]?token|secret|password)\s*["\']?\s*[:=])\s*["\']?)[^\s,"\'}]{5,}|\b(?:sk-[a-zA-Z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,})')
PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)', re.S)
MENTION = re.compile(r'\[[^\]\n]{0,180}\]\((?:<([^>\n]{1,1000})>|([^\s()\n]{1,1000}))\)|\x60([^\x60\n]{1,600})\x60')
SENSITIVE = re.compile(r'(?i)(?:^|[\\/])(?:\.ssh|\.aws|\.gnupg|\.env(?:\.[^\\/]+)?|credentials?(?:\.[^\\/]+)?|auth\.json|tokens?\.json|id_rsa|id_ed25519|cookies)(?:[\\/]|$)')


def _safe(path, base, *, directory=False):
    """Reject links before opening a known native storage location."""
    path, base = Path(path), Path(base)
    try:
        if not config._within(path, base) or '..' in path.parts:
            return False
        config._check_ancestors(str(path.parent))
        info = path.lstat()
        return not config._is_reparse(info) and (stat.S_ISDIR(info.st_mode) if directory
                                                  else stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    except (ValueError, OSError, TypeError):
        return False


@contextlib.contextmanager
def _db(path, base):
    if not _safe(path, base) or any(os.path.lexists(str(path) + s) and not _safe(str(path) + s, base)
                                     for s in ('-wal', '-shm', '-journal')):
        raise FileNotFoundError('原生会话库不可用。')
    with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as con:
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA query_only=ON')
        yield con


def _json(raw, default=None):
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError, TypeError):
        return default


def _time(value):
    try:
        if isinstance(value, (float, int)) or str(value).isdigit():
            num = float(value)
            return num / 1000 if num > 10**11 else num
        return datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return 0


def _clean(value, limit):
    if not isinstance(value, str):
        return ''
    return SECRET.sub(lambda m: (m.group(1) or '') + '[已隐藏]', PRIVATE_KEY.sub('[已隐藏]', value[:limit])).strip()


def _path(value, cwd=None):
    if not isinstance(value, str) or len(value) > 2048 or not value or any(ord(c) < 32 for c in value):
        return None
    if value.startswith('file:'):
        uri = urlparse(value)
        if uri.netloc not in ('', 'localhost'):
            return None
        value = unquote(uri.path)
        if re.match(r'^/[a-z]:/', value, re.I):
            value = value[1:]
    if value.startswith('\\\\?\\') and re.match(r'^[a-z]:', value[4:], re.I):
        value = value[4:]
    if value.startswith(('\\\\', '//')) or re.match(r'^[a-z]{2,}://', value, re.I):
        return None
    if not os.path.isabs(value):
        if not cwd:
            return None
        value = os.path.join(cwd, value)
    value = os.path.normpath(value)
    return None if SENSITIVE.search(value) else value


def _text_blocks(blocks):
    if isinstance(blocks, str):
        return blocks
    return '\n'.join(b['text'] for b in blocks or [] if isinstance(b, dict)
                     and str(b.get('type', '')).lower() in {'text', 'input_text', 'output_text', 'inputtext', 'outputtext'}
                     and isinstance(b.get('text'), str)) if isinstance(blocks, list) else ''


def _row(identifier, title, cwd, updated, source, state, revision):
    return {'id': identifier, 'title': _clean(title, 180) or identifier, 'cwd': _path(cwd) or '',
            'updated_at': _time(updated), 'source_ref': str(source), 'transcript_state': state,
            'revision': str(revision)}


class Result:
    def __init__(self, row, max_messages, max_chars):
        self.row = row
        self.message_limit = min(300, max(0, int(max_messages)))
        self.char_limit = min(120000, max(0, int(max_chars)))
        self.messages = []
        self.files = {}
        self.chars = 0
        self.truncated = False
        self.state = row['transcript_state']
        self.file(row.get('cwd'), 'workspace_related', 'native_working_directory')

    def file(self, path, relation, evidence):
        path = _path(path, self.row.get('cwd'))
        if not path:
            return
        if len(self.files) >= MAX_FILES:
            self.truncated = True
            return
        self.files[(os.path.normcase(path), relation)] = {'path': path, 'relation': relation, 'evidence': evidence}

    def message(self, role, text):
        if role not in ('user', 'assistant') or not isinstance(text, str) or not text.strip():
            return
        if text.lstrip().startswith(('<environment_context>', '<permissions instructions>', '<system-reminder>',
                                     '<subagent_notification>', '<INSTRUCTIONS>\n# Local Codex')):
            return
        for match in MENTION.finditer(text[:32000]):
            candidate = next((v for v in match.groups() if v), '')
            if Path(candidate).suffix and (os.path.isabs(candidate) or candidate.startswith(('file:', './', '.\\', '../'))):
                self.file(candidate, 'mentioned', 'message_path_reference')
        limit = min(self.char_limit, 16000)
        if len(text) > limit:
            self.truncated = True
        text = _clean(text, limit)
        if not text:
            return
        self.messages.append({'role': role, 'text': text})
        self.chars += len(text)
        while len(self.messages) > self.message_limit or self.chars > self.char_limit:
            self.chars -= len(self.messages.pop(0)['text'])
            self.truncated = True

    def finish(self):
        return {'id': self.row['id'], 'messages': self.messages, 'files': list(self.files.values()),
                'truncated': self.truncated, 'transcript_state': self.state}


class Adapter:
    def __init__(self, home):
        self.home = Path(home).absolute()

    def probe(self):
        return {'available': any(_safe(p, self.home, directory=True) if p.is_dir() else _safe(p, self.home)
                                 for p in self.locations()), 'format': self.format,
                'locations': [str(p) for p in self.locations()]}

    def read(self, session_id, *, max_messages=80, max_chars=24000):
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise ValueError('原生会话标识无效。')
        row = self.find(session_id)
        if row is None:
            raise ValueError('原生会话不存在。')
        return self.read_record(row, max_messages=max_messages, max_chars=max_chars)

    def read_record(self, row, *, max_messages=80, max_chars=24000):
        identifier = row.get('id')
        if not isinstance(identifier, str) or not SESSION_ID.fullmatch(identifier):
            raise ValueError('原生会话标识无效。')
        result = Result(row, max_messages, max_chars)
        try:
            self._read(row, result)
        except (OSError, sqlite3.Error, UnicodeError, EOFError):
            result.state = 'unavailable'
            result.truncated = True
        return result.finish()


class ZCode(Adapter):
    format = 'ZCode CLI SQLite session/message/part'

    def locations(self):
        return [self.home / '.zcode/cli/db/db.sqlite']

    def _rows(self, ident=None):
        path, = self.locations()
        if not _safe(path, self.home):
            return
        with _db(path, self.home) as con:
            cols = {r[1] for r in con.execute('PRAGMA table_info(session)')}
            if not {'id', 'title', 'directory', 'time_updated'} <= cols:
                return
            filters = ['time_deleted IS NULL'] if 'time_deleted' in cols else []
            if ident:
                filters.append('id=?')
            sql = 'SELECT id,substr(title,1,180),directory,time_updated FROM session'
            if filters:
                sql += ' WHERE ' + ' AND '.join(filters)
            for sid, title, cwd, stamp in con.execute(sql, (ident,) if ident else ()):
                if isinstance(sid, str) and SESSION_ID.fullmatch(sid):
                    yield _row(sid, title, cwd, stamp, path, 'native', stamp)

    def sessions(self):
        return self._rows()

    def find(self, ident):
        return next(self._rows(ident), None)

    def _read(self, row, result):
        path, = self.locations()
        # Extract whitelisted fields inside SQLite. Tool output, command lines,
        # reasoning, file contents and serialized HTTP requests never leave the database.
        with _db(path, self.home) as con:
            session_cols = {r[1] for r in con.execute('PRAGMA table_info(session)')}
            active = ' AND time_deleted IS NULL' if 'time_deleted' in session_cols else ''
            if not con.execute('SELECT 1 FROM session WHERE id=?' + active, (row['id'],)).fetchone():
                raise FileNotFoundError('原生会话已移除。')
            # Walk the message and part indexes newest-first. Limit raw JSON bytes
            # before extracting fields. A single huge tool response cannot monopolize
            # enumeration of the next session.
            sql = """WITH recent AS MATERIALIZED
                (SELECT CASE WHEN length(CAST(data AS BLOB))<=? AND json_valid(data) THEN data END AS doc,
                        length(CAST(data AS BLOB)) AS bytes FROM part WHERE session_id=? AND message_id=?
                 ORDER BY sequence DESC,id DESC LIMIT ?)
                SELECT bytes,doc IS NULL AS skipped,json_extract(doc,'$.type') AS kind,
                CASE WHEN json_extract(doc,'$.type')='text' AND NOT coalesce(json_extract(doc,'$.synthetic'),0)
                     AND NOT coalesce(json_extract(doc,'$.ignored'),0)
                     THEN substr(json_extract(doc,'$.text'),1,16001) END AS body,
                json_extract(doc,'$.tool') AS tool,json_extract(doc,'$.state.status') AS status,
                json_extract(doc,'$.state.input.file_path') AS input_path,
                json_extract(doc,'$.state.metadata.readFileState.path') AS confirmed_path,
                json_extract(doc,'$.source.path') AS attachment FROM recent"""
            records = []
            total, scanned = 0, 0
            msgs = con.execute("""SELECT id,CASE WHEN length(CAST(data AS BLOB))<=? AND json_valid(data)
                                 THEN json_extract(data,'$.role') END FROM message
                                 WHERE session_id=? ORDER BY sequence DESC LIMIT 301""", (MAX_LINE, row['id']))
            for n, (message_id, role) in enumerate(msgs):
                if n >= 300 or scanned >= 1000 or total >= MAX_IO:
                    result.truncated = True
                    break
                if role not in ('assistant', 'user'):
                    continue
                for item in con.execute(sql, (min(MAX_LINE, MAX_IO - total), row['id'], message_id, 1001 - scanned)):
                    scanned += 1
                    if item['skipped']:
                        result.truncated = True
                        continue
                    total += item['bytes']
                    records.append((role, item))
                    if total >= MAX_IO or scanned >= 1000:
                        result.truncated = True
                        break
            for role, item in reversed(records):
                if item['kind'] == 'text':
                    result.message(role, item['body'])
                elif item['kind'] == 'file':
                    result.file(item['attachment'], 'mentioned', 'native_attachment')
                elif item['status'] == 'completed' and item['tool'] in ('Edit', 'Write', 'MultiEdit'):
                    target = _path(item['input_path'], row['cwd'])
                    observed = _path(item['confirmed_path'], row['cwd'])
                    if target and observed and os.path.normcase(target) == os.path.normcase(observed):
                        result.file(target, 'written' if item['tool'] == 'Write' else 'edited',
                                    'zcode_completed_tool_and_file_state')


def adapters(home=None):
    from .trace_adapter_logs import Claude, Codex, DSH
    from .trace_adapter_antigravity import Antigravity
    root = Path(home or Path.home())
    classes = {'codex': Codex, 'antigravity': Antigravity, 'dsh': DSH, 'zcode': ZCode, 'claude-code': Claude}
    return {name: cls(root) for name, cls in classes.items()}
