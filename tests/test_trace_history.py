import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from aihub import collaboration, trace_history as trace
from aihub.trace_exchange import Exchange
import test_workcenter as fixtures


class TraceHistoryTests(unittest.TestCase):
    setUp=fixtures.WorkcenterTests.setUp

    def export(self,key,path,text='a message 移动镜头',relation='created'):
        adapter=Exchange(self.root)
        adapter.folder.mkdir(exist_ok=True,parents=True)
        file=adapter.folder/(key+'.json')
        file.write_text(json.dumps({'schema':'aihub-trace/1','tool':'自定义客户端','title':'short',
            'cwd':str(self.root),'messages':[{'role':'user','text':text},{'role':'system','text':'private config'}],
            'files':[{'path':str(path),'relation':relation}]}), encoding='utf-8')
        return file

    def sync(self):
        job={'seen':0,'indexed':0,'errors':0,'stop':threading.Event()}
        with patch.object(trace,'adapters',return_value={'exchange':Exchange(self.root)}):
            trace._sync(self.cfg,['exchange'],job)
        self.assertEqual(job['errors'],0)
        return job

    def test_search_reverse_lookup_sharing_and_incremental_updates(self):
        p=self.root/'test.mov'
        self.export('one',p)
        self.export('two',p,relation='mentioned')
        self.assertEqual(self.sync()['indexed'],2)
        self.assertEqual(self.sync()['indexed'],0)
        rows=trace.search(self.cfg,{'q':'移动镜头','file':'test.mov'})
        self.assertEqual(rows['total'],2)
        self.assertEqual(trace.search(self.cfg,{},shared_only=True)['total'],0)
        key=rows['items'][0]['key']
        d=trace.detail(self.cfg,key)
        self.assertNotIn('private config',str(d))
        trace.share(self.cfg,key,d['revision'],True)
        self.assertEqual(trace.detail(self.cfg,key,shared_only=True)['key'],key)
        self.assertFalse(trace.preview(self.cfg,key,str(p))['eligible'])
        self.assertEqual(trace.file_review(self.cfg,key,str(p),'pending')['references'],2)
        self.assertEqual(trace.search(self.cfg,{'review':'pending'})['total'],2)
        self.export('one',p,'new snapshot password=privatevalue and some text')
        self.export('two',p,'new snapshot access_token=privatevalue and some text')
        self.assertEqual(self.sync()['indexed'],2)
        with self.assertRaises(ValueError):
            trace.detail(self.cfg,key,shared_only=True)
        self.assertNotIn('privatevalue',str(trace.detail(self.cfg,key)))
        self.assertEqual(trace.search(self.cfg,{'q':'privatevalue'})['total'],0)

    def test_expired_temp_recycles_only_after_fresh_preview(self):
        def call(action,**args):
            return collaboration.execute(self.cfg,action,args)
        call('client_heartbeat',client_id='one',tool='codex',name='one',protocol_version=1)
        task=call('task_create',project='Test',title='trace retention',report_policy='optional')
        claim=call('task_claim',task_id=task['id'],client_id='one')
        lease={'task_id':task['id'],'client_id':'one','lease_token':claim['lease_token']}
        file=call('artifact_write',**lease,kind='temp',category='temp_candidate',title='preview',content='test only',filename='preview.txt')
        call('task_finish',**lease,summary='done')
        with collaboration.store(self.cfg) as (con,root):
            con.execute("UPDATE artifacts SET expires_at='2001-01-01' WHERE id=?",(file['id'],))
        self.export('one',file['path'])
        self.sync()
        key=trace.search(self.cfg,{})['items'][0]['key']
        preview=trace.preview(self.cfg,key,file['path'])
        self.assertTrue(preview['eligible'])
        trace.file_review(self.cfg,key,file['path'],'keep')
        with self.assertRaises(ValueError):
            trace.apply_recycle(self.cfg,preview['token'])

        self.assertTrue(Path(file['path']).exists())
        trace.file_review(self.cfg,key,file['path'],'pending')
        preview=trace.preview(self.cfg,key,file['path'])
        # A disposable fixture uses a reversible move in place of the OS recycler.
        retained=self.base/'mock-recycle'
        with patch.object(trace.recycle,'recycle_file',lambda path:Path(path).rename(retained)):
            self.assertEqual(trace.apply_recycle(self.cfg,preview['token'])['status'],'recycled')
        self.assertEqual(retained.read_text(),'test only')
        with self.assertRaises(ValueError):
            trace.apply_recycle(self.cfg,preview['token'])

    def test_incomplete_export_retains_identity_and_revokes_shared_excerpt(self):
        file=self.export('one',self.root/'x.mov')
        self.sync()
        key=trace.search(self.cfg,{})['items'][0]['key']
        snapshot=trace.detail(self.cfg,key)
        trace.share(self.cfg,key,snapshot['revision'],True)
        file.write_text('{',encoding='utf-8')
        self.sync()
        self.assertEqual(trace.search(self.cfg,{})['items'][0]['key'],key)
        self.assertEqual(trace.detail(self.cfg,key)['state'],'source_unavailable')
        self.assertEqual(trace.search(self.cfg,{},shared_only=True)['total'],0)
        self.export('one',self.root/'x.mov')
        self.sync()
        self.assertTrue(trace.detail(self.cfg,key)['ready'])
        self.assertFalse(trace.detail(self.cfg,key)['shared'])

    def test_reverse_lookup_selects_exact_file_while_text_field_supports_fragments(self):
        path=self.root/'preview.mov'
        self.export('one',path)
        self.export('two',str(path)+'.bak')
        self.sync()
        self.assertEqual(trace.search(self.cfg,{'file':'preview.mov'})['total'],2)
        self.assertEqual(trace.search(self.cfg,{'file_exact':str(path)})['total'],1)

    def test_active_session_retries_updated_revision_once(self):
        first={'id':'growing-session','revision':'1','title':'active'}
        latest={**first,'revision':'2'}
        class ActiveSource:
            def probe(self):return {'available':True}
            def sessions(self):return [first]
            def find(self,native_id):return latest
            def read_record(self,row,**bounds):
                if row==first:return {'transcript_state':'unavailable'}
                return {'messages':[{'role':'user','text':'新内容'}]}
        job={'seen':0,'indexed':0,'errors':0,'stop':threading.Event()}
        with patch.object(trace,'adapters',return_value={'codex':ActiveSource()}):
            trace._sync(self.cfg,['codex'],job)
        self.assertEqual((job['indexed'],job['errors']),(1,0))
        self.assertEqual(trace.search(self.cfg,{'q':'新内容'})['total'],1)
