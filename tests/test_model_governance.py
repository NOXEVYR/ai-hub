import errno
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aihub import api, classification, model_governance as governance
from test_aihub import Fixture


class Governance(Fixture):
    def weight(self, name, content=b'weight', **fields):
        path = self.ai / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.db.upsert_model({'path': str(path), 'filename': path.name, 'size': len(content), 'missing': 0, **fields})
        return path

    def report(self, params=None):
        status, _, body = api.model_governance_report(self.db, self.cfg, params or {}, None)
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def test_hardlinks_are_one_identity_not_reclaimable_duplicates(self):
        first = self.weight('models/one.safetensors')
        link = first.with_name('alias.safetensors')
        os.link(first, link)
        self.db.upsert_model({'path': str(link), 'filename': link.name, 'size': 6})
        report = self.report()
        s = report['summary']
        self.assertEqual((s['indexed_rows'], s['identified_files'], s['identified_file_bytes']), (2, 1, 6))
        self.assertEqual((s['indexed_bytes'], s['observed_path_bytes'], s['alias_path_bytes']), (12, 12, 6))
        self.assertEqual((s['hardlink_groups'], s['same_file_alias_groups']), (1, 1))
        self.assertIsNone(s['reclaimable_bytes'])
        self.assertFalse(report['safe_to_delete'])
        self.assertEqual(report['evidence']['content_hash'], 'not_run')

    def test_symlink_alias_is_not_itself_a_hardlink_group(self):
        first = self.weight('models/one.pth')
        alias = first.with_name('shortcut.pth')
        try:
            alias.symlink_to(first)
        except OSError as error:
            self.skipTest('This platform does not permit synthetic symlinks: ' + str(error))
        self.db.upsert_model({'path': str(alias), 'filename': alias.name, 'size': 6})
        s = self.report()['summary']
        self.assertEqual((s['identified_files'], s['link_alias_rows'], s['hardlink_groups']), (1, 1, 0))
        first.unlink()
        s = self.report()['summary']
        self.assertEqual((s['missing_rows'], s['identified_files']), (2, 0))

    def test_missing_denied_unavailable_and_not_regular_are_separate(self):
        missing = self.weight('missing.gguf'); missing.unlink()
        denied = self.weight('denied.gguf')
        unavailable = self.weight('offline.gguf')
        self.db.upsert_model({'path': str(self.ai), 'filename': 'directory', 'size': 100})
        real_stat = os.stat
        def observed(path, *args, **kwargs):
            if path == str(denied): raise PermissionError(errno.EACCES, 'synthetic denied')
            if path == str(unavailable): raise OSError(errno.EIO, 'synthetic unavailable')
            return real_stat(path, *args, **kwargs)
        with mock.patch.object(classification.os, 'stat', side_effect=observed):
            s = self.report()['summary']
        self.assertEqual([s[k] for k in ('missing_rows', 'denied_rows', 'unavailable_rows', 'not_regular_rows')], [1, 1, 1, 1])
        self.assertEqual((s['state_conflict_rows'], s['identified_files']), (1, 0))

    def test_unknown_identity_does_not_merge_unrelated_paths_or_count_as_zero(self):
        self.weight('a.pth'); self.weight('b.pth')
        real_stat = os.stat
        def observed(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if str(path).endswith('.pth'):
                return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=0, st_size=info.st_size)
            return info
        with mock.patch.object(classification.os, 'stat', side_effect=observed): report = self.report()
        s = report['summary']
        self.assertEqual((s['identity_unknown_rows'], s['identified_files'], s['unidentified_path_bytes']), (2, 0, 12))
        self.assertFalse(report['identity_coverage_complete'])
        self.assertIsNone(s['allocated_disk_bytes'])

    def test_case_distinct_paths_are_not_merged_on_case_sensitive_filesystems(self):
        first = self.weight('UPPER.pth', b'A')
        second = self.weight('upper.pth', b'BB')
        if os.path.samefile(first, second): self.skipTest('Filesystem is case-insensitive')
        s = self.report()['summary']
        self.assertEqual((s['identified_files'], s['identified_file_bytes']), (2, 3))

    def test_uncertain_intake_never_rejects_all_pth_and_bin(self):
        self.weight('site-packages/entry.pth', b'import synthetic')
        self.weight('cache/data.bin', b'cache')
        self.weight('.no_exist/hash/model.safetensors', b'')
        self.weight('tests/fixtures/tiny.safetensors', b'fixture')
        self.weight('models/real.pth', b'\x80\x04pickle-not-executed')
        self.weight('models/pytorch_model.bin', b'X' * 2048)
        self.weight('elsewhere/generic.bin', b'X' * 2048)
        self.weight('models/component.tz3', b'private-format')
        with mock.patch('builtins.open', side_effect=AssertionError('Must not read file contents')): report = self.report()
        states = {Path(i['path']).name: i['intake'] for i in report['items']}
        self.assertEqual(states['entry.pth'], 'nonmodel_candidate')
        self.assertEqual(states['real.pth'], 'model_candidate')
        self.assertEqual(states['pytorch_model.bin'], 'model_candidate')
        self.assertEqual(states['generic.bin'], 'unknown')
        self.assertEqual(states['component.tz3'], 'unknown')
        self.assertEqual(report['summary']['nonmodel_candidate_rows'], 4)

    def test_state_size_conflicts_and_index_bytes_remain_separate(self):
        first = self.weight('live.gguf', b'1234', missing=1, size=100)
        missing = self.weight('gone.gguf', b'123456', missing=1); missing.unlink()
        before = [dict(r) for r in self.db.query('SELECT * FROM models')]
        changes = self.db.conn.total_changes
        with mock.patch('os.scandir', side_effect=AssertionError('No directory traversal')): report = self.report()
        self.assertEqual([dict(r) for r in self.db.query('SELECT * FROM models')], before)
        self.assertEqual(self.db.conn.total_changes, changes)
        s = report['summary']
        self.assertEqual((s['indexed_bytes'], s['identified_file_bytes'], s['size_conflict_rows'], s['state_conflict_rows']), (106, 4, 1, 1))
        self.assertTrue(first.is_file())
        self.assertTrue(report['items'][0]['size_conflict'])

    def test_changed_alias_metadata_is_not_reported_as_exact_identity_length(self):
        first = self.weight('one.gguf'); other = self.weight('two.gguf')
        real_stat = os.stat; first_info = real_stat(first)
        def observed(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path == str(other):
                return SimpleNamespace(st_mode=info.st_mode, st_dev=first_info.st_dev, st_ino=first_info.st_ino,
                                       st_size=900, st_mtime=first_info.st_mtime, st_mtime_ns=first_info.st_mtime_ns, st_nlink=2)
            return info
        with mock.patch.object(classification.os, 'stat', side_effect=observed): report = self.report()
        self.assertEqual((report['summary']['changed_identity_groups'], report['summary']['identified_file_bytes']), (1, 0))
        self.assertFalse(report['identity_coverage_complete'])

    def test_bounded_keyset_batches_have_no_cross_batch_total_claim(self):
        for i in range(3): self.weight(f'{i}.gguf')
        first = self.report({'limit': '2'})
        self.assertEqual(first['summary']['indexed_rows'], 2)
        self.assertFalse(first['coverage']['whole_scope'])
        self.assertFalse(first['coverage']['cross_batch_totals_supported'])
        next_params = {'after_id': str(first['coverage']['next_after_id']), 'scope_token': first['coverage']['scope_token'], 'limit': '2'}
        second = self.report(next_params)
        self.assertEqual(second['summary']['indexed_rows'], 1)
        self.assertFalse(second['coverage']['whole_scope'])
        self.assertFalse(second['coverage']['has_more'])
        self.cfg['ai_root'] = str(self.folder / 'another')
        self.assertEqual(api.model_governance_report(self.db, self.cfg, next_params, None)[0], 409)

    def test_limit_rejected_without_filesystem_probes_and_missing_cursor_rejected(self):
        for params in ({'limit': '5001'}, {'limit': '0'}, {'limit': '-1'}, {'limit': ['1', '2']}, {'after_id': 'no'}):
            with self.subTest(params=params), mock.patch.object(classification.FileFacts, 'stat', side_effect=AssertionError('unexpected probe')):
                self.assertEqual(api.model_governance_report(self.db, self.cfg, params, None)[0], 400)
        self.assertEqual(api.model_governance_report(self.db, self.cfg, {'after_id': '1'}, None)[0], 409)

    def test_managed_source_boundary_and_explicit_empty_sources(self):
        inside = self.weight('models/one.gguf'); self.weight('other/private.gguf')
        self.cfg.update(workspace_managed=True, scan_roots=[str(inside.parent)])
        self.assertEqual(self.report()['summary']['indexed_rows'], 1)
        self.cfg['scan_roots'] = []
        self.assertEqual(self.report()['summary']['indexed_rows'], 0)

    def test_mid_request_source_switch_rejects_stale_report(self):
        original = governance.build_report
        def switching(rows):
            result = original(rows); self.cfg['ai_root'] = str(self.folder / 'changed'); return result
        with mock.patch.object(governance, 'build_report', side_effect=switching):
            self.assertEqual(api.model_governance_report(self.db, self.cfg, {}, None)[0], 409)

    def test_private_database_fields_not_exposed_and_get_route_is_registered(self):
        self.weight('model.gguf', notes='PRIVATE-NOTE', source_url='https://example.invalid/?secret=PRIVATE-URL')
        self.assertNotIn('PRIVATE-', json.dumps(self.report()))
        self.assertIn(('GET', r'^/api/models/governance$', api.model_governance_report), api.ROUTES)
        self.assertNotIn('POST', [method for method, route, _ in api.ROUTES if 'models/governance' in route])

    def test_empty_and_unknown_sizes_are_not_false_certainty(self):
        empty = governance.build_report([])
        self.assertEqual(empty['summary']['identified_file_bytes'], 0)
        self.assertIsNone(empty['summary']['model_package_count'])
        rows = [{'rowid_pk': 1, 'path': str(self.ai / 'missing.pth'), 'size': None}]
        s = governance.build_report(rows)['summary']
        self.assertEqual((s['indexed_bytes'], s['indexed_size_unknown_rows']), (0, 1))

    def test_equal_name_size_content_without_hash_never_means_safe_redundancy(self):
        self.weight('first/same.gguf', b'same')
        self.weight('second/same.gguf', b'same')
        report = self.report()
        self.assertEqual((report['summary']['identified_files'], report['summary']['identified_file_bytes']), (2, 8))
        self.assertEqual(report['summary']['same_file_alias_groups'], 0)
        self.assertIsNone(report['summary']['reclaimable_bytes'])
        self.assertFalse(report['safe_to_delete'])

    def test_limit_does_not_probe_the_extra_cursor_row(self):
        paths = [str(self.weight(f'bounded/{i}.gguf')) for i in range(3)]
        real_stat, probes = os.stat, []
        def observed(path, *args, **kwargs):
            if path in paths: probes.append(path)
            return real_stat(path, *args, **kwargs)
        with mock.patch.object(classification.os, 'stat', side_effect=observed):
            report = self.report({'limit': '2'})
        self.assertEqual(probes, paths[:2])
        self.assertTrue(report['coverage']['has_more'])

    def test_invalid_path_is_uncertainty_not_missing(self):
        report = governance.build_report([{'rowid_pk': 1, 'path': 'bad\0path.gguf', 'size': 4, 'missing': 0}])
        self.assertEqual(report['summary']['invalid_path_rows'], 1)
        self.assertEqual(report['summary']['missing_rows'], 0)
        self.assertEqual(report['summary']['state_conflict_rows'], 0)
