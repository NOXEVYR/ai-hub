"""Workspace-local index of explicitly selected native conversation sources."""
import contextlib
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

from . import collaboration, config, recycle, service_control, workcenter
from .trace_exchange import Exchange

_lock = threading.RLock()
_jobs = {}
_previews = {}
_relations = {'created','edited','written','mentioned','workspace_related','artifact'}


def adapters(cfg):
    from . import trace_adapters
    return {**trace_adapters.adapters(), 'exchange':Exchange(workcenter._workspace(cfg))}


def _clean(text, limit):
    if not isinstance(text, str):
        return ''
    value = text[:limit]
    value = re.sub(r'-----BEGIN [^-\r\n]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-\r\n]*PRIVATE KEY-----|$)', '[已隐藏密钥]',value)
    value = re.sub(r'(?i)(\b(?:authorization\s*[:=]\s*bearer|bearer)\s+)[^\s,;"\']+',r'\1[已隐藏]',value)
    value = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|client_secret)["\x27]?\s*[:=]\s*["\x27]?)[^\s,;"\x27}]+',r'\1[已隐藏]',value)
    return re.sub(r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{18,})\b','[已隐藏]',value)


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _path(cfg):
    root = config._key(workcenter._workspace(cfg))
    return workcenter._database_path('trace-history-'+_hash(root)[:24]+'.sqlite3')


@contextlib.contextmanager
def _db(cfg):
    path = _path(cfg)
    path.parent.mkdir(exist_ok=True,parents=True)
    with contextlib.closing(sqlite3.connect(path, timeout=8)) as con:
        con.row_factory=sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        con.executescript('''
            CREATE TABLE IF NOT EXISTS sessions(
                key TEXT PRIMARY KEY,tool TEXT NOT NULL,native_id TEXT NOT NULL,title TEXT NOT NULL,cwd TEXT NOT NULL,
                label TEXT NOT NULL,updated REAL NOT NULL,revision TEXT NOT NULL,indexed TEXT NOT NULL DEFAULT '',
                shared TEXT NOT NULL DEFAULT '',state TEXT NOT NULL,partial INTEGER NOT NULL DEFAULT 0,
                messages TEXT NOT NULL DEFAULT '[]', search TEXT NOT NULL DEFAULT '',seen TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS sessions_order ON sessions(updated DESC,key);
            CREATE INDEX IF NOT EXISTS sessions_tool ON sessions(tool);
            CREATE TABLE IF NOT EXISTS files(
                session TEXT REFERENCES sessions(key) ON DELETE CASCADE,fkey TEXT,path TEXT,relation TEXT,evidence TEXT,
                PRIMARY KEY(session,fkey,relation));
            CREATE INDEX IF NOT EXISTS trace_file ON files(fkey);
            CREATE TABLE IF NOT EXISTS reviews(fkey TEXT PRIMARY KEY,state TEXT NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS scans(tool TEXT PRIMARY KEY,updated REAL NOT NULL,error TEXT NOT NULL);
        ''')
        with con:
            yield con


def _session_key(tool,native):
    return _hash(tool+'\0'+native)


def _save_meta(con,tool,row,scan):
    native=str(row['id'])
    key=_session_key(tool,native)
    updated=float(row.get('updated_at') or 0)
    title=_clean(row.get('title'),450) or native
    cwd=_clean(row.get('cwd'),2048)
    label=_clean(row.get('source_label'),160) or tool
    revision=_hash(json.dumps([str(row.get('revision') or updated),title,cwd,label],ensure_ascii=False))
    con.execute('''INSERT INTO sessions(key,tool,native_id,title,cwd,label,updated,revision,state,seen)
        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET title=excluded.title,cwd=excluded.cwd,
        label=excluded.label,updated=excluded.updated,revision=excluded.revision,seen=excluded.seen''',
        (key,tool,native,title,cwd,label,updated,revision,'pending',scan))
    current=con.execute('SELECT indexed FROM sessions WHERE key=?',(key,)).fetchone()[0]
    return key,revision,current!=revision


def _save_detail(con,key,revision,doc):
    messages=[]
    budget=24000
    for msg in doc.get('messages',[])[:80]:
        if budget<=0: break
        if msg.get('role') in ('user','assistant'):
            text=_clean(msg.get('text'),budget)
            if text:
                messages.append({'role':msg['role'],'text':text})
                budget-=len(text)
    con.execute('DELETE FROM files WHERE session=?',(key,))
    for item in doc.get('files',[])[:400]:
        path=item.get('path')
        if not isinstance(path,str) or not os.path.isabs(path) or len(path)>2048 or '\x00' in path:
            continue
        rel=item.get('relation')
        con.execute('INSERT OR IGNORE INTO files VALUES(?,?,?,?,?)',
                    (key,config._key(path),path,rel if rel in _relations else 'mentioned',_clean(item.get('evidence'),240)))
    con.execute("UPDATE sessions SET messages=?,search=?,indexed=?,state=?,partial=?,shared='' WHERE key=?",
                (json.dumps(messages,ensure_ascii=False),'\n'.join(m['text'] for m in messages).casefold(),revision,
                 str(doc.get('transcript_state') or 'available'),int(bool(doc.get('truncated') or budget==0 or len(doc.get('files',[]))>400)),key))


def _sync(cfg,selected,job):
    sources=adapters(cfg)
    groups=[]
    for tool in selected:
        adapter=sources[tool]
        scan=secrets.token_hex(10)
        pending=[]
        job.update(tool=tool)
        try:
            if job['stop'].is_set(): break
            probe=adapter.probe()
            if probe.get('available') is False:
                # A detached or missing source must not look like an empty scan.
                with _db(cfg) as con:
                    con.execute("UPDATE sessions SET state='source_unavailable',shared='',indexed='' WHERE tool=?",(tool,))
                    con.execute('INSERT OR REPLACE INTO scans VALUES(?,?,?)',(tool,time.time(),'本机来源不可用'))
                continue
            with _db(cfg) as con:
                for row in adapter.sessions():
                    if job['stop'].is_set():return
                    key,rev,changed=_save_meta(con,tool,row,scan)
                    if changed:pending.append((key,rev,row))
                    job['seen']+=1
                    if job['seen']%100==0:con.commit()
                # A partially written export or an unfamiliar native schema may
                # temporarily omit a row. Keep local provenance, revoke sharing
                # and require a fresh read before displaying its text again.
                con.execute("UPDATE sessions SET state='source_unavailable',shared='',indexed='' WHERE tool=? AND seen<>?",(tool,scan))
                con.execute('INSERT OR REPLACE INTO scans VALUES(?,?,?)',(tool,time.time(),''))
            if pending:
                pending.sort(key=lambda item:float(item[2].get('updated_at') or 0),reverse=True)
                groups.append((tool,adapter,scan,deque(pending)))
        except Exception:
            # Native decoders can raise format-specific errors. Keep the other
            # providers available and retry this source on the next sync.
            job['errors']+=1
            with _db(cfg) as con:
                con.execute('INSERT OR REPLACE INTO scans VALUES(?,?,?)',(tool,time.time(),'读取未完成，可重新同步'))
    # Rotate across tools so a large archive cannot delay every other source.
    while groups and not job['stop'].is_set():
        for tool,adapter,scan,pending in groups[:]:
            if job['stop'].is_set():return
            key,rev,row=pending.popleft()
            job['tool']=tool
            try:
                reader=getattr(adapter,'read_record',None)
                doc=(reader(row,max_messages=80,max_chars=24000) if reader else
                     adapter.read(row['id'],max_messages=80,max_chars=24000))
                # An active session can advance between enumeration and reading.
                # Retry its latest revision once without rescanning the archive.
                if doc.get('transcript_state')=='unavailable' and reader and hasattr(adapter,'find'):
                    latest=adapter.find(row['id'])
                    if latest and latest['id']==row['id'] and latest.get('revision')!=row.get('revision'):
                        with _db(cfg) as con:
                            key,rev,_=_save_meta(con,tool,latest,scan)
                        doc=reader(latest,max_messages=80,max_chars=24000)
                if doc.get('transcript_state')=='unavailable':
                    raise OSError('本机会话暂不可读。')
                with _db(cfg) as con:
                    _save_detail(con,key,rev,doc)
                job['indexed']+=1
            except Exception:
                job['errors']+=1
                with _db(cfg) as con:
                    con.execute("UPDATE sessions SET state='unreadable',shared='' WHERE key=?",(key,))
            if not pending:groups.remove((tool,adapter,scan,pending))


def start(cfg,tool):
    known=adapters(cfg)
    if tool!='all' and tool not in known:raise ValueError('未知会话来源。')
    path=_path(cfg)
    with _lock:
        if (path in _jobs and _jobs[path]['running']):return {'started':False,'running':True}
        if not service_control.GATE.enter():raise ValueError('服务正在退出。')
        job={'running':True,'tool':'','seen':0,'indexed':0,'errors':0,'stop':threading.Event(),'cancelled':False}
        _jobs[path]=job
        def run():
            try:
                _sync(dict(cfg),list(known) if tool=='all' else [tool],job)
            except Exception:
                job['errors']+=1
            finally:
                job.update(running=False,cancelled=job['stop'].is_set())
                service_control.GATE.leave()
        threading.Thread(target=run,name='aihub-trace-index',daemon=True).start()
        return {'started':True}


def status(cfg):
    with _lock:
        job={k:v for k,v in _jobs.get(_path(cfg),{}).items() if k!='stop'}
    with _db(cfg) as con:
        totals={r['tool']:dict(r) for r in con.execute(
            "SELECT tool,count(*) AS sessions,sum(indexed=revision) AS indexed,sum(partial) AS partial,"
            "sum(state='source_unavailable') AS unavailable FROM sessions GROUP BY tool")}
        scans={r['tool']:dict(r) for r in con.execute('SELECT * FROM scans')}
    result=[]
    for tool,adapter in adapters(cfg).items():
        try:probe=adapter.probe()
        except (OSError,ValueError,sqlite3.Error):probe={'available':False}
        result.append({'tool':tool,'available':bool(probe.get('available')),**totals.get(tool,{}),'scan':scans.get(tool)})
    return {'sources':result,'job':job,'workspace_root':workcenter._workspace(cfg)}


def cancel(cfg):
    with _lock:
        job=_jobs.get(_path(cfg))
        if job:job['stop'].set()
    return {'requested':bool(job and job['running'])}


def _id(value):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value):raise ValueError('会话 ID 无效。')
    return value


