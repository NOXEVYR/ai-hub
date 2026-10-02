"""Synthetic media indexes and real HTTP range requests; no user files."""
import http.client
import json
import os
import threading
import urllib.parse
from unittest import mock

from aihub import api, config, media, service_control
from test_aihub import Fixture
import server


class MediaFixtures(Fixture):
    def setUp(self):
        super().setUp()
        self.assets = self.ai / 'Assets'
        self.output = self.ai / 'Output'
        self.assets.mkdir()
        self.output.mkdir()
        self.cfg.update(scan_roots=[str(self.assets)], output_roots=[str(self.output)])

    def add(self, name, category='image', generated=False, folder=None, data=b'0123456789'):
        folder = folder or (self.output if generated else self.assets)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(data)
        info = path.stat()
        self.db.conn.execute('INSERT OR REPLACE INTO files(path,parent,name,ext,size,mtime,category) VALUES(?,?,?,?,?,?,?)',
            (str(path), str(path.parent), path.name, path.suffix, info.st_size, info.st_mtime, category))
        if generated:
            self.db.conn.execute('INSERT OR REPLACE INTO images(path,parent,name,size,mtime,prompt,engine,seed,loras,steps) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (str(path), str(path.parent), path.name, info.st_size, info.st_mtime, 'sunset', 'comfyui', '42', '["test-lora"]', 20))
        self.db.commit()
        return path

    def listing(self, **params):
        status, _, body = api.media_list(self.db, self.cfg, params, None)
        self.assertEqual(status, 200, body)
        return json.loads(body)


class MediaIndex(MediaFixtures):
    def test_unifies_types_prioritizes_generation_metadata_and_deletes_only_outputs(self):
        self.cfg['scan_roots'] = [str(self.ai)]
        self.add('reference.png')
        self.add('generated.png', generated=True)
        self.add('clip.mp4', 'video')
        self.add('voice.wav', 'audio')
        listing = self.listing()
        self.assertEqual(listing['total'], 4)
        self.assertEqual(listing['counts'], {'image': 2, 'video': 1, 'audio': 1})
        generated = next(item for item in listing['items'] if item['name'] == 'generated.png')
        self.assertEqual((generated['prompt'], generated['seed'], generated['steps']), ('sunset', '42', 20))
        self.assertEqual(generated['loras'], ['test-lora'])
        self.assertTrue(generated['deletable'])
        self.assertTrue(all(not i['deletable'] for i in listing['items'] if i['name'] != 'generated.png'))

    def test_legacy_explicit_empty_sources_do_not_expose_history(self):
        self.add('reference.png')
        self.add('generated.png', generated=True)
        self.add('outside.mp4', 'video', folder=self.folder / 'Elsewhere')
        self.assertEqual(self.listing()['total'], 2)
        self.cfg.update(scan_roots=[], output_roots=[])
        self.assertEqual(self.listing()['items'], [])
        self.assertEqual(self.listing()['total'], 0)

    def test_managed_workspace_does_not_include_external_sources(self):
        external = self.folder / 'Elsewhere'
        self.add('outside.mp4', 'video', folder=external)
        self.cfg.update(workspace_managed=True, scan_roots=[str(external)], output_roots=[])
        self.assertEqual(self.listing()['total'], 0)

    def test_output_cache_dataset_and_hidden_records_stay_excluded(self):
        for name in ('datasets', 'cache', '.internal', 'private'):
            self.add('hidden.png', generated=True, folder=self.output / name)
        self.add('.private.png', generated=True)
        self.add('sample.png', generated=True)
        self.assertEqual([item['name'] for item in self.listing()['items']], ['sample.png'])

    def test_source_switch_and_unavailable_root_do_not_clear_database(self):
        original = self.add('reference.png')
        self.cfg['scan_roots'] = [str(self.ai / 'Missing')]
        self.assertEqual(self.listing()['total'], 0)
        self.assertIsNotNone(self.db.one('SELECT path FROM files WHERE path=?', (str(original),)))
        self.cfg['scan_roots'] = [str(self.assets)]
        self.assertEqual(self.listing()['total'], 1)

    def test_type_keyword_parent_sort_pagination_and_model_filter(self):
        path = self.add('sample.png', generated=True, folder=self.output / 'Nested')
        self.add('clip.mp4', 'video')
        self.db.upsert_model({'path': 'model', 'filename': 'style.safetensors'})
        self.db.conn.execute('INSERT INTO img_refs VALUES(?,?,?,?)', (str(path), 'model', 'lora', 'style.safetensors'))
        self.db.commit()
        self.assertEqual(self.listing(type='video')['total'], 1)
        self.assertEqual(self.listing(type='video')['counts']['image'], 1)
        self.assertEqual(self.listing(q='sunset', dir=str(self.output))['total'], 1)
        self.assertEqual(self.listing(model='style')['total'], 1)
        self.assertEqual(self.listing(model='not-present')['total'], 0)
        first = self.listing(size='1', sort='oldest')
        second = self.listing(size='1', page='2', sort='oldest')
        self.assertEqual((len(first['items']), len(second['items'])), (1, 1))
        self.assertNotEqual(first['items'][0]['path'], second['items'][0]['path'])
        self.assertEqual(api.media_list(self.db, self.cfg, {'type': 'bad'}, None)[0], 400)

    def test_stale_index_is_explicit_unavailable_and_never_deletable(self):
        path = self.add('sample.png', generated=True)
        path.write_bytes(b'replaced')
        item = self.listing()['items'][0]
        self.assertFalse(item['available'])
        self.assertFalse(item['deletable'])
        self.assertEqual(self.listing()['coverage']['count_scope'], 'indexed_records')

    def test_empty_page_and_keyword_keep_type_counts_and_directory_scope_readonly(self):
        self.add('reference.png')
        self.add('clip.mp4', 'video', folder=self.assets / 'Video')
        self.add('voice.wav', 'audio', folder=self.assets / 'Audio')
        statements = []
        self.db.conn.set_trace_callback(statements.append)
        try:
            empty_page = self.listing(type='video', size='1', page='99')
            no_match = self.listing(type='video', q='no-such-file')
        finally:
            self.db.conn.set_trace_callback(None)
        self.assertEqual(empty_page['items'], [])
        self.assertEqual(empty_page['total'], 1)
        self.assertEqual(empty_page['counts'], {'image': 1, 'video': 1, 'audio': 1})
        self.assertEqual(empty_page['dirs'], [str(self.assets / 'Video')])
        self.assertEqual(no_match['items'], [])
        self.assertEqual(no_match['total'], 0)
        self.assertEqual(no_match['dirs'], empty_page['dirs'])
        self.assertEqual(len(statements), 2)
        self.assertTrue(all(statement.lstrip().startswith('WITH ') for statement in statements))

    def test_preview_denies_unindexed_outside_changed_unknown_and_empty_source(self):
        path = self.add('clip.mp4', 'video')
        outside = self.add('outside.mp4', 'video', folder=self.folder / 'Outside')
        for target in (outside, self.assets / 'unindexed.mp4', self.add('code.html', 'video')):
            with self.assertRaises(media.MediaError):
                media.open_stream(self.db, self.cfg, str(target))
        path.write_bytes(b'changed')
        with self.assertRaises(media.MediaError):
            media.open_stream(self.db, self.cfg, str(path))
        path = self.add('clip.mp4', 'video')
        self.cfg['scan_roots'] = []
        with self.assertRaises(media.MediaError):
            media.open_stream(self.db, self.cfg, str(path))

    def test_preview_rejects_reparse_file(self):
        path = self.add('clip.mp4', 'video')
        original = os.lstat
        def marked(candidate, *args, **kwargs):
            value = original(candidate, *args, **kwargs)
            if str(candidate) == str(path):
                return type('Marked', (), {'st_mode': value.st_mode, 'st_file_attributes': 0x400})()
            return value
        with mock.patch('aihub.media.os.lstat', side_effect=marked):
            with self.assertRaises(media.MediaError):
                media.open_stream(self.db, self.cfg, str(path))

    def test_change_after_first_stat_cannot_stream_replaced_nonindexed_content(self):
        path = self.add('clip.mp4', 'video')
        original = media._available
        def change_after_validation(item, cfg):
            available = original(item, cfg)
            path.write_bytes(b'changed after first validation')
            return available
        with mock.patch.object(media, '_available', side_effect=change_after_validation):
            with self.assertRaises(media.MediaError):
                media.open_stream(self.db, self.cfg, str(path))

    def test_range_parser(self):
        for header, expected in [(None, (0, 10, 200)), ('bytes=0-2', (0, 3, 206)),
                ('bytes=7-', (7, 3, 206)), ('bytes=-4', (6, 4, 206)), ('bytes=0-99', (0, 10, 206))]:
            self.assertEqual(media.byte_range(header, 10), expected)
        for header in ('bytes=10-', 'bytes=3-1', 'bytes=-0', 'bytes=-', 'bytes=0-1,3-4', 'none'):
            with self.assertRaises(media.MediaError) as error:
                media.byte_range(header, 10)
            self.assertEqual(error.exception.status, 416)
        with self.assertRaises(media.MediaError):
            media.byte_range('bytes=0-', 0)


class MediaHTTP(MediaFixtures):
    def setUp(self):
        super().setUp()
        self.gate = service_control.ActivityGate()
        for obj, name, value in ((server, 'DB_OBJ', self.db), (server, 'CFG', self.cfg),
                                 (api, 'APP_CFG', self.cfg), (service_control, 'GATE', self.gate)):
            context = mock.patch.object(obj, name, value)
            context.start()
            self.addCleanup(context.stop)
        class Quiet(server.Handler):
            def log_message(self, *_):
                pass
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)
        super().tearDown()

    def request(self, path, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        conn.request('GET', path, headers=headers or {})
        response = conn.getresponse()
        status, result_headers, data = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return status, result_headers, data

    def test_real_listing_and_stream_ranges(self):
        path = self.add('测试.mp4', 'video')
        url = '/api/media/file?path=' + urllib.parse.quote(str(path))
        status, headers, body = self.request('/api/media?type=video')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['total'], 1)
        for supplied, code, expected in [(None, 200, b'0123456789'), ('bytes=2-5', 206, b'2345'),
                ('bytes=-2', 206, b'89'), ('bytes=8-', 206, b'89')]:
            status, headers, body = self.request(url, {'Range': supplied} if supplied else {})
            self.assertEqual((status, body), (code, expected))
            self.assertEqual(headers['Content-Type'], 'video/mp4')
            self.assertEqual(int(headers['Content-Length']), len(expected))
            if supplied == 'bytes=2-5':
                self.assertEqual(headers['Content-Range'], 'bytes 2-5/10')
        status, headers, _ = self.request(url, {'Range': 'bytes=99-'})
        self.assertEqual((status, headers['Content-Range']), (416, 'bytes */10'))

    def test_stream_reads_bounded_chunks_and_closes_after_disconnect(self):
        path = self.add('clip.mp4', 'video')
        status, headers, handle, count = media.open_stream(self.db, self.cfg, str(path))
        handle.close()
        reads, closed = [], []
        class BrokenFile:
            def read(self, size):
                reads.append(size)
                return b'x' * size
            def close(self):
                closed.append(True)
        class BrokenWriter:
            def write(self, _):
                raise BrokenPipeError()
        handler = object.__new__(server.Handler)
        handler.headers = {}
        handler.wfile = BrokenWriter()
        handler.send_response = lambda _: None
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        with mock.patch.object(media, 'open_stream', return_value=(status, headers, BrokenFile(), media.CHUNK_SIZE * 10)):
            handler._send_media(urllib.parse.urlparse('/api/media/file?path=' + urllib.parse.quote(str(path))))
        self.assertEqual(reads, [media.CHUNK_SIZE])
        self.assertEqual(closed, [True])
        self.assertTrue(self.gate.request_stop())

    def test_stopping_service_denies_new_media_reads(self):
        path = self.add('clip.mp4', 'video')
        self.assertTrue(self.gate.request_stop())
        status, _, body = self.request('/api/media/file?path=' + urllib.parse.quote(str(path)))
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)['code'], 'service_stopping')
