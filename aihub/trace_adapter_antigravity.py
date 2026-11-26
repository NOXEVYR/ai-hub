"""Read Antigravity's native visible transcript, search excerpts and session artifacts."""
from pathlib import Path
import re
import sqlite3

from .trace_adapters import Adapter, MAX_IO, SESSION_ID, _db, _json, _path, _row, _safe
from .trace_adapter_logs import _tail_records

IDS = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$', re.I)
ARTIFACT_EXT = {'.md', '.txt', '.pdf', '.html', '.htm', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.mp4', '.webm',
                '.wav', '.mp3', '.glb', '.gltf', '.blend', '.pptx', '.docx', '.xlsx', '.zip', '.svg'}


def _workspace(uris):
    if not isinstance(uris, str) or not uris:
        return ''
    candidates = _json(uris)
    if not isinstance(candidates, list):
        candidates = uris.splitlines()
    return next((p for p in (_path(v) for v in candidates) if p), '')


class Antigravity(Adapter):
    format = 'Antigravity transcript.jsonl / conversation_summaries.db / brain'

    def locations(self):
        return [self.home / '.gemini' / n for n in ('antigravity', 'antigravity-ide')]

    @staticmethod
    def _transcript(root, sid):
        folder = root / 'brain' / sid / '.system_generated/logs'
        for name in ('transcript.jsonl', 'transcript_full.jsonl'):
            path = folder / name
            if _safe(path, root):
                return path
        return None

    def _rows(self, ident=None):
        seen = set()
        for root in self.locations():
            if not _safe(root, self.home, directory=True):
                continue
            db = root / 'conversation_summaries.db'
            meta = {}
            if _safe(db, root):
                with _db(db, root) as con:
                    cols = {r[1] for r in con.execute('PRAGMA table_info(conversation_summaries)')}
                    if {'conversation_id', 'title', 'last_modified_time'} <= cols:
                        uris = 'workspace_uris' if 'workspace_uris' in cols else "''"
                        sql = 'SELECT conversation_id,substr(title,1,180),' + uris + ',last_modified_time FROM conversation_summaries'
                        for sid, title, ws, stamp in con.execute(sql + (' WHERE conversation_id=?' if ident else ''), (ident,) if ident else ()):
                            if isinstance(sid, str) and SESSION_ID.fullmatch(sid):
                                meta[sid] = (title, _workspace(ws), stamp)
            brain = root / 'brain'
            extra = [brain / ident] if ident else (brain.iterdir() if _safe(brain, root, directory=True) else [])
            for folder in extra:
                if IDS.fullmatch(folder.name) and _safe(folder, root, directory=True):
                    meta.setdefault(folder.name, ('', '', folder.stat().st_mtime))
            for sid, (title, cwd, updated) in meta.items():
                if sid in seen:
                    continue
                seen.add(sid)
                transcript = self._transcript(root, sid)
                folder = brain / sid
                src = transcript or (db if _safe(db, root) else folder)
                folder_stamp = folder.stat().st_mtime_ns if _safe(folder, root, directory=True) else 0
                stat = transcript.stat() if transcript else None
                state = 'native' if transcript else 'indexed_excerpts' if src == db else 'metadata_only'
                yield _row(sid, title, cwd, updated, src, state,
                           f'{updated}:{folder_stamp}:{stat.st_mtime_ns if stat else 0}:{stat.st_size if stat else 0}')

    def sessions(self):
        return self._rows()

    def find(self, ident):
        return next(self._rows(ident), None)

    @staticmethod
    def _excerpts(db, root, sid, result):
        """The role columns aggregate messages; split only at native byte offsets."""
        with _db(db, root) as con:
            required = {'conversation_id', 'chunk_index', 'user_body', 'agent_body',
                        'user_step_byte_offsets', 'agent_step_byte_offsets'}
            if not required <= {r[1] for r in con.execute('PRAGMA table_info(conversations_fts)')}:
                result.state = 'metadata_only'
                return
            rows = con.execute("""SELECT substr(CAST(user_body AS BLOB),1,131072),substr(CAST(agent_body AS BLOB),1,131072),
                substr(user_step_byte_offsets,1,65536),substr(agent_step_byte_offsets,1,65536)
                FROM conversations_fts WHERE conversation_id=? ORDER BY chunk_index DESC LIMIT 17""", (sid,)).fetchall()
            result.truncated = bool(rows)  # Search excerpts may omit entire steps.
            result.state = 'indexed_excerpts' if rows else 'metadata_only'
            for user, agent, user_offsets, agent_offsets in reversed(rows[:16]):
                messages = []
                for role, body, offsets in [('user', user, user_offsets), ('assistant', agent, agent_offsets)]:
                    if not body:
                        continue
                    marks = _json(offsets, [])
                    if not isinstance(marks, list):
                        continue
                    marks = [p for p in marks if isinstance(p, list) and len(p) == 2 and all(type(n) is int for n in p)
                             and 0 <= p[1] < len(body)]
                    for i, (step, start) in enumerate(marks):
                        end = marks[i + 1][1] if i + 1 < len(marks) else len(body)
                        if end > start:
                            messages.append((step, role, body[start:end].decode('utf-8', 'replace')))
                for _, role, text in sorted(messages, key=lambda m: m[0]):
                    result.message(role, text)

    def _read(self, row, result):
        current = self.find(row['id'])
        if not current:
            raise FileNotFoundError('原生会话不存在。')
        sid = current['id']
        src = Path(current['source_ref'])
        root = next((p for p in self.locations() if p in src.parents), None)
        if not root:
            raise FileNotFoundError('原生会话位置无效。')
        transcript = self._transcript(root, sid)
        if transcript:
            result.state = 'native'
            for record in _tail_records(transcript, result):
                typ, source = record.get('type'), record.get('source')
                if typ == 'USER_INPUT' and source == 'USER_EXPLICIT':
                    result.message('user', record.get('content'))
                elif typ == 'PLANNER_RESPONSE' and source == 'MODEL':
                    result.message('assistant', record.get('content'))
                if record.get('truncated_fields') and 'content' in record['truncated_fields']:
                    result.truncated = True
        elif _safe(root / 'conversation_summaries.db', root):
            self._excerpts(root / 'conversation_summaries.db', root, sid, result)
        folder = root / 'brain' / sid
        if _safe(folder, root, directory=True):
            for n, artifact in enumerate(folder.iterdir()):
                if n >= 256:
                    result.truncated = True
                    break
                if not artifact.name.startswith('.') and artifact.suffix.lower() in ARTIFACT_EXT and _safe(artifact, folder):
                    result.file(str(artifact), 'artifact', 'antigravity_session_artifact_directory')
