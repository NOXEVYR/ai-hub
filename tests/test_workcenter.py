"""All fixtures are disposable: federation never changes source files."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aihub import harnesses, config, collaboration, collaboration_maintenance as maintenance, workcenter

_REAL_REPORTS = workcenter.management.reports
_REAL_PROJECTS = workcenter.management.projects


class WorkcenterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihub-workcenter-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        self.data = self.base / 'application/data'
        self.data.mkdir(parents=True)
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True}
        for name, value in [('DATA_DIR', str(self.data)), ('APP_DIR', str(self.data.parent)),
                            ('REPORTS_DIR', str(self.data / 'reports'))]:
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        # This fixture explicitly opts into the four compatibility recipes.
        for identifier in harnesses.BUILTIN_IDS:
            harnesses.save(self.cfg, {'id': identifier, 'revision': 0, 'connection_mode': 'mcp_stdio'})
        self.reports = mock.patch.object(workcenter.management, 'reports', return_value=[]).start()
        self.projects = mock.patch.object(workcenter.management, 'projects', return_value={'items': []}).start()
        self.addCleanup(mock.patch.stopall)

    def file(self, relative, content='sample', base=None):
        path = (base or self.root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def scan(self, root=None, tool='codex'):
        source = maintenance.add_source(self.cfg, {'path': str(root or self.root), 'label': 'fixture source', 'tool': tool})
        maintenance.scan_source(self.cfg, {'source_id': source['id']})
        return source

    def test_all_records_pagination_filters_facets_and_metadata_only(self):
        for number in range(507):
            self.file('2026-09-25/alpha/outputs/report-%03d.md' % number)
        source = self.scan()
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('no document bodies in list')):
            first = workcenter.list_documents(self.cfg, {'page_size': '100'})
        self.assertEqual(first['total'], 507)
        self.assertEqual(len(first['items']), 100)
        sixth = workcenter.list_documents(self.cfg, {'page_size': 100, 'page': 6})
        self.assertEqual(len(sixth['items']), 7)
        self.assertFalse({d['id'] for d in first['items']} & {d['id'] for d in sixth['items']})
        self.assertEqual(first['workspace_root'], str(self.root))
        self.assertEqual(first['facets']['tools'], [{'value': 'codex', 'label': 'codex', 'count': 507}])
        filtered = workcenter.list_documents(self.cfg, {'query': 'report-506', 'category': 'report', 'tool': 'codex', 'intake_status': 'indexed'})
        self.assertEqual(filtered['total'], 1)
        doc = filtered['items'][0]
        self.assertEqual(doc['project_origin'], 'inferred')
        self.assertEqual(doc['project_name'], 'alpha')
        self.assertEqual(workcenter.list_documents(self.cfg, {'project_id': doc['project_id']})['total'], 507)
        self.assertFalse(first['coverage']['sources'][0]['truncated'])
        self.assertEqual(first['coverage']['sources'][0]['id'], source['id'])
        self.assertFalse((self.data / 'workcenter.sqlite3').exists())

    def test_legacy_and_inventory_dedupe_path_and_registered_intake(self):
        path = self.file('report.md')
        self.scan()
        self.reports.return_value = [{'path': str(path), 'name': 'legacy', 'group': 'old', 'size': 6, 'mtime': 0}]
        result = workcenter.list_documents(self.cfg)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['intake_status'], 'indexed')
        self.assertEqual(set(result['items'][0]['source_labels']), {'old', 'fixture source'})
        self.assertEqual(workcenter.lookup_document(self.cfg, str(path))['id'], result['items'][0]['id'])
        task = collaboration.execute(self.cfg, 'task_create', {'project': 'P', 'title': 'task', 'description': 'description', 'target_tool': 'codex'})
        collaboration.execute(self.cfg, 'client_heartbeat', {'client_id': 'test', 'tool': 'codex', 'name': 'test', 'protocol_version': 1})
        claimed = collaboration.execute(self.cfg, 'task_claim', {'task_id': task['id'], 'client_id': 'test'})
        report = Path(task['paths']['reports']) / 'final.md'
        report.write_text('final', encoding='utf-8')
        collaboration.execute(self.cfg, 'artifact_register', {'task_id': task['id'], 'client_id': 'test', 'lease_token': claimed['lease_token'], 'kind': 'report', 'path': str(report), 'title': 'Final'})
        registered = workcenter.list_documents(self.cfg, {'intake_status': 'registered'})
        self.assertEqual(registered['total'], 1)
        self.assertEqual(registered['items'][0]['category'], 'report')

    def test_classification_project_name_persists_without_source_change(self):
        first = self.file('2026-09-25/task/outputs/a.md', 'original A')
        second = self.file('2026-09-25/task/outputs/b.md', 'original B')
        self.scan()
        before = {p: p.read_bytes() for p in (first, second)}
        doc = workcenter.lookup_document(self.cfg, str(first))
        saved = workcenter.classify(self.cfg, {'document_id': doc['id'], 'category': 'delivery', 'project_name': '我的实际项目'})
        self.assertTrue(saved['category_manual'])
        self.assertEqual(saved['category'], 'delivery')
        self.assertEqual(workcenter.lookup_document(self.cfg, str(second))['project_name'], '我的实际项目')
        self.scan()
        self.assertEqual(workcenter.lookup_document(self.cfg, str(first))['category'], 'delivery')
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(workcenter.list_projects(self.cfg)['items'][0]['name'], '我的实际项目')

    def test_external_sources_root_isolation_and_unknown_paths(self):
        external = self.base / 'external'
        self.file('2026-09-25/t/outputs/report.html', '<script>bad()</script>', external)
        self.scan(external)
        doc = workcenter.list_documents(self.cfg)['items'][0]
        preview = workcenter.read_document(self.cfg, doc['id'])
        self.assertEqual(preview['content'], '<script>bad()</script>')
        self.assertEqual(preview['format'], 'text')
        self.assertTrue(preview['preview_supported'])
        other = self.base / 'other'
        other.mkdir()
        cfg = {**self.cfg, 'ai_root': str(other)}
        self.assertEqual(workcenter.list_documents(cfg)['total'], 0)
        for action in (workcenter.read_document, workcenter.resolve_document):
            with self.assertRaises(ValueError):
                action(cfg, doc['id'])
            with self.assertRaises(ValueError):
                action(self.cfg, '../../private')
        with self.assertRaises(ValueError):
            workcenter.lookup_document(self.cfg, str(external / 'unindexed.md'))

    def test_missing_hardlink_symlink_and_database_link_rejected(self):
        path = self.file('report.md')
        self.scan()
        ident = workcenter.list_documents(self.cfg)['items'][0]['id']
        path.unlink()
        self.assertEqual(workcenter.list_documents(self.cfg)['items'][0]['status'], 'missing')
        with self.assertRaises(ValueError):
            workcenter.resolve_document(self.cfg, ident)
        original = self.file('original.txt')
        os.link(original, path)
        self.assertEqual(workcenter.list_documents(self.cfg)['items'][0]['status'], 'rejected')
        with self.assertRaises(ValueError):
            workcenter.read_document(self.cfg, ident)
        path.unlink()
        db = self.data / 'collaboration-maintenance.sqlite3'
        os.link(db, self.data / 'bad.sqlite3')
        with self.assertRaises(ValueError):
            workcenter.list_documents(self.cfg)

    def test_reparse_and_mid_preview_change_rejected(self):
        path = self.file('report.md', 'original')
        self.scan()
        ident = workcenter.list_documents(self.cfg)['items'][0]['id']
        original = os.lstat
        def reparse(value, *args, **kwargs):
            result = original(value, *args, **kwargs)
            if Path(value) == path:
                return mock.Mock(st_mode=result.st_mode, st_file_attributes=0x400, st_nlink=1)
            return result
        with mock.patch.object(workcenter.os, 'lstat', side_effect=reparse):
            self.assertEqual(workcenter.list_documents(self.cfg)['items'][0]['status'], 'rejected')
            with self.assertRaises(ValueError):
                workcenter.read_document(self.cfg, ident)
        real_open = os.open
        def changing_open(value, flags, *args, **kwargs):
            if Path(value) == path:
                path.write_text('changed size', encoding='utf-8')
            return real_open(value, flags, *args, **kwargs)
        with mock.patch.object(workcenter.os, 'open', side_effect=changing_open):
            with self.assertRaisesRegex(ValueError, '身份'):
                workcenter.read_document(self.cfg, ident)

    def test_preview_bounded_binary_unsupported_and_bad_params(self):
        self.file('large.md', 'x' * (workcenter.PREVIEW_BYTES + 10))
        pdf = self.file('report.pdf', '%PDF fixture')
        self.scan()
        docs = workcenter.list_documents(self.cfg)['items']
        large = next(d for d in docs if d['title'] == 'large.md')
        preview = workcenter.read_document(self.cfg, large['id'])
        self.assertTrue(preview['truncated'])
        self.assertEqual(len(preview['content']), workcenter.PREVIEW_BYTES)
        self.assertFalse(workcenter.read_document(self.cfg, workcenter.lookup_document(self.cfg, str(pdf))['id'])['preview_supported'])
        for params in ({'page': 0}, {'page_size': 101}, {'page': True}, {'query': []}, {'tool': []}):
            with self.assertRaises(ValueError):
                workcenter.list_documents(self.cfg, params)
        for body in ({'document_id': large['id'], 'category': 'arbitrary'}, {'document_id': large['id'], 'project_name': '\n'}, {'document_id': large['id']}):
            with self.assertRaises(ValueError):
                workcenter.classify(self.cfg, body)

    def test_stale_dataset_copy_entries_excluded_and_unscanned_projects_visible(self):
        path = self.file('2026-09-25/one/outputs/report.md')
        empty = self.root / '2026-09-25/two'
        empty.mkdir(parents=True)
        source = self.scan()
        stale = self.file('2026-09-25/one/Datasets/caption.txt')
        with maintenance._db() as con, con:
            con.execute('INSERT INTO inventory VALUES(?,?,?,?,?,?)', (source['id'], str(stale), stale.name, 6, 0, 'other_text'))
            con.execute('UPDATE sources SET truncated=1 WHERE id=?', (source['id'],))
        result = workcenter.list_documents(self.cfg)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['coverage']['excluded_documents'], 1)
        self.assertEqual(result['coverage']['partial_sources'], 1)
        projects = workcenter.list_projects(self.cfg)['items']
        self.assertEqual(len(projects), 2)
        self.assertTrue(all(p['origin'] == 'inferred' for p in projects))
        self.assertEqual(next(p for p in projects if p['name'] == 'two')['status'], 'unscanned')
        self.assertEqual(workcenter.list_projects(self.cfg, {'tool': 'codex', 'query': 'two'})['total'], 1)

    def test_actual_legacy_reports_and_metadata_project_discovery(self):
        with mock.patch.object(workcenter.management, 'reports', _REAL_REPORTS), mock.patch.object(workcenter.management, 'projects', _REAL_PROJECTS):
            path = self.file('00_Management/Reports/验收报告.md')
            self.file('40_Projects/真实项目/README.md')
            result = workcenter.list_documents(self.cfg)
            self.assertTrue(any(d['path'] == str(path) and d['intake_status'] == 'legacy' for d in result['items']))
            self.assertTrue(any(p['name'] == '真实项目' for p in workcenter.list_projects(self.cfg)['items']))

    def test_project_lookup_longest_ancestor_and_equal_path_registration_priority(self):
        parent = self.root / 'parent'
        child = parent / 'child'
        projects = [{'path': str(parent), 'name': 'parent', 'origin': 'registered'},
                    {'path': str(child), 'name': 'inferred child', 'origin': 'inferred'},
                    {'path': str(child).upper(), 'name': 'registered child', 'origin': 'registered'},
                    {'path': str(child), 'name': 'later discovery', 'origin': 'discovered'}]
        source = {'path': str(self.root), 'label': 'fallback', 'tool': 'any'}
        index = workcenter._project_index(projects)
        self.assertEqual(workcenter._project_for(child / 'outputs/report.md', source, index, self.root)['name'], 'registered child')
        self.assertEqual(workcenter._project_for(parent / 'report.md', source, index, self.root)['name'], 'parent')
        self.assertEqual(workcenter._project_for(self.root / 'parent-other/report.md', source, index, self.root)['name'], 'fallback')
        # No project containment scan remains in the indexed match path.
        with mock.patch.object(config, '_within', side_effect=AssertionError('linear containment scan')):
            self.assertEqual(workcenter._project_for(child / 'report.md', source, index, self.root)['origin'], 'registered')


if __name__ == '__main__':
    unittest.main()
