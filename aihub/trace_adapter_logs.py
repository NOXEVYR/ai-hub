"""Adapters for structured native JSONL. Only completed, identified file operations count as writes."""
import contextlib
from collections import Counter, defaultdict, deque
import hashlib
import io
import os
from pathlib import Path
import re
import stat

from . import config

from .trace_adapters import (Adapter, Result, SESSION_ID, MAX_IO, MAX_LINE, _db, _json, _path, _row,
                             _safe, _text_blocks)


def _records(stream, result, byte_limit=MAX_IO):
    used = 0
    while used < byte_limit:
        limit = min(MAX_LINE, byte_limit - used)
        raw = stream.readline(limit)
        if not raw:
            return
        used += len(raw)
        if len(raw) == limit and not raw.endswith(b'\n'):
            result.truncated = True
            while used < byte_limit and raw and not raw.endswith(b'\n'):
                raw = stream.readline(min(MAX_LINE, byte_limit - used))
                used += len(raw)
            continue
        record = _json(raw)
        if isinstance(record, dict):
            yield record
        elif raw.strip():
            result.truncated = True
    result.truncated = True


def _tail_records(path, result):
    with path.open('rb') as src:
        size = os.fstat(src.fileno()).st_size
        if size > MAX_IO:
            result.truncated = True
            src.seek(size - MAX_IO)
            src.readline(MAX_LINE)
        yield from _records(src, result)


def _children(base, pattern, home):
    if not _safe(base, home, directory=True):
        return
    parents = [base]
    parts = pattern.split('/')
    for part in parts[:-1]:
        parents = [path for parent in parents for path in parent.glob(part) if _safe(path, base, directory=True)]
    for parent in parents:
        for path in parent.glob(parts[-1]):
            if _safe(path, base):
                yield path


