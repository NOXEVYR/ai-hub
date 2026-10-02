"""Project roles, full-catalog filters and evidence boundaries on disposable roots."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from aihub import config, collaboration, collaboration_maintenance as maintenance, harnesses, projects, workcenter


class ProjectCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='aihub-project-catalog-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        self.data = self.base / 'app/data'
        self.data.mkdir(parents=True)
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True}
        for name, value in [('DATA_DIR', str(self.data)), ('APP_DIR', str(self.data.parent)),
                            ('REPORTS_DIR', str(self.data / 'reports'))]:
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        for identifier in ('codex', 'zcode'):
            harnesses.save(self.cfg, {'id': identifier, 'revision': 0, 'connection_mode': 'mcp_stdio'})
        self.management = mock.patch.object(workcenter.management, 'projects', return_value={'items': []}).start()
        self.reports = mock.patch.object(workcenter.management, 'reports', return_value=[]).start()
        self.addCleanup(mock.patch.stopall)

    def file(self, relative, text='fixture'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def scan(self, path=None, tool='codex', label='fixture source'):
        source = maintenance.add_source(self.cfg, {'path': str(path or self.root), 'label': label, 'tool': tool})
        maintenance.scan_source(self.cfg, {'source_id': source['id']})
        return source

    def test_default_formal_projects_roles_and_stable_report_deep_link(self):
        registered = self.root / '40_Projects/Formal'
        registered.mkdir(parents=True)
        candidate = self.file('2026-10-01/Unregistered/outputs/report.md')
        template = self.root / '40_Projects/_TEMPLATE_PROJ-YYYY-NNN_项目名'
        template.mkdir()
        self.management.return_value = {'items': [
            {'path': str(registered), 'name': 'Formal', 'registered': True, 'id': 'legacy-id', 'type': 'creative'},
            {'path': str(template), 'name': template.name, 'registered': False}]}
        self.scan()
        result = workcenter.list_projects(self.cfg)
        self.assertEqual([p['name'] for p in result['items']], ['Formal'])
        self.assertEqual(result['counts'], {'project': 1, 'candidate': 1, 'template': 1, 'source': 1})
        self.assertEqual(result['catalog_total'], 4)
        formal = result['items'][0]
        self.assertEqual(formal['registry_id'], 'legacy-id')
        self.assertEqual(formal['classification_evidence'], 'project_registry')
        self.assertEqual(formal['task_count'], 0)
        self.assertEqual(formal['linked_tools'], [])
        doc = workcenter.lookup_document(self.cfg, str(candidate))
        found = workcenter.list_projects(self.cfg, {'project_id': doc['project_id']})
        self.assertEqual(found['total'], 1)
        self.assertEqual(found['items'][0]['entry_kind'], 'candidate')
        self.assertEqual(workcenter.list_documents(self.cfg, {'project_id': doc['project_id']})['total'], 1)
        self.assertEqual(workcenter.list_projects(self.cfg, {'entry_kind': 'template'})['items'][0]['classification_evidence'], 'directory_role_hint')
        self.assertFalse((self.data / 'workcenter.sqlite3').exists())

    def test_search_filters_pagination_and_facets_use_all_projects(self):
        candidates = []
        for number in range(105):
            path = self.root / ('40_Projects/Project-%03d' % number)
            path.mkdir(parents=True)
            candidates.append({'path': str(path), 'name': path.name, 'registered': False})
        self.management.return_value = {'items': candidates}
        first = self.file('40_Projects/Project-104/outputs/report-target.md')
        self.file('40_Projects/Project-104/outputs/another.md')
        self.scan(tool='codex')
        result = workcenter.list_projects(self.cfg, {'entry_kind': 'candidate', 'page': 2, 'page_size': 100})
        self.assertEqual(result['total'], 105)
        self.assertEqual(len(result['items']), 5)
        self.assertEqual(result['counts']['candidate'], 105)
        empty = workcenter.list_projects(self.cfg, {'entry_kind': 'candidate', 'query': 'Project-103'})
        self.assertEqual(empty['total'], 1)
        self.assertEqual(empty['items'][0]['document_count'], 0)
        filtered = workcenter.list_projects(self.cfg, {'entry_kind': 'candidate', 'source_type': 'codex', 'query': first.name, 'category': 'report'})
        self.assertEqual(filtered['total'], 1)
        self.assertEqual(filtered['items'][0]['document_count'], 2)
        self.assertEqual(filtered['counts']['candidate'], 105)
        self.assertEqual(next(f for f in filtered['facets']['source_types'] if f['value'] == 'codex')['count'], 2)

    def test_external_template_ancestors_do_not_change_formal_projects_or_deep_links(self):
        for relative_root in ('Templates/Studio/Workspace', 'Studio/Templates'):
            with self.subTest(workspace=relative_root):
                self.root = self.base / relative_root
                self.root.mkdir(parents=True)
                self.cfg = {**self.cfg, 'ai_root': str(self.root)}
                report = self.file('40_Projects/Formal/outputs/report.md')
                grouped_template = self.root / '40_Projects/Templates/Sample'
                grouped_template.mkdir(parents=True)
                self.management.return_value = {'items': [
                    {'path': str(self.root / '40_Projects/Formal'), 'name': 'Formal', 'registered': True},
                    {'path': str(grouped_template), 'name': 'Sample', 'registered': False}]}
                self.reports.return_value = [{'path': str(report), 'name': report.name, 'group': 'fixture', 'size': 7, 'mtime': 0}]
                result = workcenter.list_projects(self.cfg)
                self.assertEqual([p['name'] for p in result['items']], ['Formal'])
                self.assertEqual(result['counts'], {'project': 1, 'candidate': 0, 'template': 1, 'source': 0})
                doc = workcenter.lookup_document(self.cfg, str(report))
                deep_link = workcenter.list_projects(self.cfg, {'project_id': doc['project_id']})
                self.assertEqual(deep_link['total'], 1)
                self.assertEqual(deep_link['items'][0]['entry_kind'], 'project')
                self.assertEqual(workcenter.list_documents(self.cfg, {'project_id': doc['project_id']})['total'], 1)
                templates = workcenter.list_projects(self.cfg, {'entry_kind': 'template'})
                self.assertEqual([p['name'] for p in templates['items']], ['Sample'])

    def test_external_candidates_only_use_their_own_template_directory_name(self):
        external = self.base / 'Templates/External'
        ordinary = external / '2026-10-01/Ordinary'
        explicit = external / '2026-10-01/_TEMPLATE_SAMPLE'
        ordinary.mkdir(parents=True)
        explicit.mkdir()
        self.management.return_value = {'items': [
            {'path': str(ordinary), 'name': ordinary.name, 'registered': False},
            {'path': str(explicit), 'name': explicit.name, 'registered': False}]}
        result = workcenter.list_projects(self.cfg, {'entry_kind': 'all'})
        by_name = {p['name']: p['entry_kind'] for p in result['items']}
        self.assertEqual(by_name, {'Ordinary': 'candidate', '_TEMPLATE_SAMPLE': 'template'})
        self.assertEqual(result['counts'], {'project': 0, 'candidate': 1, 'template': 1, 'source': 0})

    def test_valid_creation_manifest_and_invalid_or_linked_manifests(self):
        paths = []
        for name in ('Created', 'Invalid', 'Linked'):
            path = self.root / '40_Projects' / name
            path.mkdir(parents=True)
            paths.append(path)
            documents = projects._documents(path, [])
            manifest = path / '.aihub-project.json'
            manifest.write_bytes(documents[str(manifest)])
        invalid = paths[1] / '.aihub-project.json'
        value = json.loads(invalid.read_text(encoding='utf-8'))
        value['root'] = str(paths[0])
        invalid.write_text(json.dumps(value), encoding='utf-8')
        os.link(paths[2] / '.aihub-project.json', self.root / 'retained-manifest.json')
        self.management.return_value = {'items': [{'name': p.name, 'path': str(p), 'registered': False} for p in paths]}
        result = workcenter.list_projects(self.cfg)
        self.assertEqual([p['name'] for p in result['items']], ['Created'])
        self.assertEqual(result['items'][0]['registration_status'], 'created')
        self.assertEqual(result['items'][0]['classification_evidence'], 'creation_manifest')
        self.assertEqual(result['counts']['candidate'], 2)

    def test_task_and_tool_summaries_are_real_bounded_and_not_project_registration(self):
        task = None
        for number in range(22):
            current = collaboration.execute(self.cfg, 'task_create', {'project': 'Task directory', 'title': 'Actual task %02d' % number,
                                                                    'description': 'fixture', 'target_tool': 'codex'})
            task = task or current
        self.assertEqual(workcenter.list_projects(self.cfg)['total'], 0)
        candidate = workcenter.list_projects(self.cfg, {'entry_kind': 'candidate'})['items'][0]
        self.assertEqual(candidate['task_count'], 22)
        self.assertEqual(len(candidate['tasks']), workcenter.TASK_SUMMARY_LIMIT)
        self.assertEqual(candidate['task_status_counts'], {'queued': 22})
        self.assertEqual(candidate['linked_tools'], ['codex'])
        self.assertEqual(candidate['artifact_count'], 0)
        self.assertEqual(candidate['association_evidence'], 'registered_tasks_and_artifacts_only')
        self.assertNotIn('owner', candidate)
        self.assertNotIn('author', candidate)
        collaboration.execute(self.cfg, 'client_heartbeat', {'client_id': 'fixture-worker', 'tool': 'codex', 'name': 'Fixture worker', 'protocol_version': 1})
        claimed = collaboration.execute(self.cfg, 'task_claim', {'task_id': task['id'], 'client_id': 'fixture-worker'})
        path = Path(task['paths']['reports']) / 'report.md'
        path.write_text('submitted fixture', encoding='utf-8')
        collaboration.execute(self.cfg, 'artifact_register', {'task_id': task['id'], 'client_id': 'fixture-worker',
                              'lease_token': claimed['lease_token'], 'kind': 'report', 'path': str(path), 'title': 'Actual report', 'category': 'report'})
        self.assertEqual(workcenter.list_projects(self.cfg)['total'], 0)
        project = workcenter.list_projects(self.cfg, {'entry_kind': 'candidate'})['items'][0]
        self.assertEqual(project['artifact_count'], 1)
        self.assertEqual(project['entry_kind'], 'candidate')
        self.assertNotEqual(project['classification_evidence'], 'project_registry')
        doc = workcenter.lookup_document(self.cfg, str(path))
        self.assertEqual(doc['project_origin'], 'discovered')
        image = Path(task['paths']['outputs']) / 'result.png'
        image.write_bytes(b'fixture image')
        collaboration.execute(self.cfg, 'artifact_register', {'task_id': task['id'], 'client_id': 'fixture-worker',
                              'lease_token': claimed['lease_token'], 'kind': 'output', 'path': str(image), 'title': 'Fixture image', 'category': 'delivery'})
        all_artifacts = workcenter.list_projects(self.cfg, {'entry_kind': 'candidate'})['items'][0]
        self.assertEqual(all_artifacts['artifact_count'], 2)
        self.assertEqual(all_artifacts['document_count'], 1)
        self.scan(path.parent, tool='codex')
        path.write_text('changed submitted fixture', encoding='utf-8')
        maintenance.scan_source(self.cfg, {'source_id': maintenance.list_sources(self.cfg)['items'][0]['id']})
        changed = workcenter.lookup_document(self.cfg, str(path))
        self.assertEqual(changed['intake_status'], 'registered')
        self.assertEqual(changed['submission_evidence'], 'snapshot_changed')
        with self.assertRaises(ValueError):
            workcenter.resolve_document(self.cfg, changed['id'])

    def test_multi_source_project_filter_does_not_collapse_to_any(self):
        parent = self.root / '40_Projects/Mixed'
        codex_path = self.file('40_Projects/Mixed/codex/report-a.md')
        self.file('40_Projects/Mixed/zcode/report-b.md')
        self.management.return_value = {'items': [{'name': 'Mixed', 'path': str(parent), 'registered': True}]}
        self.scan(parent / 'codex', tool='codex', label='Codex source')
        self.scan(parent / 'zcode', tool='zcode', label='ZCode source')
        result = workcenter.list_projects(self.cfg, {'source_type': 'codex'})
        self.assertEqual(result['total'], 1)
        project = result['items'][0]
        self.assertEqual(project['tool'], 'any')
        self.assertEqual(project['source_types'], ['codex', 'zcode'])
        self.assertEqual(project['document_count'], 2)
        self.assertEqual(project['linked_tools'], [])
        self.assertEqual(workcenter.lookup_document(self.cfg, str(codex_path))['project_id'], project['id'])

    def test_manual_category_does_not_follow_file_replacement_or_legacy_label(self):
        path = self.file('report.md', 'old fixture')
        self.scan()
        doc = workcenter.lookup_document(self.cfg, str(path))
        saved = workcenter.classify(self.cfg, {'document_id': doc['id'], 'category': 'plan'})
        self.assertTrue(saved['category_manual'])
        path.rename(path.with_name('retained-old.md'))
        path.write_text('unrelated replacement', encoding='utf-8')
        current = workcenter.lookup_document(self.cfg, str(path))
        self.assertFalse(current['category_manual'])
        self.assertEqual(current['category_override_status'], 'identity_changed')
        self.assertEqual(current['classification_status'], 'needs_review')
        self.assertEqual(current['category'], 'report')
        with closing(sqlite3.connect(self.data / 'workcenter.sqlite3')) as con, con:
            con.execute('DROP TABLE document_label_evidence')
        before = (self.data / 'workcenter.sqlite3').read_bytes()
        legacy = workcenter.lookup_document(self.cfg, str(path))
        self.assertEqual(legacy['category_override_status'], 'legacy_unverified')
        self.assertFalse(legacy['category_manual'])
        self.assertEqual((self.data / 'workcenter.sqlite3').read_bytes(), before)
        workcenter.classify(self.cfg, {'document_id': doc['id'], 'category': 'plan'})
        backups = list(self.data.glob('workcenter.sqlite3.pre-label-evidence-*.backup'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as con:
            self.assertEqual(con.execute('SELECT category FROM document_labels').fetchone()[0], 'plan')

    def test_live_overlapping_source_wins_stale_discovery_without_submission_downgrade(self):
        nested = self.root / 'nested'
        path = self.file('nested/report.md', 'old fixture')
        parent = self.scan(self.root, label='parent')
        old_nested = self.scan(nested, label='nested')
        nested.rename(self.root / 'retained-nested')
        nested.mkdir()
        path.write_text('replacement fixture', encoding='utf-8')
        maintenance.scan_source(self.cfg, {'source_id': parent['id']})
        self.assertFalse(maintenance.source_identity_matches(old_nested))
        doc = workcenter.lookup_document(self.cfg, str(path))
        self.assertEqual(doc['status'], 'available')
        self.assertEqual(set(doc['source_labels']), {'parent', 'nested'})
        self.assertEqual(workcenter.resolve_document(self.cfg, doc['id']), path)

    def test_invalid_params_and_isolated_empty_workspace(self):
        for params in ({'entry_kind': 'made-up'}, {'entry_kind': []}, {'source_type': []}, {'query': []}, {'page': False}):
            with self.assertRaises(ValueError):
                workcenter.list_projects(self.cfg, params)
        result = workcenter.list_projects(self.cfg)
        self.assertEqual(result['catalog_total'], 0)
        self.assertEqual(result['counts'], {'project': 0, 'candidate': 0, 'template': 0, 'source': 0})
        self.assertEqual(result['facets']['source_types'], [])
        self.assertFalse((self.data / 'collaboration.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
