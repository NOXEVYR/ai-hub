"""Disposable-fixture checks for the local, per-listing parent validation scope."""
import os
import unittest
from pathlib import Path
from unittest import mock
import test_workcenter as fixture
from aihub import config, workcenter, collaboration_api


class LocalListingValidationTests(unittest.TestCase):
    setUp = fixture.WorkcenterTests.setUp
    file = fixture.WorkcenterTests.file
    scan = fixture.WorkcenterTests.scan

    def prepare(self, count=8):
        files = [self.file('2026-11-11/project/outputs/report-%02d.md' % n) for n in range(count)]
        self.source = self.scan()
        self.addCleanup(workcenter._REMOVAL_PREVIEWS.clear)
        return files

    def test_repeated_parent_is_checked_once_but_each_leaf_is_checked(self):
        files = self.prepare()
        checked = set()
        with mock.patch.object(config, '_check_ancestors', wraps=config._check_ancestors) as parents:
            with mock.patch.object(workcenter.os, 'lstat', wraps=os.lstat) as leaf:
                for path in files:
                    self.assertEqual(workcenter._path_status(path, self.root, checked), 'available')
            seen = [os.fspath(call.args[0]) for call in leaf.call_args_list]
            self.assertTrue(all(str(path) in seen for path in files))
        self.assertEqual(parents.call_count, 1)
        self.assertEqual(len(checked), 1)

    def test_strict_default_rechecks_every_parent(self):
        files = self.prepare()
        with mock.patch.object(config, '_check_ancestors', wraps=config._check_ancestors) as parents:
            for path in files:
                self.assertEqual(workcenter._path_status(path, self.root), 'available')
        self.assertEqual(parents.call_count, len(files))

    def test_failed_parent_is_never_reused(self):
        path = self.prepare(1)[0]
        checked = set()
        with mock.patch.object(config, '_check_ancestors', side_effect=ValueError('blocked')) as parents:
            for _ in range(2):
                self.assertEqual(workcenter._path_status(path, self.root, checked), 'rejected')
        self.assertEqual(parents.call_count, 2)
        self.assertFalse(checked)

    def test_path_spelling_and_boundary_checks_precede_parent_reuse(self):
        path = self.prepare(1)[0]
        checked = set()
        self.assertEqual(workcenter._path_status(path, self.root, checked), 'available')
        for bad in (str(path.parent / '..' / path.name), str(path) + '\x01', 'relative.md', str(self.root)):
            self.assertEqual(workcenter._path_status(bad, self.root, checked), 'rejected')
        self.assertEqual(workcenter._path_status(path, self.base / 'different', checked), 'rejected')

    def test_leaf_loss_and_hardlink_are_detected_despite_parent_reuse(self):
        path = self.prepare(1)[0]
        checked = set()
        self.assertEqual(workcenter._path_status(path, self.root, checked), 'available')
        path.unlink()
        self.assertEqual(workcenter._path_status(path, self.root, checked), 'missing')
        source = self.file('original.txt')
        os.link(source, path)
        self.assertEqual(workcenter._path_status(path, self.root, checked), 'rejected')

    def test_no_parent_validation_is_shared_between_list_requests(self):
        files = self.prepare()
        parent = config._key(files[0].parent)
        with mock.patch.object(config, '_check_ancestors', wraps=config._check_ancestors) as checks:
            workcenter.list_documents(self.cfg)
            workcenter.list_documents(self.cfg, {'page': 2})
        self.assertEqual(sum(config._key(call.args[0]) == parent for call in checks.call_args_list), 2)

    def test_list_and_project_snapshots_are_metadata_only(self):
        self.prepare()
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('body read')):
            with mock.patch.object(workcenter, '_catalog', wraps=workcenter._catalog) as catalog:
                self.assertEqual(workcenter.list_documents(self.cfg)['total'], 8)
                workcenter.list_projects(self.cfg, {'entry_kind': 'all'})
        self.assertEqual([call.kwargs for call in catalog.call_args_list], [{'metadata_only': True}] * 2)
        self.assertFalse((self.data / 'workcenter.sqlite3').exists())

    def test_read_resolve_and_lookup_do_not_reuse_listing_validation(self):
        path = self.prepare(1)[0]
        ident = workcenter.list_documents(self.cfg)['items'][0]['id']
        for action, arg in ((workcenter.read_document, ident), (workcenter.resolve_document, ident), (workcenter.lookup_document, str(path))):
            with mock.patch.object(workcenter, '_catalog', wraps=workcenter._catalog) as catalog:
                action(self.cfg, arg)
            self.assertTrue(catalog.called)
            self.assertTrue(all(not call.kwargs.get('metadata_only', False) for call in catalog.call_args_list))

    def test_parent_change_after_listing_blocks_read_and_resolve(self):
        path = self.prepare(1)[0]
        ident = workcenter.list_documents(self.cfg)['items'][0]['id']
        original = config._check_ancestors
        def blocked(parent):
            if config._key(parent) == config._key(path.parent):
                raise ValueError('simulated reparse replacement')
            return original(parent)
        with mock.patch.object(config, '_check_ancestors', side_effect=blocked):
            for action in (workcenter.read_document, workcenter.resolve_document):
                with self.assertRaises(ValueError):
                    action(self.cfg, ident)
        self.assertEqual(path.read_text(encoding='utf-8'), 'sample')

    def test_no_workspace_or_data_directory_reuse(self):
        self.prepare(1)
        self.assertEqual(workcenter.list_documents(self.cfg)['total'], 1)
        other = self.base / 'other'
        other.mkdir()
        self.assertEqual(workcenter.list_documents({**self.cfg, 'ai_root': str(other)})['total'], 0)
        empty_data = self.base / 'empty-data'
        empty_data.mkdir()
        with mock.patch.object(config, 'DATA_DIR', str(empty_data)):
            self.assertEqual(workcenter.list_documents(self.cfg)['total'], 0)
        self.assertEqual(workcenter.list_documents(self.cfg)['total'], 1)

    def test_remove_restore_and_classification_stay_immediately_visible(self):
        path = self.prepare(1)[0]
        original = path.read_bytes()
        doc = workcenter.list_documents(self.cfg)['items'][0]
        with mock.patch.object(workcenter, '_catalog', wraps=workcenter._catalog) as catalog:
            workcenter.classify(self.cfg, {'document_id': doc['id'], 'category': 'delivery', 'project_name': 'kept'})
            body = {'_workspace_root': str(self.root), 'document_id': doc['id']}
            preview = workcenter.removal_preview(self.cfg, body)
            workcenter.removal_apply(self.cfg, {**body, 'preview_token': preview['preview_token']})
        self.assertTrue(all(not call.kwargs.get('metadata_only', False) for call in catalog.call_args_list))
        self.assertEqual(workcenter.list_documents(self.cfg)['total'], 0)
        self.assertEqual(workcenter.removal_list(self.cfg)['total'], 1)
        workcenter.removal_restore(self.cfg, body)
        restored = workcenter.list_documents(self.cfg)['items'][0]
        self.assertEqual((restored['category'], restored['project_name']), ('delivery', 'kept'))
        self.assertEqual(path.read_bytes(), original)

    def test_revoked_source_never_survives_a_previous_listing(self):
        self.prepare(1)
        self.assertEqual(workcenter.list_documents(self.cfg)['total'], 1)
        body = {'_workspace_root': str(self.root), 'entity_type': 'source', 'entity_id': self.source['id']}
        preview = collaboration_api.execute(self.cfg, 'record_delete_preview', body)
        collaboration_api.execute(self.cfg, 'record_delete_apply', {**body, 'preview_token': preview['preview_token']})
        self.assertEqual(workcenter.list_documents(self.cfg)['total'], 0)
        collaboration_api.execute(self.cfg, 'record_restore', body)
        self.assertEqual(workcenter.list_documents(self.cfg)['total'], 1)


if __name__ == '__main__':
    unittest.main()