class Codex(Adapter):
    format = 'Codex state SQLite / rollout JSONL or JSONL.zst / dated workspace outputs'
    _uuid = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', re.I)

    def __init__(self, home):
        super().__init__(home)
        self._reset()

    def _reset(self):
        self._rollout_index = None
        self._outputs = {}
        self._counts = None
        self._native = {}
        self._first_user_available = False

    def _roots(self):
        return [self.home / '.codex' / n for n in ('sessions', 'archived_sessions')]

    def _allowed_log(self, path):
        return (path.name.startswith('rollout-') and path.name.endswith(('.jsonl', '.jsonl.zst'))
                and any(_safe(path, root) for root in self._roots()))

    def _rollout(self, stored, sid):
        normalized = _path(stored)
        if not normalized:
            return None
        candidate = Path(normalized)
        if not any(candidate.is_relative_to(root) for root in self._roots()):
            return None
        if self._allowed_log(candidate):
            return candidate
        # Codex may compress the same rollout while retaining its former .jsonl
        # spelling in state_5.sqlite. Prefer that exact sibling before relocation.
        if candidate.suffix == '.jsonl' and self._allowed_log(Path(str(candidate) + '.zst')):
            return Path(str(candidate) + '.zst')
        if not self._uuid.fullmatch(sid):
            return None
        if self._rollout_index is None:
            active, archived = self._roots()
            found = defaultdict(list)
            total = 0
            for base, pattern in ((active, '[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/rollout-*.jsonl*'),
                                  (archived, 'rollout-*.jsonl*')):
                for path in _children(base, pattern, self.home):
                    total += 1
                    if total > 50000:
                        # With a partial inventory, there is no proof of uniqueness.
                        self._rollout_index = {}
                        return None
                    match = self._uuid.search(path.name)
                    if match and path.name.endswith(('.jsonl', '.jsonl.zst')):
                        found[match.group().lower()].append(path)
            self._rollout_index = found
        paths = self._rollout_index.get(sid.lower(), [])
        return paths[0] if len(paths) == 1 else None

    def _workspace_outputs(self, cwd):
        known = self.home / 'Documents/Codex'
        directory = Path(cwd or '')
        try:
            parts = directory.relative_to(known).parts
        except ValueError:
            return [], '', False
        if len(parts) != 2 or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', parts[0]):
            return [], '', False
        if directory in self._outputs:
            return self._outputs[directory]
        base = directory / 'outputs'
        files, partial = [], False
        digest = hashlib.sha256()
        if _safe(base, known, directory=True):
            queue = deque([(base, 0)])
            visited, entries = 0, 0
            while queue and visited < 20 and entries < 320 and len(files) < 64:
                parent, depth = queue.popleft()
                visited += 1
                try:
                    if not _safe(parent, base, directory=True):
                        partial = True
                        continue
                    info = parent.lstat()
                    digest.update(f'{parent.relative_to(base)}:{info.st_ino}:{info.st_mtime_ns}\n'.encode())
                    with os.scandir(parent) as children:
                        batch = []
                        for item in children:
                            if len(batch) >= 100 or entries >= 320:
                                partial = True
                                break
                            entries += 1
                            batch.append(item.name)
                    for name in sorted(batch, key=str.casefold):
                        if (name.startswith('.') or name.casefold() in ('node_modules', '__pycache__', 'venv', 'site-packages')
                                or not _path(str(parent / name))):
                            continue
                        child = parent / name
                        info = child.lstat()
                        if config._is_reparse(info):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if depth < 3:
                                queue.append((child, depth + 1))
                            else:
                                partial = True
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                            if len(files) >= 64:
                                partial = True
                                break
                            digest.update(f'{child.relative_to(base)}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}\n'.encode())
                            files.append(str(child))
                except OSError:
                    partial = True
            partial |= bool(queue)
        result = files, digest.hexdigest()[:24], partial
        self._outputs[directory] = result
        return result

    def locations(self):
        base = self.home / '.codex'
        paths = [p for p in base.glob('state_*.sqlite') if re.fullmatch(r'state_\d+\.sqlite', p.name)
                 and _safe(p, base)] if _safe(base, self.home, directory=True) else []
        return [max(paths, key=lambda p: int(p.stem.split('_')[1]))] if paths else [base / 'state_5.sqlite']

    def _rows(self, ident=None):
        path, = self.locations()
        if not _safe(path, self.home):
            return
        with _db(path, self.home) as con:
            cols = {r[1] for r in con.execute('PRAGMA table_info(threads)')}
            if not {'id', 'title', 'cwd', 'rollout_path', 'updated_at'} <= cols:
                return
            self._first_user_available = 'first_user_message' in cols
            if self._counts is None:
                self._counts = Counter()
                for cwd, count in con.execute('SELECT cwd,count(*) FROM threads GROUP BY cwd'):
                    self._counts[os.path.normcase(_path(cwd) or '')] += count
            title = "coalesce(nullif(name,''),title)" if 'name' in cols else 'title'
            stamp = 'coalesce(updated_at_ms,updated_at)' if 'updated_at_ms' in cols else 'updated_at'
            sql = 'SELECT id,substr(' + title + ',1,180),cwd,' + stamp + ',rollout_path FROM threads'
            for sid, label, cwd, updated, src in con.execute(sql + (' WHERE id=?' if ident else ''), (ident,) if ident else ()):
                if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
                    continue
                source = self._rollout(src, sid)
                info = None
                if source is not None:
                    try:
                        info = source.stat()
                    except OSError:
                        source = None
                files, version, partial = self._workspace_outputs(_path(cwd))
                shared = self._counts[os.path.normcase(_path(cwd) or '')] > 1
                self._native[sid] = (source, files, shared, partial)
                signature = (f'codex-v2:{updated}:{info.st_ino if info else 0}:{info.st_size if info else 0}:'
                             f'{info.st_mtime_ns if info else 0}:{version}:{int(shared)}')
                yield _row(sid, label, cwd, updated, source or _path(src) or '', 'native' if source else 'metadata_only', signature)

    def sessions(self):
        self._reset()
        return self._rows()

    def find(self, ident):
        return next(self._rows(ident), None)

    def _read(self, row, result):
        sid = row['id']
        current = self.find(sid)
        if not current:
            raise FileNotFoundError('原生会话不存在。')
        path, files, shared, partial = self._native[sid]
        if path is not None and row.get('revision') != current['revision']:
            raise OSError('会话版本已变化，请重新同步。')
        self._read_rollout(path, row, result)
        result.truncated |= partial
        if not result.messages and self._first_user_available:
            database, = self.locations()
            with _db(database, self.home) as con:
                record = con.execute('SELECT substr(first_user_message,1,16001) FROM threads WHERE id=?', (sid,)).fetchone()
            if record:
                result.message('user', record[0])
                if result.messages:
                    result.state = 'first_user_excerpt'
                    result.truncated = True
        for name in files:
            result.file(name, 'workspace_related', 'codex_outputs_shared_workspace' if shared else 'codex_workspace_outputs')

    def _read_rollout(self, path, row, result, *, extended=False):
        sid = row['id']
        if path is None or not self._allowed_log(path):
            result.state = 'metadata_only'
            return
        result.state = 'native'
        # Both a moved log and a compressed sibling have to establish their
        # native identity. A coincidentally named file cannot become a transcript.
        with path.open('rb') as fp:
            compressed = path.name.endswith('.jsonl.zst')
            with DSH._stream(fp) if compressed else contextlib.nullcontext(fp) as stream:
                if stream is None:
                    result.state = 'codec_unavailable'
                    return
                raw = stream.readline(MAX_LINE)
                header = _json(raw)
                if not isinstance(header, dict) or header.get('type') != 'session_meta' or (header.get('payload') or {}).get('id') != sid:
                    result.state = 'metadata_only'
                    result.truncated = True
                    return
                if compressed:
                    budget = 40 * 1024 * 1024 if extended else MAX_IO
                    self._messages(_records(stream, result, budget-len(raw)), row, result)
                else:
                    available = MAX_IO - len(raw)
                    size = os.fstat(fp.fileno()).st_size
                    if size > MAX_IO:
                        fp.seek(size - available)
                        fp.readline(min(MAX_LINE, available))
                        result.truncated = True
                    self._messages(_records(fp, result, available), row, result)
        if compressed and not extended and result.truncated and not result.messages:
            # A few archives begin with a multi-megabyte context/image record.
            # Retry only empty excerpts. Individual oversized lines are still skipped.
            self._read_rollout(path, row, result, extended=True)

    def _messages(self, records, row, result):
        # Modern records have completed UI messages. Older releases use event
        # messages or response items. Choose one representation to avoid duplicates.
        fallback = Result(row, result.message_limit, result.char_limit)
        seen = set()
        for record in records:
            payload = record.get('payload')
            if not isinstance(payload, dict):
                continue
            family, kind = record.get('type'), payload.get('type')
            if family == 'event_msg':
                item = payload.get('item') if kind == 'item_completed' else None
                if isinstance(item, dict):
                    typ = item.get('type')
                    if typ in ('AgentMessage', 'UserMessage'):
                        ident = item.get('id')
                        if ident and ident in seen:
                            continue
                        if ident:
                            seen.add(ident)
                        result.message('user' if typ == 'UserMessage' else 'assistant', _text_blocks(item.get('content')))
                    elif typ == 'FileChange' and item.get('status') == 'completed':
                        self._changes(item.get('changes'), result)
                elif kind == 'patch_apply_end' and payload.get('success') is True:
                    self._changes(payload.get('changes'), result)
                elif kind in ('user_message', 'agent_message') and isinstance(payload.get('message'), str):
                    fallback.message('user' if kind == 'user_message' else 'assistant', payload['message'])
            elif family == 'response_item' and kind == 'message' and payload.get('role') in ('assistant', 'user'):
                text = _text_blocks(payload.get('content'))
                if not fallback.messages or fallback.messages[-1] != {'role': payload['role'], 'text': text.strip()}:
                    fallback.message(payload['role'], text)
        if not result.messages:
            result.messages = fallback.messages
            result.files.update(fallback.files)
            result.truncated |= fallback.truncated

    @staticmethod
    def _changes(changes, result):
        if not isinstance(changes, dict):
            return
        for path, change in changes.items():
            if isinstance(change, dict):
                typ = str(change.get('type') or '').lower()
                if typ in ('add', 'create', 'update', 'edit'):
                    result.file(path, 'created' if typ in ('add', 'create') else 'edited', 'codex_completed_file_change')


