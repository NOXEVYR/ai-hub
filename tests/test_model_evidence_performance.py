import json
import os
from unittest import mock

from aihub import api, classification, meta, scan
from test_aihub import Fixture


class ModelEvidence(Fixture):
    def file(self, relative, size):
        path = self.ai / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('wb') as stream:
            stream.truncate(size)
        return path

    def index(self, path, **fields):
        self.db.upsert_model({'path': str(path), 'filename': path.name,
            'ext': path.suffix, 'size': path.stat().st_size, 'mtime': path.stat().st_mtime,
            'mtype': 'Unknown', **fields})

    def payload(self, endpoint, params=None):
        response = endpoint(self.db, self.cfg, params or {}, None)
        self.assertEqual(response[0], 200)
        return json.loads(response[2])

    def test_scan_retains_weight_evidence_and_demotes_old_sentinels_without_deletion(self):
        sentinel = self.file('backup/private-sentinel.bin', 21)
        cache = self.file('cache/_nop_math.bin', 7609)
        runtime = self.file('runtime/v8_context_snapshot.bin', 759648)
        vision = self.file('Library/Vision/clip-vit-large-patch14-336.bin', 2048)
        adapter = self.file('Library/IPAdapter/ip-adapter-faceid-plusv2_sdxl.bin', 2048)
        speech = self.file('Packages/TTS/IndexTTS/hf_cache/campplus_cn_common.bin', 2048)
        hf = self.file('cache/models--Intel--dpt/snapshots/rev/pytorch_model.bin', 2048)
        generic = self.file('Library/tts/acoustic.bin', 1024*1024)
        self.index(sentinel, notes='keep history', rating=9)
        self.db.conn.execute('INSERT INTO model_labels(model_path,domain,purposes) VALUES(?,?,?)',
            (str(sentinel), 'unknown', '[]'))
        self.db.commit()
        result = scan.scan_all(self.db, self.cfg)
        scan.build_models(self.db, self.cfg, result)
        rows = {r['path']: r for r in self.db.query('SELECT * FROM files')}
        for p in [sentinel, cache, runtime]:
            self.assertEqual(rows[str(p)]['category'], 'other')
            self.assertTrue(p.exists())
        for p in [vision, adapter, speech, hf, generic]:
            self.assertEqual(rows[str(p)]['category'], 'model')
            self.assertIsNotNone(self.db.model_by_path(str(p)))
        old = self.db.model_by_path(str(sentinel))
        self.assertEqual((old['rating'], old['notes']), (9, 'keep history'))
        self.assertEqual(len(self.db.query('SELECT * FROM model_labels')), 1)
        listing = self.payload(api.models_list)
        item = next(r for r in listing['items'] if r['path'] == str(sentinel))
        self.assertTrue(item['classification']['model_evidence_pending'])
        overview = self.payload(api.overview)
        self.assertNotIn(str(sentinel), [r['path'] for r in overview['recent_models']])
        self.assertEqual(len(overview['recent_models']), 5)

    def test_manual_and_catalog_evidence_are_retained(self):
        manual = self.file('unusual/opaque.bin', 8)
        self.index(manual)
        mid = self.db.model_by_path(str(manual))['rowid_pk']
        response = api.models_classify(self.db, self.cfg, {}, {'ids':[mid], 'model_role':'Vision'})
        self.assertEqual(response[0], 200)
        item = self.payload(api.models_list)['items'][0]
        self.assertEqual(item['classification']['model_evidence'], 'manual')
        alias = self.ai / 'unusual' / 'alias.bin'
        os.link(manual, alias)
        result = scan.scan_all(self.db, self.cfg)
        scan.build_models(self.db, self.cfg, result)
        self.assertEqual(len(result['model_groups']), 1)
        self.assertEqual(self.db.model_by_path(str(manual))['missing'], 0)
        self.assertEqual(self.payload(api.models_list)['total'], 1)
        self.write_json('Catalogs/models.json', {'models': [{'id':'known', 'canonical_path':str(manual), 'category':'Vision'}]})
        self.assertEqual(self.payload(api.models_list)['items'][0]['classification']['model_evidence'], 'manual')
        self.db.conn.execute('DELETE FROM model_labels')
        self.db.commit()
        self.assertEqual(self.payload(api.models_list)['items'][0]['classification']['model_evidence'], 'catalog')

    def test_requests_share_one_stat_per_path_and_next_request_refreshes_identity(self):
        first = self.file('one/style.safetensors', 100)
        alias = self.ai / 'alias.safetensors'
        os.link(first, alias)
        self.index(first, mtype='LoRA', alt_paths=json.dumps([str(alias)]))
        self.index(alias, mtype='LoRA')
        mid = self.db.model_by_path(str(first))['rowid_pk']
        self.assertEqual(api.models_classify(self.db, self.cfg, {},
            {'ids':[mid], 'domain':'video', 'purposes':['motion']})[0], 200)
        # Keep only the primary manual row so a replaced alias must lose propagation.
        self.db.conn.execute('DELETE FROM model_labels WHERE model_path=?', (str(alias),))
        self.db.commit()
        original = os.stat
        for endpoint in (api.overview, api.models_list):
            seen = []
            def counted(path, *args, **kwargs):
                seen.append(classification.path_key(path))
                return original(path, *args, **kwargs)
            with mock.patch.object(os, 'stat', counted):
                result = self.payload(endpoint)
            self.assertEqual(seen.count(classification.path_key(first)), 1)
            self.assertEqual(seen.count(classification.path_key(alias)), 1)
            self.assertEqual(result.get('total', result.get('model_count')), 1)
        alias.unlink()
        alias.write_bytes(b'different file')
        changed = self.payload(api.models_list)
        self.assertEqual(changed['total'], 2)
        second = next(r for r in changed['items'] if r['path'] == str(alias))
        self.assertNotEqual(second['classification']['domain_source'], 'manual')
        alias.unlink()
        missing = next(r for r in self.payload(api.models_list)['items'] if r['path'] == str(alias))
        self.assertEqual(missing['missing'], 1)

    def test_bin_evidence_size_change_visible_on_next_request(self):
        path = self.file('tts/acoustic.bin', 1024*1024)
        self.index(path)
        self.assertFalse(self.payload(api.models_list)['items'][0]['classification']['model_evidence_pending'])
        path.write_bytes(b'tiny')
        self.assertTrue(self.payload(api.models_list)['items'][0]['classification']['model_evidence_pending'])
        self.assertEqual(self.payload(api.overview)['recent_models'], [])

    def test_suffix_alone_is_never_evidence(self):
        self.assertEqual(meta.classify_file('.bin', 'models'), ('other', None))
        self.assertEqual(meta.bin_model_evidence('cache/random.bin', 10*1024*1024), 'candidate')
        self.assertEqual(meta.bin_model_evidence('tts/private-sentinel.bin', 21), 'candidate')
