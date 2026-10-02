"""Trusted instance injection and pure read paths on a synthetic local server."""
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import server
from aihub import api, capabilities, collaboration, collaboration_api, config, harnesses, service_control


class InteropHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='aihub-interop-http-')
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'工作区';self.root.mkdir()
        self.data=self.base/'app/data'
        self.cfg={'ai_root':str(self.root),'workspace_managed':True,'server':{'port':8765}}
        for target,name,value in [(config,'DATA_DIR',str(self.data)),(server,'CFG',self.cfg),(server,'DB_OBJ',None),(service_control,'GATE',service_control.ActivityGate())]:
            mocker=patch.object(target,name,value);mocker.start();self.addCleanup(mocker.stop)
        class Quiet(server.Handler):
            def log_message(self,*_args):pass
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),Quiet)
        self.port=self.http.server_address[1]
        self.control=service_control.ServiceControl(self.base/'app',self.data,self.port)
        self.http.service_control=self.control
        self.thread=threading.Thread(target=self.http.serve_forever,kwargs={'poll_interval':.02},daemon=True)
        self.thread.start();self.addCleanup(self.stop)
    def stop(self):
        self.http.shutdown();self.http.server_close();self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
    def request(self,path,body=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        try:
            conn.request('POST' if body is not None else 'GET',path,
                         json.dumps(body,ensure_ascii=False).encode('utf-8') if body is not None else None,
                         {'Content-Type':'application/json'})
            response=conn.getresponse();return response.status,json.loads(response.read())
        finally:conn.close()
    def descriptor(self):
        code,result=self.request('/api/interop/describe');self.assertEqual(code,200,result);return result
    def seed(self):
        harnesses.save(self.cfg,{'id':'codex','revision':0,'connection_mode':'mcp_stdio'})
        collaboration.execute(self.cfg,'client_heartbeat',{'client_id':'synthetic-worker','tool':'codex','name':'合成工作端','protocol_version':1})
        capabilities.publish(self.cfg,{'client_id':'synthetic-worker','capabilities_json':json.dumps([{
            'key':'fixture.video','name':'合成视频声明','kind':'mcp_tool','domains':['video'],
            'inputs':{'type':'object','properties':{'prompt':{'type':'string','maxLength':200}},'required':['prompt']},
            'constraints':['仅声明，不执行模型']}],ensure_ascii=False)})
        return capabilities.catalog_readonly(self.cfg)['items'][0]['id']
    def file_state(self):
        return {str(p.relative_to(self.base)):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.base.rglob('*') if p.is_file()}

    def test_empty_describe_is_public_read_without_initializing_any_store(self):
        self.cfg['ai_root']='';self.cfg['workspace_managed']=False
        before=self.file_state()
        with patch.object(collaboration,'store',side_effect=AssertionError('must not initialize store')):
            desc=self.descriptor()
            code,mcp=self.request('/api/collaboration/mcp/interop_describe',{'client_id':'read-only-unregistered'})
        self.assertEqual(code,200,mcp)
        self.assertEqual(desc['protocol'],'aihub-interop/1')
        self.assertEqual(desc['identity']['service_instance_id'],self.control.record['instance_id'])
        self.assertEqual(desc['identity']['port'],self.port)
        self.assertIsNone(desc['connection_revision'])
        self.assertEqual(mcp['workspace_root'],'')
        self.assertNotIn(self.control.record['token'],json.dumps(desc))
        self.assertEqual(before,self.file_state());self.assertFalse(self.data.exists())

    def test_snapshot_http_mcp_matches_exact_declaration_and_does_not_mutate_stores(self):
        identifier=self.seed();desc=self.descriptor();before=self.file_state()
        payload={'capability_id':identifier,'connection_revision':desc['connection_revision'],'_workspace_root':str(self.root)}
        with patch.object(collaboration,'store',side_effect=AssertionError('must not initialize store')):
            code,snapshot=self.request('/api/interop/capability-snapshot?'+urlencode(payload))
            other_code,mcp=self.request('/api/collaboration/mcp/interop_capability_snapshot',dict(payload,client_id='read-only-unregistered'))
        self.assertEqual(code,200,snapshot);self.assertEqual(other_code,200,mcp)
        self.assertEqual(snapshot['snapshot_id'],mcp['snapshot_id'])
        raw=snapshot['declaration']['text'].encode('utf-8')
        self.assertEqual(snapshot['declaration']['sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(snapshot['declaration']['bytes'],len(raw))
        self.assertEqual(snapshot['selected']['client_id'],'synthetic-worker')
        self.assertEqual(snapshot['selected']['target_tool'],'codex')
        self.assertNotIn(self.control.record['token'],json.dumps(snapshot))
        self.assertEqual(before,self.file_state())

    def test_missing_or_forged_instance_context_cannot_replace_trusted_identity(self):
        desc=self.descriptor();instance=self.control.record['instance_id']
        code,result=self.request('/api/collaboration/mcp/interop_describe',{'client_id':'read-only','public_identity':{'service_instance_id':'forged'}})
        self.assertEqual(code,400,result)
        del self.http.service_control
        unknown=self.descriptor()
        self.assertEqual(unknown['identity']['status'],'identity_unavailable')
        self.assertIsNone(unknown['connection_revision'])
        self.assertEqual(desc['identity']['service_instance_id'],instance)

    def test_query_parameters_are_unambiguous_and_instance_revision_is_required(self):
        identifier=self.seed();desc=self.descriptor()
        payload={'capability_id':identifier,'_workspace_root':str(self.root)}
        code,_=self.request('/api/interop/capability-snapshot?'+urlencode(payload));self.assertEqual(code,400)
        payload['connection_revision']=desc['connection_revision']
        query=urlencode(payload)
        code,result=self.request('/api/interop/capability-snapshot?'+query+'&capability_id='+identifier)
        self.assertEqual((code,result['code']),(400,'invalid_request'))
        payload['_workspace_root']=str(self.base/'other')
        code,_=self.request('/api/interop/capability-snapshot?'+urlencode(payload));self.assertEqual(code,409)
        payload['_workspace_root']=str(self.root);payload['connection_revision']='0'*64
        code,_=self.request('/api/interop/capability-snapshot?'+urlencode(payload));self.assertEqual(code,409)

    def test_old_facade_call_signature_and_health_remain_compatible(self):
        code,_,raw=api.dispatch(None,self.cfg,'GET','/api/health',{},None)
        self.assertEqual(code,200);self.assertEqual(json.loads(raw)['version'],'2.13.10')
        code,health=self.request('/api/health');self.assertEqual(code,200)
        self.assertEqual(health['service_instance_id'],self.control.record['instance_id'])
        self.assertNotIn(self.control.record['token'],json.dumps(health))
        with self.assertRaises(PermissionError):
            collaboration_api.execute(self.cfg,'interop_describe',{},actor='other')

    def test_handler_preserves_legacy_positional_dispatch_hooks(self):
        dispatch=api.dispatch
        def positional(db,cfg,method,path,params,body):
            return dispatch(db,cfg,method,path,params,body)
        with patch.object(api,'dispatch',side_effect=positional):
            code,health=self.request('/api/health')
        self.assertEqual(code,200)
        self.assertEqual(health['service_instance_id'],self.control.record['instance_id'])


if __name__=='__main__':unittest.main()