def search(cfg,params,*,shared_only=False):
    allowed={'q','file','file_exact','tool','page','review'}
    if set(params)-allowed:raise ValueError('未知追溯查询条件。')
    where,args=[],[]
    for k in ('q','file','file_exact','tool'):
        if not isinstance(params.get(k,''),str) or len(params.get(k,''))>2048:raise ValueError('查询条件无效。')
    page=int(params.get('page',1))
    if not 1<=page<=1000000:raise ValueError('页码超出范围。')
    if params.get('tool'):where.append('s.tool=?');args.append(params['tool'])
    if params.get('q'):
        where.append("(instr(cf(s.title||char(10)||s.cwd||char(10)||s.native_id||char(10)||s.label),?) OR (s.indexed=s.revision AND instr(s.search,?)))")
        args.extend([params['q'].casefold()]*2)
    if shared_only:where.append("s.shared=s.revision AND s.indexed=s.revision AND s.shared<>''")
    if params.get('file') or params.get('file_exact') or params.get('review')=='pending':
        sub='SELECT 1 FROM files f LEFT JOIN reviews r ON r.fkey=f.fkey WHERE f.session=s.key AND s.indexed=s.revision'
        if params.get('file_exact'):
            if not os.path.isabs(params['file_exact']):raise ValueError('精确反查需要绝对路径。')
            sub+=' AND f.fkey=?';args.append(config._key(params['file_exact']))
        elif params.get('file'):
            sub+=' AND instr(cf(f.path),?)';args.append(params['file'].casefold())
        if params.get('review')=='pending':sub+=" AND r.state='pending'"
        where.append('EXISTS ('+sub+')')
    clause=' FROM sessions s'+(' WHERE '+' AND '.join(where) if where else '')
    with _db(cfg) as con:
        con.create_function('cf',1,str.casefold)
        count=con.execute('SELECT count(*)'+clause,args).fetchone()[0]
        rows=con.execute('SELECT key,tool,title,cwd,label,updated,state,partial,indexed=revision AS ready'+clause+
                         ' ORDER BY updated DESC,key LIMIT 30 OFFSET ?',(*args,(page-1)*30))
        return {'items':[dict(r) for r in rows],'total':count,'page':page,'page_size':30}


