"""Source regression fixtures never touch production assets or model networks."""
import json
import os
from unittest import mock

from aihub import api, jobs, scan
from test_aihub import Fixture


class FunctionalSourceBoundaries(Fixture):
    def setUp(self):
        super().setUp()
        self.current = self.ai / 'Models'
        self.current.mkdir()
        self.outputs = self.ai / 'Outputs'
        self.outputs.mkdir()
        self.old = self.folder / 'Previous'
        self.old.mkdir()
        self.cfg.update(workspace_managed=True, scan_roots=[str(self.current)], output_roots=[str(self.outputs)])
        self.model = self.current / 'model.safetensors'
        self.model.write_bytes(b'synthetic')
        self.old_model = self.old / 'old.safetensors'
        self.old_model.write_bytes(b'synthetic')
        for path in (self.model, self.old_model):
            self.db.upsert_model({'path': str(path), 'filename': path.name, 'mtype': 'LoRA', 'missing': 0, 'notes': 'keep'})
        self.mid = self.db.model_by_path(str(self.model))['rowid_pk']
        self.old_mid = self.db.model_by_path(str(self.old_model))['rowid_pk']

    def value(self, result):
        self.assertEqual(result[0], 200, result[2])
        return json.loads(result[2])

    def test_hidden_model_actions_reject_without_mutation_or_network(self):
        for empty in (False, True):
            with self.subTest(empty=empty), mock.patch.object(api.upd, 'Checker') as checker, mock.patch.object(api.upd, 'save_registry') as registry:
                if empty:
                    self.cfg['scan_roots'] = []
                for handler, body in ((api.model_edit, {'notes': 'changed'}), (api.model_set_source, {'url': 'https://example.invalid'}),
                                      (api.model_check, {}), (api.models_classify, {'ids': [self.old_mid], 'domain': 'image'})):
                    self.assertEqual(handler(self.db, self.cfg, {'id': self.old_mid}, body)[0], 404)
                checker.assert_not_called()
                registry.assert_not_called()
                self.assertEqual(self.db.model_by_path(str(self.old_model))['notes'], 'keep')
                self.assertEqual(self.db.query('SELECT * FROM model_labels'), [])

    def test_expired_workspace_requests_reject_all_model_actions(self):
        for root in (str(self.old), ''):
            for handler, body in ((api.model_edit, {'notes': 'changed'}), (api.model_set_source, {'url': ''}),
                                  (api.model_check, {}), (api.models_classify, {'ids': [self.mid], 'domain': 'image'}),
                                  (api.check_updates_start, {})):
                body = {**body, '_workspace_root': root}
                self.assertEqual(handler(self.db, self.cfg, {'id': self.mid}, body)[0], 409)
        self.assertEqual(self.db.model_by_path(str(self.model))['notes'], 'keep')

    def test_legacy_mode_and_valid_model_edit_remain_supported(self):
        self.assertEqual(api.model_edit(self.db, self.cfg, {'id': self.mid},
                         {'notes': 'valid', '_workspace_root': str(self.ai)})[0], 200)
        self.cfg['workspace_managed'] = False
        self.assertEqual(api.model_edit(self.db, self.cfg, {'id': self.old_mid}, {'notes': 'legacy'})[0], 200)

    def test_live_registered_compatibility_path_uses_selected_source(self):
        if os.name != 'nt':
            self.skipTest('Windows junction behavior')
        import _winapi
        link = self.ai / 'compatibility'
        _winapi.CreateJunction(str(self.current), str(link))
        try:
            alias = link / self.model.name
            self.cfg['aliases'] = {str(link): str(self.current)}
            self.db.upsert_model({'path': str(alias), 'filename': alias.name, 'mtype': 'LoRA', 'missing': 0})
            mid = self.db.model_by_path(str(alias))['rowid_pk']
            self.assertEqual(api.model_edit(self.db, self.cfg, {'id': mid}, {'notes': 'compatibility'})[0], 200)
            self.cfg['scan_roots'] = []
            self.assertEqual(api.model_edit(self.db, self.cfg, {'id': mid}, {'notes': 'wrong'})[0], 404)
            self.assertEqual(self.db.model_by_path(str(alias))['notes'], 'compatibility')
        finally:
            self.assertTrue(os.lstat(link).st_file_attributes & 0x400)
            os.rmdir(link)

    def test_update_check_filters_before_limit_and_empty_sources_do_not_call_checker(self):
        similar = self.ai / 'Models-Other'
        similar.mkdir()
        path = similar / 'wrong.safetensors'
        path.write_bytes(b'synthetic')
        self.db.upsert_model({'path': str(path), 'filename': path.name, 'mtype': 'LoRA', 'missing': 0, 'size': 999})
        captured = []
        class Checker:
            def __init__(self, *args, **kwargs): pass
            def check_many(self, rows):
                captured.extend(row['path'] for row in rows)
                return {}
        with mock.patch.object(jobs, 'start', side_effect=lambda name, target: target()), mock.patch.object(jobs.updater, 'Checker', Checker):
            jobs.run_update_check(self.db, self.cfg, limit=1)
            self.assertEqual(captured, [str(self.model)])
            captured.clear()
            self.cfg['scan_roots'] = []
            jobs.run_update_check(self.db, self.cfg)
            self.assertEqual(captured, [])

    def test_queued_update_snapshot_rejects_workspace_or_source_changes(self):
        captured = []
        with mock.patch.object(jobs, 'start', side_effect=lambda name, target: captured.append(target)):
            jobs.run_update_check(self.db, self.cfg)
        self.cfg['scan_roots'] = []
        with mock.patch.object(jobs.updater, 'Checker') as checker, self.assertRaisesRegex(ValueError, '来源已改变'):
            captured[0]()
        checker.assert_not_called()

    def test_inflight_update_cannot_write_model_after_workspace_switch(self):
        live_cfg = self.cfg
        previous_root = str(self.old)
        class Checker:
            def __init__(self, db, *args, **kwargs): self.db = db
            def check_many(self, rows):
                row = next(iter(rows))
                live_cfg['ai_root'] = previous_root
                self.db.upsert_model({'path': row['path'], 'notes': 'bad'})
                return {}
        with mock.patch.object(jobs, 'start', side_effect=lambda name, target: target()), mock.patch.object(jobs.updater, 'Checker', Checker):
            with self.assertRaisesRegex(ValueError, '工作环境'):
                jobs.run_update_check(self.db, self.cfg)
        self.assertEqual(self.db.model_by_path(str(self.model))['notes'], 'keep')

    def add_reference(self, path, model, role='LoRA'):
        self.db.conn.execute('INSERT INTO images(path,parent,name,mtime) VALUES(?,?,?,?)', (str(path), str(path.parent), path.name, 86401))
        self.db.conn.execute('INSERT INTO img_refs VALUES(?,?,?,?)', (str(path), str(model) if model else None, role, 'ghost' if not model else model.name))
        self.db.commit()

    def test_reference_stats_usage_and_details_use_both_active_sources(self):
        self.add_reference(self.outputs / 'current.png', self.model)
        self.add_reference(self.old / 'old.png', self.model)
        self.add_reference(self.outputs / 'old-model.png', self.old_model)
        self.add_reference(self.outputs / 'ghost.png', None)
        self.add_reference(self.old / 'old-ghost.png', None)
        self.db.upsert_model({'path': str(self.model), 'img_count': 99, 'days_used': 99})
        overview = self.value(api.overview(self.db, self.cfg, {}, None))
        self.assertEqual((overview['used_models'], overview['ref_count'], overview['ghost_count']), (1, 1, 1))
        model = self.value(api.model_detail(self.db, self.cfg, {'id': self.mid}, None))
        self.assertEqual((model['img_count'], model['days_used'], model['image_total']), (1, 1, 1))
        self.assertEqual(model['images'][0]['path'], str(self.outputs / 'current.png'))
        self.cfg['output_roots'] = []
        overview = self.value(api.overview(self.db, self.cfg, {}, None))
        self.assertEqual((overview['used_models'], overview['ref_count'], overview['ghost_count']), (0, 0, 0))
        listing = self.value(api.models_list(self.db, self.cfg, {'usage': 'used'}, None))
        self.assertEqual(listing['total'], 0)
        self.assertEqual(self.db.model_by_path(str(self.model))['img_count'], 99)

    def test_overlapping_scan_roots_are_validated_then_collapsed(self):
        text = self.current / 'one.txt'
        text.write_bytes(b'12345')
        for roots in ([str(self.ai), str(self.current)], [str(self.current), str(self.ai)], [str(self.ai), str(self.ai)],
                      [str(self.current), str(self.outputs)]):
            with self.subTest(roots=roots):
                self.cfg['scan_roots'] = roots
                result = scan.scan_all(self.db, self.cfg)
                self.assertEqual(result['file_count'], 2)
                view = self.value(api.overview(self.db, self.cfg, {}, None))
                self.assertEqual((view['total_files'], view['total_size'], view['unique_size']), (2, 14, 14))
        if os.name == 'nt':
            self.cfg['scan_roots'] = [str(self.ai), str(self.ai).upper()]
            self.assertEqual(scan.scan_all(self.db, self.cfg)['file_count'], 2)

    def test_unreadable_nested_source_is_not_hidden_by_parent_cover(self):
        self.cfg['scan_roots'] = [str(self.ai), str(self.current)]
        real_scan = os.scandir
        def scandir(path):
            if os.path.normcase(str(path)) == os.path.normcase(str(self.current)):
                raise PermissionError('synthetic child source')
            return real_scan(path)
        with mock.patch.object(scan.os, 'scandir', side_effect=scandir):
            with self.assertRaises(scan.ScanSourceError):
                scan.scan_all(self.db, self.cfg)

    def test_hardlink_logical_and_unique_model_capacity_stay_distinct(self):
        os.link(self.model, self.current / 'alias.safetensors')
        self.cfg['scan_roots'] = [str(self.ai), str(self.current)]
        result = scan.scan_all(self.db, self.cfg)
        self.assertEqual((result['file_count'], result['unique_size']), (2, 9))
        view = self.value(api.overview(self.db, self.cfg, {}, None))
        self.assertEqual((view['total_files'], view['total_size'], view['unique_size']), (2, 18, 9))
