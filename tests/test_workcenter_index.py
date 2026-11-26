"""Large catalog behavior, exercised with disposable workspaces and small thresholds."""
import concurrent.futures
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest import mock

from aihub import collaboration, collaboration_maintenance as maintenance, workcenter as wc, workcenter_index as index
import test_workcenter as fixture


class CatalogIndexTests(unittest.TestCase):
    setUp = fixture.WorkcenterTests.setUp
    file = fixture.WorkcenterTests.file
    scan = fixture.WorkcenterTests.scan

    def prepare(self):
        self.files = [self.file('2026-10-14/alpha/outputs/report-%d.md' % i) for i in range(4)]
        self.source = self.scan()
        threshold = mock.patch.object(index, 'MIN_RECORDS', 3)
        threshold.start()
        self.addCleanup(threshold.stop)

    def call(self, action, **params):
        return collaboration.execute(self.cfg, action, params)

    def test_persistent_pages_equal_live_queries_and_do_not_rescan(self):
        self.prepare()
        self.call('task_create', project='Managed', title='检验报告')
        requests = [(wc.list_documents, {'query': 'REPORT', 'page': '2', 'page_size': '2'}),
                    (wc.list_documents, {'task_id': ':unknown:'}),
                    (wc.list_projects, {'entry_kind': 'all', 'query': 'REPORT', 'task_id': ':unknown:'}),
                    (wc.list_projects, {'entry_kind': 'all', 'query': '检验报告'}),
                    (wc.list_projects, {'entry_kind': 'all', 'source_type': 'codex'})]
        with mock.patch.object(index, 'MIN_RECORDS', 100000):
            expected = [fn(self.cfg, params) for fn, params in requests]
        baseline = wc.list_documents(self.cfg)
        self.assertTrue(baseline['index']['persistent'])
        self.assertTrue(index._path(self.root).is_file())
        self.assertFalse((self.data / 'workcenter.sqlite3').exists())
        with mock.patch.object(wc, '_catalog', side_effect=AssertionError('repeated catalog build')):
            for (fn, params), live in zip(requests, expected):
                result = fn(self.cfg, params)
                self.assertEqual(result.pop('index')['state'], 'ready')
                live.pop('index')
                self.assertEqual(result, live)
            self.assertEqual(wc.lookup_indexed_project(self.cfg, 'alpha'), [])
            item, = wc.lookup_indexed_project(self.cfg, 'Managed')
            self.assertEqual(wc.lookup_indexed_project(self.cfg, item['id'])[0]['id'], item['id'])
            self.assertEqual(wc.lookup_indexed_project(self.cfg, item['path'])[0]['id'], item['id'])

    def test_heartbeat_claim_handoff_and_finish_change_only_live_task_rows(self):
        self.prepare()
        task = self.call('task_create', project='P', title='task', report_policy='optional')
        self.call('client_heartbeat', client_id='one', tool='codex', name='one', protocol_version=1)
        self.call('client_heartbeat', client_id='two', tool='zcode', name='two', protocol_version=1)
        item = wc.list_projects(self.cfg, {'entry_kind': 'all', 'task_id': task['id']})['items'][0]
        self.assertEqual(item['task_status_counts'], {'queued': 1})
        with mock.patch.object(wc, '_catalog', side_effect=AssertionError('rebuilt for active work')):
            self.call('client_heartbeat', client_id='new', tool='codex', name='new', protocol_version=1)
            one = self.call('task_claim', task_id=task['id'], client_id='one')['lease_token']
            self.assertEqual(wc.lookup_indexed_project(self.cfg, 'P')[0]['task_status_counts'], {'active': 1})
            self.call('task_handoff', task_id=task['id'], client_id='one', lease_token=one, summary='handoff', target_tool='zcode')
            p = wc.lookup_indexed_project(self.cfg, 'P')[0]
            self.assertEqual(p['linked_tools'], ['zcode'])
            self.assertEqual(p['tasks'][0]['status'], 'queued')
            two = self.call('task_claim', task_id=task['id'], client_id='two')['lease_token']
            self.call('task_finish', task_id=task['id'], client_id='two', lease_token=two, summary='done')
            self.assertEqual(wc.lookup_indexed_project(self.cfg, 'P')[0]['task_status_counts'], {'completed': 1})

    def test_registered_document_reports_live_owner_after_handoff(self):
        self.prepare()
        task = self.call('task_create', project='P', title='real report')
        self.call('client_heartbeat', client_id='one', tool='codex', name='one', protocol_version=1)
        claim = self.call('task_claim', task_id=task['id'], client_id='one')['lease_token']
        report = Path(task['paths']['reports']) / 'final.md'
        report.write_text('final', encoding='utf-8')
        self.call('artifact_register', task_id=task['id'], client_id='one', lease_token=claim, kind='report',
                  path=str(report), title='Final')
        params = {'task_id': task['id'], 'intake_status': 'registered'}
        doc, = wc.list_documents(self.cfg, params)['items']
        self.assertEqual(doc['task_owner_id'], 'one')
        with mock.patch.object(wc, '_catalog', side_effect=AssertionError('rebuilt for handoff')):
            self.call('task_handoff', task_id=task['id'], client_id='one', lease_token=claim, summary='pass', target_tool='zcode')
            doc, = wc.list_documents(self.cfg, params)['items']
            self.assertIsNone(doc['task_owner_id'])
            self.assertEqual(doc['task_target_tool'], 'zcode')
            p, = wc.lookup_indexed_project(self.cfg, 'P')
            self.assertEqual(p['linked_tools'], ['codex', 'zcode'])

    def test_manual_refresh_and_revoke_hide_stale_records(self):
        self.prepare()
        listing = wc.list_documents(self.cfg)
        self.files[0].unlink()
        result = wc.list_documents(self.cfg, {'refresh': '1'})
        missing, = [d for d in result['items'] if d['path'] == str(self.files[0])]
        self.assertEqual(missing['status'], 'missing')
        self.assertGreater(result['index']['built_at'], listing['index']['built_at'])
        body = {'_workspace_root': str(self.root), 'entity_type': 'source', 'entity_id': self.source['id']}
        token = maintenance.record_lifecycle(self.cfg, 'record_delete_preview', body)['preview_token']
        maintenance.record_lifecycle(self.cfg, 'record_delete_apply', {**body, 'preview_token': token})
        self.assertEqual(wc.list_documents(self.cfg)['total'], 0)
        maintenance.record_lifecycle(self.cfg, 'record_restore', body)
        self.assertEqual(wc.list_documents(self.cfg)['total'], 4)

    def test_background_warmup_and_expiry_coalesce(self):
        self.prepare()
        entered, release = threading.Event(), threading.Event()
        original = index._fill
        def hold(*args):
            entered.set()
            self.assertTrue(release.wait(10))
            original(*args)
        with concurrent.futures.ThreadPoolExecutor(2) as workers, mock.patch.object(index, '_fill', side_effect=hold) as fill:
            self.assertFalse(wc.warm_index(self.cfg))
            self.assertTrue(entered.wait(10))
            try:
                self.assertFalse(wc.index_ready(self.cfg))
                with self.assertRaises(index.IndexPending):
                    wc.lookup_indexed_project(self.cfg, 'Managed')
                first = workers.submit(wc.list_documents, self.cfg)
                second = workers.submit(wc.list_projects, self.cfg)
            finally:
                release.set()
            self.assertEqual(first.result(10)['total'], 4)
            second.result(10)
            self.assertEqual(fill.call_count, 1)
        entered.clear()
        release.clear()
        with mock.patch.object(index, 'MAX_AGE', -1), mock.patch.object(index, '_fill', side_effect=hold):
            self.assertEqual(wc.list_documents(self.cfg)['index']['state'], 'refreshing')
            self.assertTrue(entered.wait(10))
            try:
                with index._lock:
                    running = index._jobs[index._path(self.root)]
                self.assertEqual(wc.list_documents(self.cfg)['total'], 4)
            finally:
                release.set()
                running.result(10)

    def test_unwritable_cache_keeps_a_single_memory_snapshot(self):
        self.prepare()
        original = sqlite3.connect
        path = index._path(self.root)
        def connect(db, *args, **kwargs):
            if db == str(path):
                raise sqlite3.OperationalError('read-only fixture')
            return original(db, *args, **kwargs)
        with mock.patch.object(sqlite3, 'connect', side_effect=connect):
            result = wc.list_documents(self.cfg)
        self.addCleanup(index._fallback.pop, path, None)
        self.assertFalse(result['index']['persistent'])
        self.assertFalse(path.exists())
        with mock.patch.object(wc, '_catalog', side_effect=AssertionError('discarded memory index')):
            self.assertTrue(wc.index_ready(self.cfg))
            self.assertEqual(wc.list_documents(self.cfg)['total'], 4)

    def test_new_process_reuses_persistent_index_without_reopening_sources(self):
        self.prepare()
        self.assertEqual(wc.list_documents(self.cfg)['total'], 4)
        script = """
import sys
from pathlib import Path
from unittest.mock import patch
from aihub import config, workcenter as wc
config.DATA_DIR = sys.argv[1]
config.REPORTS_DIR = str(Path(config.DATA_DIR) / 'reports')
with patch.object(wc, '_catalog', side_effect=AssertionError('rebuilt in clean process')):
    result = wc.list_documents({'ai_root': sys.argv[2], 'workspace_managed': True})
assert result['total'] == 4 and result['index']['persistent']
"""
        subprocess.run([sys.executable, '-B', '-c', script, str(self.data), str(self.root)],
                       check=True, capture_output=True, timeout=10)

    def test_task_deletion_and_restoration_change_the_authority_signature(self):
        self.prepare()
        task = self.call('task_create', project='Archived', title='archivable')
        wc.list_documents(self.cfg)
        self.assertTrue(wc.lookup_indexed_project(self.cfg, 'Archived'))
        body = {'_workspace_root': str(self.root), 'entity_type': 'task', 'entity_id': task['id']}
        token = collaboration.record_lifecycle(self.cfg, 'record_delete_preview', body)['preview_token']
        collaboration.record_lifecycle(self.cfg, 'record_delete_apply', {**body, 'preview_token': token})
        wc.list_documents(self.cfg)
        self.assertFalse(wc.lookup_indexed_project(self.cfg, 'Archived'))
        collaboration.record_lifecycle(self.cfg, 'record_restore', body)
        wc.list_documents(self.cfg)
        self.assertTrue(wc.lookup_indexed_project(self.cfg, 'Archived'))

    def test_large_legacy_only_catalog_promotes_after_discovery(self):
        self.projects.return_value = {'items': [
            {'name': str(n), 'path': str(self.root / '40_Projects' / str(n)), 'registered': True} for n in range(5)]}
        with mock.patch.object(index, 'MIN_RECORDS', 4):
            first = wc.list_projects(self.cfg)
            second = wc.list_projects(self.cfg)
        self.assertEqual(first['index']['state'], 'live')
        self.assertTrue(second['index']['persistent'])
        self.assertEqual(first['items'], second['items'])