def detail(cfg,key,*,shared_only=False):
    with _db(cfg) as con:
        con.execute('BEGIN')
        row=con.execute('SELECT key,tool,title,cwd,updated,state,revision,indexed,shared,messages,partial FROM sessions WHERE key=?',(_id(key),)).fetchone()
        if not row or (shared_only and (row['indexed']!=row['revision'] or row['shared']!=row['revision'])):raise ValueError('没有可读取的会话记录。')
        ready=row['indexed']==row['revision']
        files=con.execute('SELECT f.path,f.relation,f.evidence,'+('NULL' if shared_only else 'r.state')+
                          ' AS review FROM files f LEFT JOIN reviews r ON r.fkey=f.fkey WHERE f.session=? ORDER BY f.path,f.relation',(key,))
        return {'key':key,'tool':row['tool'],'title':row['title'],'cwd':row['cwd'],'state':row['state'],
                'revision':row['revision'],'ready':ready,'partial':bool(row['partial']),
                'shared':ready and row['shared']==row['revision'],'messages':json.loads(row['messages']) if ready else [],
                'files':[dict(r) for r in files] if ready else []}


def share(cfg,key,revision,enabled):
    if type(enabled) is not bool:raise ValueError('共享状态无效。')
    with _db(cfg) as con:
        row=con.execute('UPDATE sessions SET shared=? WHERE key=? AND revision=? AND indexed=revision',
                        (revision if enabled else '',_id(key),revision))
        if row.rowcount!=1:raise ValueError('记录已更新，请重开会话后确认。')
    return {'shared':enabled}


