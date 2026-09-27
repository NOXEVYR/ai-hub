"""Synthetic discovery evidence. No target app, shell or native configuration executes."""
import ctypes
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from aihub import harness_discovery as discovery


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class ProcessKernel:
    def __init__(self, entries, denied=()):
        self.entries, self.denied = entries, set(denied)
        self.index, self.opens, self.closed = -1, [], []
        self.CreateToolhelp32Snapshot = Function(self.snapshot)
        self.Process32FirstW = Function(self.first)
        self.Process32NextW = Function(self.next)
        self.OpenProcess = Function(self.open)
        self.QueryFullProcessImageNameW = Function(self.query)
        self.CloseHandle = Function(lambda handle: self.closed.append(handle) or True)

    def snapshot(self, flags, pid):
        assert (flags, pid) == (2, 0), 'Only process-name snapshots allowed'
        return 11

    def fill(self, pointer):
        if self.index >= len(self.entries):
            return False
        pid, name, _path = self.entries[self.index]
        pointer._obj.th32ProcessID = pid
        pointer._obj.szExeFile = name
        return True

    def first(self, handle, pointer):
        assert handle == 11
        self.index = 0
        return self.fill(pointer)

    def next(self, handle, pointer):
        assert handle == 11
        self.index += 1
        return self.fill(pointer)

    def open(self, rights, inherit, pid):
        assert rights == 0x1000 and inherit is False, 'Query-limited process rights only'
        self.opens.append(pid)
        return 0 if pid in self.denied else pid + 1000

    def query(self, handle, flags, buffer, length):
        assert flags == 0
        buffer.value = next(path for pid, _name, path in self.entries if pid == handle - 1000)
        length._obj.value = len(buffer.value)
        return True


