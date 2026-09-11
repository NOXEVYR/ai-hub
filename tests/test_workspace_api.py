"""Source boundaries and preview/apply integration using synthetic assets only."""
import json
from pathlib import Path
from unittest import mock

from aihub import api, config, images, scan, workspace
from test_aihub import Fixture


class WorkspaceAPI(Fixture):
    def setUp(self):
        super().setUp()
        self.app = self.folder / 'Application'
        self.app.mkdir()
        for key, value in [('APP_DIR', self.app), ('DATA_DIR', self.app), ('CONFIG_PATH', self.app / 'config.json')]:
            patcher = mock.patch.object(config, key, str(value))
            patcher.start()
            self.addCleanup(patcher.stop)
        config.save_config(self.cfg)
        workspace._previews.clear()

    def call(self, handler, params=None, body=None):
        status, _, raw = handler(self.db, self.cfg, params or {}, body)
        return status, json.loads(raw)

    def test_apply_busy_guard_and_shared_configuration(self):
        root = self.folder / 'New Studio'
        status, preview = self.call(api.workspace_environment_action, {'action': 'preview'}, {'mode': 'create', 'root': str(root)})
        self.assertEqual(status, 200)
        with mock.patch.object(api.organization, 'busy', return_value=True):
            self.assertEqual(self.call(api.workspace_environment_action, {'action': 'apply'}, {'token': preview['token']})[0], 409)
        self.assertFalse(root.exists())
        with mock.patch.object(api, 'APP_CFG', self.cfg), mock.patch.object(api.jobs, 'run_full_pipeline') as scan:
            status, applied = self.call(api.workspace_environment_action, {'action': 'apply'}, {'token': preview['token'], 'scan': True})
            self.assertEqual(status, 200, applied)
            scan.assert_called_once_with(self.db, self.cfg)
        self.assertTrue(self.cfg['workspace_managed'])
        self.assertEqual(self.cfg['ai_root'], str(root))
        self.assertTrue(applied['scan_started'])
        self.assertEqual(self.call(api.workspace_environment_action, {'action': 'apply'}, {'token': preview['token']})[0], 400)

    def test_managed_sources_filter_before_pagination_preserve_old_records(self):
        active = self.ai / 'Models_1'
        sibling = self.ai / 'ModelsX1'
        gallery = self.ai / 'verify_out'
        for folder in (active, sibling, gallery):
            folder.mkdir()
        self.cfg.update(workspace_managed=True, scan_roots=[str(active)], output_roots=[str(gallery)])
        for folder in (active, sibling):
            self.db.upsert_model({'path': str(folder / 'one.safetensors'), 'filename': 'one.safetensors', 'mtype': 'LoRA', 'rating': 8, 'notes': 'keep', 'missing': 0})
            image = folder / 'old.png'
            self.db.conn.execute('INSERT INTO images(path,parent,name,mtime) VALUES(?,?,?,?)', (str(image), str(folder), image.name, 2))
        image = gallery / 'brb.png'
        self.db.conn.execute('INSERT INTO images(path,parent,name,mtime) VALUES(?,?,?,?)', (str(image), str(gallery), image.name, 1))
        self.db.commit()
        status, result = self.call(api.models_list)
        self.assertEqual((status, result['total']), (200, 1))
        self.assertEqual(result['items'][0]['path'], str(active / 'one.safetensors'))
        status, result = self.call(api.images_list)
        self.assertEqual((status, result['total']), (200, 1))
        self.assertEqual(result['dirs'], [str(gallery)])
        self.assertEqual(self.db.one('SELECT COUNT(*) FROM models')[0], 2)
        self.assertEqual(self.db.one('SELECT COUNT(*) FROM models WHERE rating=8 AND notes=?', ('keep',))[0], 2)
        old_id = self.db.one('SELECT rowid_pk FROM models WHERE path=?', (str(sibling / 'one.safetensors'),))[0]
        self.assertEqual(self.call(api.model_detail, {'id': old_id})[0], 404)
        status, result = self.call(api.overview)
        self.assertEqual((result['indexed_records'], result['image_count']), (1, 1))
        self.cfg['scan_roots'] = []
        self.assertEqual(self.call(api.models_list)[1]['total'], 0)
        self.cfg['workspace_managed'] = False
        self.assertEqual(self.call(api.models_list)[1]['total'], 2)

    def test_gallery_scanner_does_not_enter_nested_dataset_or_cache(self):
        output = self.ai / '70_Output'
        for name in ('Datasets', 'CACHE', 'verify_out'):
            folder = output / name
            folder.mkdir(parents=True)
            (folder / 'image.png').write_bytes(b'fixture')
        self.cfg['output_roots'] = [str(output)]
        seen = []
        def analyze(path, *args):
            seen.append(path)
            return None, []
        with mock.patch.object(images, 'analyze_image', side_effect=analyze):
            images.run_image_scan(self.db, self.cfg)
        self.assertEqual(seen, [str(output / 'verify_out/image.png')])

    def test_explicit_empty_scan_sources_do_not_fall_back_to_whole_root(self):
        (self.ai / 'should-not-scan.txt').write_text('fixture')
        self.cfg['scan_roots'] = []
        result = scan.scan_all(self.db, self.cfg)
        self.assertEqual(result['file_count'], 0)
        self.assertEqual(self.db.one('SELECT COUNT(*) FROM files')[0], 0)
