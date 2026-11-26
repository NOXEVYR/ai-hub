import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from aihub import trace_adapters as trace
from aihub import trace_adapter_logs as logs


class NativeTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.cwd = self.home / 'work'
        self.cwd.mkdir()
        self.adapters = trace.adapters(self.home)

    def jsonl(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b''.join(json.dumps(r, ensure_ascii=False).encode() + b'\n' for r in records))

    def db(self, path, schema):
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        con.executescript(schema)
        return con

    def test_codex_native_events_prove_add_and_update_only_on_success(self):
        sid = 'native-one'
        path = self.home / '.codex/sessions/2026/11/rollout-native-one.jsonl'
        def ev(item):
            return {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': item}}
        self.jsonl(path, [{'type': 'session_meta', 'payload': {'id': sid}},
                          ev({'type': 'UserMessage', 'id': 'u', 'content': [{'type': 'text', 'text': 'start'}]}),
                          {'type': 'response_item', 'payload': {'type': 'message', 'role': 'developer', 'content': 'PRIVATE-INSTRUCTIONS'}},
                          ev({'type': 'AgentMessage', 'id': 'a', 'content': [{'type': 'text', 'text': 'done'}]}),
                          ev({'type': 'FileChange', 'status': 'completed', 'changes': {'new.md': {'type': 'add'}, 'old.md': {'type': 'update'}}}),
                          ev({'type': 'FileChange', 'status': 'failed', 'changes': {'invented.md': {'type': 'add'}}})])
        db = self.db(self.home / '.codex/state_5.sqlite',
                     'CREATE TABLE threads(id TEXT,title TEXT,cwd TEXT,updated_at INTEGER,rollout_path TEXT)')
        db.execute('INSERT INTO threads VALUES(?,?,?,?,?)', (sid, 'Title', str(self.cwd), 1500000000, str(path)))
        db.commit()
        adapter = self.adapters['codex']
        row, = adapter.sessions()
        with mock.patch.object(adapter, 'sessions', side_effect=AssertionError('enumerated')):
            result = adapter.read_record(row)
        self.assertEqual([i['text'] for i in result['messages']], ['start', 'done'])
        relations = {Path(f['path']).name: f['relation'] for f in result['files']}
        self.assertEqual(relations, {'work': 'workspace_related', 'new.md': 'created', 'old.md': 'edited'})
        db.execute('UPDATE threads SET rollout_path=?', (str(self.home / 'private.jsonl'),))
        db.commit()
        self.assertEqual(adapter.read_record(row)['transcript_state'], 'metadata_only')

    def test_codex_compressed_rename_relocation_and_native_identity(self):
        try:
            import zstandard
        except ImportError:
            self.skipTest('optional native zstd decoder unavailable')
        ids = [f'00000000-0000-4000-8000-00000000000{n}' for n in range(1,5)]
        active = self.home / '.codex/sessions/2026/11/25'
        archived = self.home / '.codex/archived_sessions'
        active.mkdir(parents=True)
        archived.mkdir()
        db = self.db(self.home / '.codex/state_5.sqlite',
                     'CREATE TABLE threads(id TEXT PRIMARY KEY,title TEXT,cwd TEXT,updated_at INTEGER,rollout_path TEXT)')
        for n, sid in enumerate(ids):
            name = 'rollout-2026-11-25-' + sid + '.jsonl'
            stored = active / name
            physical = Path(str(stored)+'.zst') if n in (0,3) else archived / (name+'.zst')
            if n == 2:
                stored = physical
            records = [{'type':'session_meta','payload':{'id':ids[0] if n==3 else sid}},
                       {'type':'event_msg','payload':{'type':'user_message','message':'original question'}}]
            physical.write_bytes(zstandard.ZstdCompressor().compress(b'\n'.join(json.dumps(r).encode() for r in records)+b'\n'))
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?)',(sid,'t',str(self.cwd),1600000000,str(stored)))
        db.commit()
        adapter = self.adapters['codex']
        rows = list(adapter.sessions())
        self.assertEqual(len(rows),4)
        for row in rows[:3]:
            result = adapter.read_record(row)
            self.assertEqual(result['messages'],[{'role':'user','text':'original question'}])
            self.assertTrue(row['revision'].startswith('codex-v2:'))
        self.assertEqual(adapter.read_record(rows[3])['transcript_state'],'metadata_only')
        self.assertTrue(adapter._rollout_index)
        # A very large internal context can precede the first visible message.
        src = Path(rows[0]['source_ref'])
        body = [{'type':'session_meta','payload':{'id':ids[0]}},
                {'type':'response_item','payload':{'type':'message','role':'developer','content':'x'*10000}},
                {'type':'event_msg','payload':{'type':'user_message','message':'after large context'}}]
        src.write_bytes(zstandard.ZstdCompressor().compress(b'\n'.join(json.dumps(e).encode() for e in body)+b'\n'))
        with mock.patch.object(logs,'MAX_IO',1024), mock.patch.object(logs,'MAX_LINE',512):
            self.assertEqual(adapter.read(ids[0])['messages'],[{'role':'user','text':'after large context'}])

    def test_codex_only_bounded_dated_outputs_report_workspace_evidence_and_revision(self):
        work = self.home/'Documents/Codex/2026-11-25/task'
        out = work/'outputs'
        out.mkdir(parents=True)
        (out/'A.txt').write_text('do not read',encoding='utf8')
        (out/'.env').write_text('never read',encoding='utf8')
        (out/'node_modules').mkdir()
        (out/'node_modules/leak.txt').write_text('',encoding='utf8')
        (self.cwd/'outputs').mkdir()
        (self.cwd/'outputs/outside.txt').write_text('do not visit',encoding='utf8')
        db = self.db(self.home/'.codex/state_5.sqlite',
                     'CREATE TABLE threads(id TEXT PRIMARY KEY,title TEXT,cwd TEXT,updated_at INTEGER,rollout_path TEXT)')
        for i, directory in enumerate([work,work,self.cwd]):
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?)',(str(i),'t',str(directory),1600000000,''))
        db.commit()
        adapter = self.adapters['codex']
        first = list(adapter.sessions())
        result = adapter.read_record(first[0])
        self.assertEqual(result['messages'],[])
        self.assertEqual({f['path']:f['evidence'] for f in result['files']},
                         {str(work):'native_working_directory',str(out/'A.txt'):'codex_outputs_shared_workspace'})
        other = adapter.read_record(first[2])
        self.assertFalse(any(f['path'].endswith('outside.txt') for f in other['files']))
        (out/'A.txt').write_text('replacement content',encoding='utf8')
        second = list(adapter.sessions())
        self.assertNotEqual(first[0]['revision'],second[0]['revision'])
        self.assertEqual(second[0]['revision'],second[1]['revision'])
        self.assertEqual(first[2]['revision'],second[2]['revision'])

    def test_codex_empty_transcript_can_use_only_the_actual_first_user_field(self):
        db = self.db(self.home/'.codex/state_5.sqlite',
                     'CREATE TABLE threads(id TEXT PRIMARY KEY,title TEXT,cwd TEXT,updated_at INTEGER,rollout_path TEXT,first_user_message TEXT)')
        for sid, first in [('one','genuine typed request'),('two','')]:
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?)',(sid,'a title is not a message',str(self.cwd),1600000000,'',first))
        db.commit()
        available = self.adapters['codex'].read('one')
        self.assertEqual(available['messages'],[{'role':'user','text':'genuine typed request'}])
        self.assertEqual(available['transcript_state'],'first_user_excerpt')
        self.assertTrue(available['truncated'])
        self.assertEqual(self.adapters['codex'].read('two')['messages'],[])

    def test_antigravity_reads_visible_native_steps_and_session_artifacts(self):
        sid = '00000000-0000-4000-8000-000000000001'
        base = self.home / '.gemini/antigravity/brain' / sid
        self.jsonl(base / '.system_generated/logs/transcript.jsonl', [
            {'type': 'USER_INPUT', 'source': 'USER_EXPLICIT', 'content': 'hello'},
            {'type': 'PLANNER_RESPONSE', 'source': 'MODEL', 'thinking': 'HIDDEN', 'content': 'answer',
             'tool_calls': [{'name': 'write', 'args': {'file_path': str(self.cwd / 'intent.md')}}]},
            {'type': 'SYSTEM_MESSAGE', 'content': 'HIDDEN'}])
        (base / 'walkthrough.md').write_text('do not ingest', encoding='utf8')
        adapter = self.adapters['antigravity']
        row, = adapter.sessions()
        result = adapter.read_record(row)
        self.assertEqual([r['text'] for r in result['messages']], ['hello', 'answer'])
        self.assertEqual(result['files'], [{'path': str(base / 'walkthrough.md'), 'relation': 'artifact',
                                           'evidence': 'antigravity_session_artifact_directory'}])
        self.assertFalse(row['cwd'])

    def test_antigravity_search_index_fallback_orders_multibyte_steps(self):
        sid = 'session-fts'
        db = self.db(self.home / '.gemini/antigravity/conversation_summaries.db', '''
            CREATE TABLE conversation_summaries(conversation_id,title,last_modified_time,workspace_uris);
            CREATE TABLE conversations_fts(conversation_id,chunk_index,user_body,agent_body,user_step_byte_offsets,agent_step_byte_offsets);
        ''')
        db.execute('INSERT INTO conversation_summaries VALUES(?,?,?,?)', (sid, 'test', '2026-11-25T00:00:00Z', self.cwd.as_uri()))
        db.execute('INSERT INTO conversations_fts VALUES(?,?,?,?,?,?)',
                   (sid, 0, '甲乙', '回答', b'[[0,0],[2,3]]', b'[[1,0]]'))
        db.commit()
        out = self.adapters['antigravity'].read(sid)
        self.assertEqual([r['text'] for r in out['messages']], ['甲', '回答', '乙'])
        self.assertEqual(out['transcript_state'], 'indexed_excerpts')
        self.assertTrue(out['truncated'])

    def test_dsh_compressed_native_events_validate_results_and_skip_runtime_prompts(self):
        try:
            import zstandard
        except ImportError:
            self.skipTest('optional native DSH v4 zstd decoder unavailable')
        sid = 'session-dsh1'
        path = self.home / '.dsh/sessions/work' / sid / 'session.v4.jsonl.zstd'
        path.parent.mkdir(parents=True)
        records = [{'type': 'session', 'id': sid, 'cwd': str(self.cwd)},
                   {'type': 'user/message', 'data': {'source': {'kind': 'user'}, 'content': 'do task'}},
                   {'type': 'user/message', 'data': {'source': {'kind': 'agent-instructions'}, 'content': 'HIDDEN'}},
                   {'type': 'assistant/message', 'data': {'message': {'content': [{'type': 'reasoning', 'text': 'HIDDEN'},
                                                                                {'type': 'text', 'text': 'done'}]}}},
                   {'type': 'tool/call', 'data': {'callId': 'a', 'name': 'write', 'arguments': '{"file_path":"result.md"}'}},
                   {'type': 'tool/result', 'data': {'message': {'toolCallId': 'a', 'isError': False}}},
                   {'type': 'tool/call', 'data': {'callId': 'b', 'name': 'edit', 'arguments': '{"file_path":"failed.md"}'}},
                   {'type': 'tool/result', 'data': {'message': {'toolCallId': 'b', 'isError': True}}}]
        path.write_bytes(zstandard.ZstdCompressor().compress(b'\n'.join(json.dumps(r).encode() for r in records) + b'\n'))
        adapter = self.adapters['dsh']
        row, = adapter.sessions()
        result = adapter.read_record(row)
        self.assertEqual([r['text'] for r in result['messages']], ['do task', 'done'])
        self.assertEqual([(Path(r['path']).name, r['relation']) for r in result['files']],
                         [('work', 'workspace_related'), ('result.md', 'written')])
        with mock.patch.object(adapter, '_stream', return_value=contextlib.nullcontext(None)):
            self.assertEqual(adapter.read_record(row)['transcript_state'], 'codec_unavailable')

    def test_zcode_deleted_session_and_successful_structured_tool_file(self):
        db = self.db(self.home / '.zcode/cli/db/db.sqlite', '''
            CREATE TABLE session(id,title,directory,time_updated,time_deleted);
            CREATE TABLE message(id,session_id,data,sequence);
            CREATE TABLE part(id,message_id,session_id,data,sequence);
        ''')
        db.executemany('INSERT INTO session VALUES(?,?,?,?,?)', [('s1', 'known', str(self.cwd), 1750000000123, None),
                                                                ('s2', 'deleted', str(self.cwd), 1750000000123, 1)])
        db.execute('INSERT INTO message VALUES(?,?,?,?)', ('m1', 's1', '{"role":"assistant"}', 2))
        states = [{'type': 'reasoning', 'text': 'HIDDEN'}, {'type': 'text', 'text': 'answer'},
                  {'type': 'text', 'synthetic': True, 'text': 'HIDDEN'},
                  {'type': 'tool', 'tool': 'Edit', 'state': {'status': 'completed', 'input': {'file_path': 'a.md'},
                                                              'metadata': {'readFileState': {'path': str(self.cwd/'a.md')}}}},
                  {'type': 'tool', 'tool': 'Write', 'state': {'status': 'error', 'input': {'file_path': 'bad.md'}}}]
        db.executemany('INSERT INTO part VALUES(?,?,?,?,?)', [(f'p{n}', 'm1', 's1', json.dumps(obj), n) for n, obj in enumerate(states)])
        db.commit()
        adapter = self.adapters['zcode']
        row, = adapter.sessions()
        result = adapter.read_record(row)
        self.assertEqual(result['messages'], [{'role': 'assistant', 'text': 'answer'}])
        self.assertIn('edited', [f['relation'] for f in result['files']])
        self.assertFalse(any(f['path'].endswith('bad.md') for f in result['files']))
        db.execute('UPDATE session SET time_deleted=1')
        db.commit()
        self.assertEqual(adapter.read_record(row)['transcript_state'], 'unavailable')

    def test_claude_record_pairs_tool_invocation_and_return_and_marks_mentions(self):
        sid = 'c1'
        path = self.home / '.claude/projects/work/c1.jsonl'
        a, b, mentioned = str(self.cwd / 'created.md'), str(self.cwd / 'failed.md'), str(self.cwd / 'reference.md')
        def msg(role, blocks, **extra):
            return dict(type=role, sessionId=sid, cwd=str(self.cwd), message={'role': role, 'content': blocks}, **extra)
        self.jsonl(path, [msg('user', f'请查阅 [文件](<{mentioned}>)'),
                          msg('assistant', [{'type': 'tool_use', 'name': 'Write', 'id': 'ok', 'input': {'file_path': a}},
                                            {'type': 'tool_use', 'name': 'Write', 'id': 'no', 'input': {'file_path': b}}]),
                          msg('user', [{'type': 'tool_result', 'tool_use_id': 'ok'}], toolUseResult={'type': 'create', 'filePath': a}),
                          msg('user', [{'type': 'tool_result', 'tool_use_id': 'no', 'is_error': True}], toolUseResult={'filePath': b}),
                          msg('assistant', [{'type': 'thinking', 'thinking': 'HIDDEN'}, {'type': 'text', 'text': 'ready'}])])
        adapter = self.adapters['claude-code']
        row, = adapter.sessions()
        result = adapter.read_record(row)
        self.assertEqual({f['path']: f['relation'] for f in result['files']},
                         {str(self.cwd): 'workspace_related', mentioned: 'mentioned', a: 'created'})
        self.assertEqual([m['role'] for m in result['messages']], ['user', 'assistant'])
        outside = self.home / 'other' / path.name
        outside.parent.mkdir()
        outside.write_bytes(path.read_bytes())
        self.assertEqual(adapter.read_record({**row, 'source_ref': str(outside)})['transcript_state'], 'unavailable')

    def test_long_tail_bad_lines_and_redacted_secret_never_overrun_budget(self):
        path = self.home / '.claude/projects/project/s1.jsonl'
        self.jsonl(path, [{'type': 'user', 'sessionId': 's1', 'cwd': str(self.cwd), 'message': {'role': 'user', 'content': 'x' * 4000}},
                          {'type': 'assistant', 'sessionId': 's1', 'message': {'role': 'assistant', 'content': 'API_KEY=example_sensitive_value'}}])
        with mock.patch.object(logs, 'MAX_IO', 1024), mock.patch.object(logs, 'MAX_LINE', 256):
            out = self.adapters['claude-code'].read('s1', max_chars=100, max_messages=1)
        self.assertTrue(out['truncated'])
        self.assertEqual(out['messages'], [{'role': 'assistant', 'text': 'API_KEY=[已隐藏]'}])