class RegistryKey:
    def __init__(self, values=None, children=None):
        self.values, self.children = values or {}, children or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class Registry(types.SimpleNamespace):
    HKEY_CURRENT_USER = 1
    HKEY_LOCAL_MACHINE = 2
    KEY_WOW64_64KEY = 256
    KEY_WOW64_32KEY = 512
    KEY_READ = 1
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self, app_path=None, rows=None, denied=False):
        self.app_path, self.rows, self.denied = app_path, rows or {}, denied
        self.reads = []

    def OpenKey(self, parent, path, _reserved, access):
        assert access in (257, 513), 'Only read registry access allowed'
        if self.denied:
            raise PermissionError('synthetic restricted metadata')
        if isinstance(parent, RegistryKey):
            if path not in parent.children:
                raise FileNotFoundError(path)
            return parent.children[path]
        if path.endswith('App Paths\\Qoder.exe') or path.endswith('App Paths\\qoder.exe'):
            if self.app_path:
                return RegistryKey({'': self.app_path})
        if path.endswith('CurrentVersion\\Uninstall'):
            return RegistryKey(children={name: RegistryKey(values) for name, values in self.rows.items()})
        raise FileNotFoundError('absent synthetic key')

    def QueryValueEx(self, key, name):
        assert name in ('', 'DisplayName', 'DisplayIcon', 'InstallLocation'), 'Unexpected native registry field'
        self.reads.append((key, name))
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name], self.REG_SZ

    def QueryInfoKey(self, key):
        return len(key.children), len(key.values), 0

    def EnumKey(self, key, index):
        return list(key.children)[index]


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='aihub-discovery-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / 'Workspace'
        self.root.mkdir()
        self.environment = {'PATH': '', 'APPDATA': str(self.base / 'Roaming'),
                            'LOCALAPPDATA': str(self.base / 'Local'), 'ProgramFiles': str(self.base / 'Programs'),
                            'ProgramFiles(x86)': str(self.base / 'ProgramsX86')}
        for patch in (mock.patch.dict(os.environ, self.environment),
                      mock.patch.object(Path, 'home', return_value=self.base / 'Home'),
                      mock.patch.object(discovery, '_local_drive', return_value=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def file(self, relative):
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic metadata only, never execute')
        return path

    def run_discovery(self, processes=(), registry=(), summaries=None, **kwargs):
        summaries = summaries or {'app_paths': discovery._summary(), 'uninstall_registry': discovery._summary()}
        with mock.patch.object(discovery, '_process_candidates', return_value=(list(processes), discovery._summary())), \
                mock.patch.object(discovery, '_registry_candidates', return_value=(list(registry), summaries)):
            return discovery.discover({'ai_root': str(self.root)}, **kwargs)

    def candidate(self, identifier, path, source, pid=None):
        return discovery._candidate(discovery._BY_ID[identifier], path, source, pid)

    def test_running_qoder_custom_location_is_discovered_without_known_disk_path(self):
        executable = self.file('unusual portable folder/Qoder.exe')
        result = self.run_discovery(processes=[self.candidate('qoder', executable, 'running_process', 123)])
        row = next(item for item in result['items'] if item['id'] == 'qoder')
        self.assertEqual(row['executable'], str(executable))
        self.assertTrue(row['running'])
        self.assertEqual(row['process_count'], 1)
        self.assertEqual(row['evidence_sources'], ['running_process'])
        self.assertEqual(row['connection_mode'], 'manual')
        self.assertFalse(row['registered'])
        self.assertFalse(row['verified'])
        self.assertNotIn('pid', row)
        self.assertNotIn('client_online', row)

    def test_canonical_executable_deduplicates_processes_and_install_evidence(self):
        executable = self.file('portable/Qoder.exe')
        alias = str(executable.parent / '.' / executable.name)
        processes = [self.candidate('qoder', executable, 'running_process', 10),
                     self.candidate('qoder', alias, 'running_process', 11),
                     self.candidate('qoder', executable, 'running_process', 11)]
        registry = [self.candidate('qoder', executable, 'app_paths'),
                    self.candidate('qoder', executable, 'uninstall_registry')]
        result = self.run_discovery(processes, registry)
        items = [row for row in result['items'] if row['id'] == 'qoder']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['process_count'], 2)
        self.assertEqual(set(items[0]['evidence_sources']), {'running_process', 'app_paths', 'uninstall_registry'})

    def test_multiple_installs_with_same_suggested_id_remain_separate_candidates(self):
        first, second = self.file('one/Qoder.exe'), self.file('two/Qoder.exe')
        result = self.run_discovery(registry=[self.candidate('qoder', first, 'app_paths'), self.candidate('qoder', second, 'uninstall_registry')])
        self.assertEqual(len([row for row in result['items'] if row['id'] == 'qoder']), 2)

    def test_path_shims_and_workspace_common_locations_are_metadata_only(self):
        suffix = '.cmd' if os.name == 'nt' else ''
        cli = self.file('bin/qodercli' + suffix)
        claude = self.file('bin/claude' + suffix)
        gui = self.file('Workspace/10_Apps/Qoder/qoder.exe')
        with mock.patch.dict(os.environ, {'PATH': str(cli.parent)}), \
                mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('contents read')), \
                mock.patch.object(Path, 'read_text', side_effect=AssertionError('contents read')):
            result = self.run_discovery(max_checks=4096)
        by_id = {row['id']: row for row in result['items']}
        self.assertEqual(by_id['qoder-cli']['executable'], str(cli))
        self.assertEqual(by_id['claude']['executable'], str(claude))
        self.assertEqual(by_id['qoder']['executable'], str(gui))
        self.assertIn('path', by_id['qoder-cli']['evidence_sources'])
        self.assertIn('common_location', by_id['qoder']['evidence_sources'])
        self.assertFalse(any(row['registered'] or row['verified'] for row in result['items']))
        self.assertFalse((self.root / 'data').exists())

    def test_unknown_name_and_missing_path_are_not_invented_as_installed(self):
        unknown = self.file('bin/unknown.exe')
        with mock.patch.dict(os.environ, {'PATH': str(unknown.parent)}):
            result = self.run_discovery(registry=[self.candidate('qoder', self.base / 'missing/Qoder.exe', 'app_paths')])
        self.assertFalse(result['items'])

    def test_count_path_directory_and_time_budgets_are_explicit(self):
        with mock.patch.dict(os.environ, {'PATH': os.pathsep.join(str(self.base / ('bin%d' % i)) for i in range(80))}):
            result = self.run_discovery(max_checks=20, max_path_dirs=4)
        self.assertEqual(result['checks'], 20)
        self.assertTrue(result['truncated'])
        self.assertTrue(result['sources']['path']['truncated'])
        with mock.patch.object(discovery, 'MAX_SECONDS', 0):
            result = self.run_discovery()
        self.assertEqual(result['checks'], 0)
        self.assertTrue(result['truncated'])
        for budget in (0, -1, True, 5000):
            with self.assertRaises(ValueError):
                self.run_discovery(max_checks=budget)

    def test_access_limit_surfaces_separately_from_no_results(self):
        summaries = {'app_paths': discovery._summary(), 'uninstall_registry': dict(discovery._summary('partial'), limited=True)}
        result = self.run_discovery(summaries=summaries)
        self.assertTrue(result['limited'])
        self.assertTrue(any('受限' in message for message in result['limitations']))
        self.assertFalse(result['scan_scope']['reads_configuration_contents'])
        self.assertFalse(result['scan_scope']['process_command_lines'])
        self.assertFalse(result['scan_scope']['recursive'])

    def test_network_and_control_character_paths_are_rejected(self):
        for value in ('relative/Qoder.exe', '//server/share/Qoder.exe', '\\\\server\\share\\Qoder.exe', str(self.base / 'Qoder.exe') + '\n'):
            self.assertIsNone(discovery._local_path(value))
        path = self.file('Qoder.exe')
        with mock.patch.object(discovery, '_local_drive', return_value=False):
            self.assertIsNone(discovery._canonical_file(path, {}))

    def test_registry_path_parser_drops_arguments_and_nonpath_values(self):
        executable = self.base / 'Qoder.exe'
        self.assertEqual(discovery._registry_executable('"%s",-2' % executable), str(executable))
        self.assertEqual(discovery._registry_executable(str(executable)), str(executable))
        for text in ('"%s" --token=never-publish' % executable, '%UNKNOWN_PRIVATE_VAR%/Qoder.exe',
                     'https://example.invalid/Qoder.exe', '"%s",0 --extra' % executable, None):
            self.assertIsNone(discovery._registry_executable(text))

    def test_reparse_parent_is_rejected_before_any_descendant_or_realpath_probe(self):
        link = self.base / 'redirect'
        target = link / 'Qoder.exe'
        original_lstat = Path.lstat
        checked = []
        def lstat(path):
            checked.append(path)
            if path == link:
                return types.SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            if link in path.parents:
                raise AssertionError('must not probe through reparse parent')
            return original_lstat(path)
        skipped = {}
        with mock.patch.object(Path, 'lstat', lstat), mock.patch.object(os.path, 'realpath', side_effect=AssertionError('realpath could follow UNC')):
            self.assertIsNone(discovery._canonical_file(target, {}, skipped))
        self.assertNotIn(target, checked)
        self.assertEqual(skipped, {'reparse_path': 1})

    def test_reparse_skip_is_reported_as_limited_discovery(self):
        executable = self.file('Qoder.exe')
        def reject(_value, _drives, skipped):
            skipped['reparse_path'] = skipped.get('reparse_path', 0) + 1
            return None
        with mock.patch.object(discovery, '_canonical_file', side_effect=reject):
            result = self.run_discovery(processes=[self.candidate('qoder', executable, 'running_process', 1)], max_checks=1)
        self.assertTrue(result['limited'])
        self.assertEqual(result['skipped']['reparse_path'], 1)
        self.assertTrue(any('联接' in message for message in result['limitations']))
        self.assertFalse(result['scan_scope']['native_call_hard_timeout'])

    @unittest.skipUnless(os.name == 'nt', 'Windows native metadata contract')
    def test_native_process_api_uses_only_names_images_and_query_limited_rights(self):
        qoder = self.file('custom/Qoder.exe')
        kernel = ProcessKernel([(10, 'node.exe', str(self.base / 'node.exe')),
                                (11, 'Qoder.exe', str(qoder)), (12, 'Qoder.exe', str(qoder))], denied={12})
        with mock.patch.object(ctypes, 'WinDLL', return_value=kernel), mock.patch.object(ctypes, 'get_last_error', return_value=18):
            items, summary = discovery._process_candidates(time.monotonic() + 10)
        self.assertEqual(kernel.opens, [11, 12])
        self.assertEqual([item['path'] for item in items], [str(qoder)])
        self.assertTrue(summary['limited'])
        self.assertEqual(summary['checked'], 3)
        self.assertEqual(set(kernel.closed), {1011, 11})

    @unittest.skipUnless(os.name == 'nt', 'Windows native metadata contract')
    def test_native_process_enumeration_stops_at_budget_and_closes_snapshot(self):
        kernel = ProcessKernel([(10 + i, 'node.exe', 'unused') for i in range(10)])
        with mock.patch.object(ctypes, 'WinDLL', return_value=kernel), \
                mock.patch.object(ctypes, 'get_last_error', return_value=18), mock.patch.object(discovery, 'MAX_PROCESSES', 2):
            items, summary = discovery._process_candidates(time.monotonic() + 10)
        self.assertFalse(items)
        self.assertEqual(summary['checked'], 2)
        self.assertTrue(summary['truncated'])
        self.assertEqual(kernel.closed, [11])

    @unittest.skipUnless(os.name == 'nt', 'Windows native registry contract')
    def test_registry_reads_only_allowlisted_metadata_and_ignores_unrelated_products(self):
        qoder = self.file('custom/Qoder.exe')
        registry = Registry(app_path='"%s"' % qoder, rows={
            'known': {'DisplayName': 'Qoder', 'InstallLocation': str(qoder.parent), 'DisplayIcon': '"%s",0' % qoder,
                      'UninstallString': 'never read credentials or arguments'},
            'unrelated': {'DisplayName': 'Private unrelated product', 'InstallLocation': 'never read'}})
        with mock.patch.dict(sys.modules, {'winreg': registry}):
            items, sources = discovery._registry_candidates(time.monotonic() + 10)
        self.assertTrue(any(item['source'] == 'app_paths' and item['path'] == str(qoder) for item in items))
        self.assertTrue(any(item['source'] == 'uninstall_registry' and Path(item['path']).name.casefold() == 'qoder.exe' for item in items))
        self.assertFalse(any(name == 'UninstallString' for _key, name in registry.reads))
        self.assertFalse(any(key.values.get('DisplayName') == 'Private unrelated product' and name != 'DisplayName' for key, name in registry.reads))
        self.assertFalse(any(value['limited'] for value in sources.values()))

    @unittest.skipUnless(os.name == 'nt', 'Windows native registry contract')
    def test_registry_restricted_and_truncated_sources_are_reported(self):
        with mock.patch.dict(sys.modules, {'winreg': Registry(denied=True)}):
            items, sources = discovery._registry_candidates(time.monotonic() + 10)
        self.assertFalse(items)
        self.assertTrue(all(value['limited'] for value in sources.values()))
        registry = Registry(rows={str(i): {'DisplayName': 'Unknown'} for i in range(10)})
        with mock.patch.dict(sys.modules, {'winreg': registry}), mock.patch.object(discovery, 'MAX_REGISTRY_ENTRIES', 2):
            _items, sources = discovery._registry_candidates(time.monotonic() + 10)
        self.assertEqual(sources['uninstall_registry']['checked'], 2)
        self.assertTrue(sources['uninstall_registry']['truncated'])


if __name__ == '__main__':
    unittest.main()