def file_review(cfg,key,path,state=None):
    with _db(cfg) as con:
        con.execute('BEGIN IMMEDIATE' if state is not None else 'BEGIN')
        return _file_review(con,cfg,key,path,state)


def _file_review(con,cfg,key,path,state=None):
    if not isinstance(path,str) or not os.path.isabs(path) or len(path)>2048:raise ValueError('文件路径无效。')
    fkey=config._key(path)
    if not con.execute('SELECT 1 FROM files f JOIN sessions s ON s.key=f.session WHERE f.session=? AND f.fkey=? AND s.indexed=s.revision',(_id(key),fkey)).fetchone():
        raise ValueError('文件未关联到此会话的当前索引。')
    if state is not None:
        if state not in ('pending','keep'):raise ValueError('复核状态无效。')
        con.execute('INSERT OR REPLACE INTO reviews VALUES(?,?,?)',(fkey,state,time.time()))
    refs=con.execute('SELECT count(DISTINCT session) FROM files WHERE fkey=?',(fkey,)).fetchone()[0]
    review=con.execute('SELECT state FROM reviews WHERE fkey=?',(fkey,)).fetchone()
    within=config._within(path,workcenter._workspace(cfg))
    result={'path':path,'references':refs,'review':review[0] if review else None,'eligible':False,
            'lifecycle_scope':str(Path(path).parent) if within else None,'reason':'普通产物可标记待复核或保留。'}
    if result['review']=='keep':return {**result,'reason':'已标记保留。'}
    if refs>1:return {**result,'reason':'多条已索引会话引用此文件，请先核对用途。'}
    if not within:return {**result,'reason':'文件位于当前曜核工作区外，可记录复核意见。'}
    if _jobs.get(_path(cfg),{}).get('running') or con.execute("SELECT 1 FROM sessions WHERE indexed<>revision AND state NOT IN ('unreadable','source_unavailable') LIMIT 1").fetchone():
        return {**result,'reason':'会话索引仍有待更新记录，完成后可重新预览回收。'}
    with workcenter._readonly('collaboration.sqlite3') as con:
        if con is None or not {'tasks','artifacts'}<=workcenter._tables(con):return result
        row=con.execute("SELECT a.* FROM artifacts a JOIN tasks t ON t.id=a.task_id AND t.root=a.root WHERE a.root=? AND a.path_key=? AND a.kind='temp' AND a.status='active' AND a.pinned=0 AND t.status='completed' AND a.deleted_at IS NULL AND t.deleted_at IS NULL AND a.expires_at<=?",
                        (config._key(cfg['ai_root']),fkey,collaboration._now())).fetchone()
        if row:
            task=collaboration._get(con,'tasks',row['root'],row['task_id'])
            if collaboration._same_snapshot(row,collaboration.file_snapshot(row['path'],collaboration._task_parent(task,'temp',cfg['ai_root']))):
                return {**result,'eligible':True,'artifact_id':row['id'],'bytes':row['size'],'reason':'已核实为已完成任务的到期临时产物。'}
    return result


def preview(cfg,key,path):
    result=file_review(cfg,key,path)
    if result['eligible']:
        token=secrets.token_urlsafe(24)
        with _lock:
            now=time.monotonic()
            for old in list(_previews):
                if _previews[old][0]<now:del _previews[old]
            _previews[token]=(now+120,_path(cfg),key,path,result['artifact_id'])
        result['token']=token
    return result


def apply_recycle(cfg,token):
    with _lock:item=_previews.pop(token,None) if isinstance(token,str) else None
    if not item or item[0]<time.monotonic() or item[1]!=_path(cfg):raise ValueError('回收预览已失效。')
    _,_,key,path,artifact=item
    with _lock, _db(cfg) as con:
        con.execute('BEGIN IMMEDIATE')
        result=_file_review(con,cfg,key,path)
        if not result['eligible'] or result['artifact_id']!=artifact:raise ValueError('文件或引用已变化，请重新预览。')
        return collaboration.recycle_candidate(cfg,artifact,recycle.recycle_file)