class DSH(Adapter):
    format = 'DeepSeek Harness session.v4.jsonl.zstd + session_projcache'

    def locations(self):
        return [self.home / '.dsh/sessions']

    @staticmethod
    @contextlib.contextmanager
    def _stream(handle):
        try:
            from compression import zstd
        except ImportError:
            try:
                import zstandard
            except ImportError:
                yield None
                return
            try:
                with io.BufferedReader(zstandard.ZstdDecompressor(max_window_size=64 * 1024 * 1024).stream_reader(handle)) as stream:
                    yield stream
            except zstandard.ZstdError as exc:
                raise OSError('原生历史压缩帧无法读取。') from exc
        else:
            with zstd.open(handle, 'rb') as stream:
                yield stream

    def _row(self, path):
        sid = path.parent.name
        if not SESSION_ID.fullmatch(sid):
            return None
        cache_root = self.home / '.dsh/storages/session_projcache/sessions'
        cache = cache_root / (sid + '.json')
        title, cwd, cache_revision = '', '', 0
        if _safe(cache, cache_root) and cache.stat().st_size < 512 * 1024:
            with cache.open('rb') as handle:
                obj = _json(handle.read(512 * 1024), {}) or {}
            data = obj.get('record') or {}
            rows = data.get('rows') or {}
            cwd = (data.get('identity') or {}).get('cwd') or (rows.get('workingDirectory') or {}).get('val')
            title = (rows.get('title') or {}).get('val')
            cache_revision = cache.stat().st_mtime_ns
        if not cwd:
            try:
                with path.open('rb') as fp, self._stream(fp) as stream:
                    if stream is not None:
                        header = _json(stream.readline(65536), {}) or {}
                        if header.get('type') == 'session' and header.get('id') == sid:
                            cwd = header.get('cwd')
            except (OSError, EOFError, ValueError):
                pass
        st = path.stat()
        return _row(sid, title, cwd, st.st_mtime, path, 'native', f'{st.st_size}:{st.st_mtime_ns}:{cache_revision}')

    def sessions(self):
        for path in _children(self.locations()[0], '*/*/session.v4.jsonl.zstd', self.home):
            row = self._row(path)
            if row:
                yield row

    def find(self, ident):
        for path in _children(self.locations()[0], '*/' + ident + '/session.v4.jsonl.zstd', self.home):
            return self._row(path)
        return None

    def _read(self, row, result):
        path = Path(row['source_ref'])
        if (path.name != 'session.v4.jsonl.zstd' or path.parent.name != row['id']
                or not _safe(path, self.locations()[0])):
            raise FileNotFoundError('原生会话路径无效。')
        with path.open('rb') as fp, self._stream(fp) as stream:
            if stream is None:
                result.state = 'codec_unavailable'
                return
            if stream is not None:
                events = iter(_records(stream, result))
                header = next(events, {})
                if header.get('type') != 'session' or header.get('id') != row['id']:
                    raise FileNotFoundError('原生会话身份不匹配。')
                pending = {}
                for event in events:
                    typ, data = event.get('type'), event.get('data')
                    if not isinstance(data, dict):
                        continue
                    if typ == 'user/message':
                        source = data.get('source') or {}
                        kind = source.get('kind') if isinstance(source, dict) else source
                        if kind in (None, 'user', 'user-approval') and data.get('role', 'user') == 'user':
                            result.message('user', _text_blocks(data.get('content')))
                    elif typ == 'assistant/message':
                        msg = data.get('message') or {}
                        result.message('assistant', _text_blocks(msg.get('content')))
                    elif typ == 'tool/call' and isinstance(data.get('name'), str):
                        name = data['name'].lower()
                        args = _json(data.get('arguments'), {}) if isinstance(data.get('arguments'), str) else data.get('arguments')
                        if name in ('write', 'edit', 'write_file', 'edit_file', 'create_file') and isinstance(args, dict):
                            pending[data.get('callId')] = (name, args.get('file_path') or args.get('path'))
                    elif typ == 'tool/result':
                        msg = data.get('message') or {}
                        pair = pending.pop(msg.get('toolCallId'), None)
                        if pair and msg.get('isError') is False:
                            name, file_path = pair
                            relation = 'created' if name == 'create_file' else 'edited' if 'edit' in name else 'written'
                            result.file(file_path, relation, 'dsh_matched_successful_file_tool')


