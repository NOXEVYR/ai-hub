"""Local interchange for clients that can export a small JSON session manifest."""
import json
import os
from pathlib import Path
import re
import stat

from . import config


class Exchange:
    def __init__(self, root):
        self.folder = Path(root) / '00_Management' / 'AIHub' / 'TraceExports'

    def probe(self):
        return {'available': self.folder.is_dir(), 'location': str(self.folder), 'format': 'aihub-trace/1'}

    def _load(self, path):
        config._check_ancestors(str(path.parent))
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or config._is_reparse(before)
                or before.st_size > 4 * 1024 * 1024):
            raise ValueError('通用清单须为 4 MiB 以内的普通 JSON 文件。')
        with path.open('rb') as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
            opened = os.fstat(stream.fileno())
        after = path.lstat()
        signature = lambda s: (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_nlink)
        if signature(before) != signature(opened) or signature(before) != signature(after):
            raise ValueError('清单读取期间已变化。')
        data = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(data, dict) or data.get('schema') != 'aihub-trace/1':
            raise ValueError('未知会话清单格式。')
        if not all(isinstance(data.get(k,[]),list) for k in ('messages','files')):
            raise ValueError('消息与文件字段须为列表。')
        return data, after

    def sessions(self):
        if not self.folder.is_dir():
            return
        config._check_ancestors(str(self.folder))
        for path in self.folder.glob('*.json'):
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',path.stem):
                continue
            try:
                data, info = self._load(path)
            except (ValueError,OSError,RecursionError):
                continue
            yield {'id':path.stem,'title':data.get('title') or path.stem,'cwd':data.get('cwd') or '',
                   'source_label':data.get('tool') or '通用清单','updated_at':info.st_mtime,
                   'source_ref':str(path),'revision':f'{info.st_size}/{info.st_mtime_ns}','transcript_state':'exported'}

    def read(self, session_id, *, max_messages=80, max_chars=24000):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',session_id):
            raise ValueError('清单 ID 无效。')
        data, _ = self._load(self.folder / (session_id+'.json'))
        messages, files, used, truncated = [], [], 0, False
        for item in data.get('messages', []):
            if not isinstance(item,dict) or item.get('role') not in ('user','assistant') or not isinstance(item.get('text'),str):
                continue
            text = item['text']
            if len(messages)>=max_messages or used>=max_chars:
                truncated=True
                break
            clipped=text[:max_chars-used]
            truncated |= clipped != text
            used+=len(clipped)
            messages.append({'role':item['role'],'text':clipped})
        for item in data.get('files', [])[:200]:
            if isinstance(item,dict) and isinstance(item.get('path'),str) and os.path.isabs(item['path']):
                relation=item.get('relation')
                files.append({'path':item['path'],'relation':relation if relation in ('created','edited','artifact','mentioned') else 'mentioned',
                              'evidence':'导出工具声明'})
        return {'id':session_id,'messages':messages,'files':files,'truncated':truncated or len(data.get('files',[]))>200,
                'transcript_state':'exported'}
