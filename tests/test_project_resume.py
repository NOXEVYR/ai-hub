import json
import unittest
from unittest.mock import patch

from aihub import api, collaboration, collaboration_api, project_resume, workcenter
import test_workcenter as fixtures


class ProjectResumeTests(unittest.TestCase):
    setUp = fixtures.WorkcenterTests.setUp

    def call(self, action, **params):
        return collaboration.execute(self.cfg, action, params)

    def task(self, project, title):
        return self.call('task_create', project=project, title=title, report_policy='optional')

    def test_existing_handoff_and_explicit_next_step(self):
        self.call('client_heartbeat', client_id='one', tool='codex', name='one', protocol_version=1)
        task = self.task('Project', 'first task')
        self.task('Other', 'unrelated task')
        claim = self.call('task_claim', task_id=task['id'], client_id='one')
        lease = dict(task_id=task['id'], client_id='one', lease_token=claim['lease_token'])
        self.call('artifact_write', **lease, kind='report', category='report', filename='proof.md',
                  title='evidence', content='contents should only be opened explicitly', memory_candidates=[])
        self.call('task_handoff', **lease, summary='checked one clip', next_step='verify clip two', target_tool='codex')
        data = collaboration_api.execute(self.cfg, 'project_resume', {'project': 'Project', 'client_id': 'one'}, actor='mcp')
        self.assertEqual(data['next_step'], {'source': 'handoff', 'task_id': task['id'], 'text': 'verify clip two'})
        self.assertEqual(data['latest_handoff']['summary'], 'checked one clip')
        self.assertEqual(data['open_task_count'], 1)
        self.assertEqual(data['files'][0]['title'], 'evidence')
        self.assertNotIn('unrelated task', str(data))
        self.assertNotIn(lease['lease_token'], str(data))
        self.assertNotIn('contents should only', str(data))
        found = project_resume.search_documents(self.cfg, {'project': data['project']['id'], 'query': 'proof', 'page_size': 1})
        self.assertEqual([r['id'] for r in found['items']], [data['files'][0]['id']])
        ident = data['files'][0]['id']
        first = collaboration_api.execute(self.cfg, 'document_read',
                {'client_id':'one','document_id':ident,'max_chars':8}, actor='mcp')
        self.assertEqual(first['content'], 'contents')
        tail = project_resume.read_excerpt(self.cfg, {'document_id':ident, 'offset':first['next_offset'],
                                                      'preview_sha256':first['preview_sha256']})
        self.assertIn('explicitly', tail['content'])
        self.assertIsNone(tail['next_offset'])
        self.assertEqual(project_resume.read_excerpt(self.cfg, {'document_id':ident,'offset':8,
                         'preview_sha256':'a'*64})['state'],'changed')
        with self.assertRaises(ValueError):
            project_resume.read_excerpt(self.cfg, {'document_id':ident,'offset':8})
        code, _, raw = api.workcenter_request(None, self.cfg, {'action':'project-resume','project':task['project']}, None)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)['next_step'], data['next_step'])
        self.assertEqual(api.workcenter_request(None, self.cfg, {'action':'project-resume','project':'Project','_workspace_root':'other'}, None)[0], 409)
        claim = self.call('task_claim', task_id=task['id'], client_id='one')
        lease['lease_token'] = claim['lease_token']
        self.call('task_handoff', **lease, summary='legacy free text', target_tool='codex')
        legacy = project_resume.resume(self.cfg, {'project':'project'})
        self.assertEqual(legacy['latest_handoff']['summary'], 'legacy free text')
        self.assertEqual(legacy['next_step']['source'], 'task')
        claim = self.call('task_claim', task_id=task['id'], client_id='one')
        self.call('task_finish', client_id='one', task_id=task['id'], lease_token=claim['lease_token'], summary='all checked')
        done = project_resume.resume(self.cfg, {'project':'Project'})
        self.assertEqual(done['next_step']['source'], 'none')
        self.assertEqual(done['last_completion']['summary'], 'all checked')

    def test_same_named_project_at_other_location_is_not_merged(self):
        self.task('Project', 'workspace task')
        training = self.root / '50_Training' / 'Projects' / 'Project'
        training.mkdir(parents=True)
        self.projects.return_value = {'items':[{'name':'Project','path':str(training),'registered':True}]}
        ambiguous = project_resume.resume(self.cfg, {'project':'Project'})
        self.assertEqual(ambiguous['state'], 'ambiguous')
        self.assertEqual(len(ambiguous['candidates']), 2)
        data = project_resume.resume(self.cfg, {'project':str(training)})
        self.assertEqual(data['open_task_count'], 0)
        self.assertEqual(data['project']['path'],str(training))
        self.assertEqual(project_resume.resume(self.cfg, {'project':'missing'})['state'],'not_found')

    def test_cold_start_and_query_validation(self):
        with patch.object(workcenter, 'index_ready', return_value=False):
            self.assertEqual(project_resume.resume(self.cfg, {'project':'x'})['state'], 'building')
            self.assertEqual(project_resume.search_documents(self.cfg, {'query':'a'})['state'], 'building')
        for params in ({},{'query':'a','page_size':31},{'project':'x','page':False},{'unbounded':True}):
            with self.assertRaises(ValueError):
                project_resume.search_documents(self.cfg, params)