class Claude(Adapter):
    format = 'Claude Code projects/<project>/<sessionId>.jsonl'

    def locations(self):
        return [self.home / '.claude/projects']

    def _row(self, path):
        sid = path.stem
        if not SESSION_ID.fullmatch(sid):
            return None
        cwd, title = '', ''
        with path.open('rb') as fp:
            for _ in range(20):
                line = fp.readline(16384)
                if not line or len(line) == 16384 and not line.endswith(b'\n'):
                    break
                event = _json(line, {}) or {}
                if event.get('sessionId') != sid:
                    continue
                cwd = event.get('cwd') or cwd
                if event.get('type') == 'custom-title':
                    title = event.get('customTitle') or title
                elif event.get('type') == 'user' and not title:
                    title = _text_blocks((event.get('message') or {}).get('content'))
                if cwd and title:
                    break
        st = path.stat()
        return _row(sid, title, cwd, st.st_mtime, path, 'native', f'{st.st_size}:{st.st_mtime_ns}')

    def sessions(self):
        for path in _children(self.locations()[0], '*/*.jsonl', self.home):
            row = self._row(path)
            if row:
                yield row

    def find(self, ident):
        for path in _children(self.locations()[0], '*/' + ident + '.jsonl', self.home):
            return self._row(path)
        return None

    def _read(self, row, result):
        path = Path(row['source_ref'])
        if path.name != row['id'] + '.jsonl' or not _safe(path, self.locations()[0]):
            raise FileNotFoundError('原生会话路径无效。')
        pending = {}
        for event in _tail_records(path, result):
            if event.get('sessionId') != row['id'] or event.get('isMeta') or event.get('isSidechain'):
                continue
            msg = event.get('message') or {}
            if not isinstance(msg, dict) or msg.get('role') not in ('user', 'assistant'):
                continue
            blocks = msg.get('content')
            result.message(msg['role'], _text_blocks(blocks))
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if (msg['role'] == 'assistant' and block.get('type') == 'tool_use' and block.get('id')
                        and block.get('name') in ('Write', 'Edit', 'MultiEdit')):
                    params = block.get('input') or {}
                    pending[block['id']] = (block['name'], _path(params.get('file_path'), row['cwd']))
                elif msg['role'] == 'user' and block.get('type') == 'tool_result':
                    pair = pending.pop(block.get('tool_use_id'), None)
                    evidence = event.get('toolUseResult')
                    if not pair or block.get('is_error') or not isinstance(evidence, dict):
                        continue
                    target = _path(evidence.get('filePath') or evidence.get('file_path'), row['cwd'])
                    if target and pair[1] and os.path.normcase(target) == os.path.normcase(pair[1]):
                        mode = evidence.get('type')
                        relation = 'created' if mode == 'create' else 'edited' if mode == 'update' or pair[0] != 'Write' else 'written'
                        result.file(target, relation, 'claude_matched_tool_result')
