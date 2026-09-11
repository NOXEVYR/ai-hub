import tempfile
from pathlib import Path, PureWindowsPath
import unittest
from unittest import mock

from aihub import tool_adapters


class ToolAdaptersTests(unittest.TestCase):
    def test_unconfigured_root_only_uses_user_and_path_candidates(self):
        with mock.patch.object(tool_adapters.shutil, 'which', return_value=None), \
                mock.patch.dict(tool_adapters.os.environ,
                                {'APPDATA': 'User/Roaming', 'LOCALAPPDATA': 'User/Local'}):
            for tool_id in tool_adapters._TOOLS:
                candidates = tool_adapters._candidates(tool_id, {})
                self.assertTrue(candidates)
                self.assertTrue(all(str(p).startswith('User') for p in candidates))
                self.assertFalse(any('10_Apps' in str(p) or 'tool' in p.parts for p in candidates))

    def test_portable_windows_root_uses_its_own_drive_not_a_fixed_drive(self):
        # Pure path fixture verifies Windows candidate construction without probing drives.
        for drive in ('Q:', 'R:'):
            root = PureWindowsPath(drive + '/Assets/AI')
            with mock.patch.object(tool_adapters, 'Path', PureWindowsPath), \
                    mock.patch.object(PureWindowsPath, 'home', create=True,
                                      return_value=PureWindowsPath('C:/Users/Synthetic')), \
                    mock.patch.object(tool_adapters.shutil, 'which', return_value=None):
                for key, folder in [('zcode', 'ZCode'), ('workbuddy', 'WorkBuddy')]:
                    paths = tool_adapters._candidates(key, {'ai_root': str(root)})
                    self.assertIn(PureWindowsPath(drive + '/tool') / folder / (folder + '.exe'), paths)
                    self.assertIn(root / '10_Apps' / folder / (folder + '.exe'), paths)
                self.assertIn(root / '10_Apps/DeepSeek_Harness_Launcher/Start-DSH-Fast.ps1',
                              tool_adapters._candidates('dsh', {'ai_root': str(root)}))

    def test_installed_entry_does_not_certify_current_version(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'tool.exe'
            executable.touch()
            with mock.patch.object(tool_adapters, '_candidates', return_value=[executable]):
                rows = tool_adapters.status({})
            for row in rows:
                self.assertTrue(row['detected'])
                self.assertIn('当前安装版本仍须确认', row['notes'][0])

    def test_missing_is_not_available_or_isolated(self):
        with mock.patch.object(tool_adapters, '_candidates', return_value=[]):
            rows = tool_adapters.status({})
        self.assertEqual([r['id'] for r in rows], ['codex', 'zcode', 'dsh', 'workbuddy'])
        for row in rows:
            self.assertFalse(row['available'])
            self.assertFalse(row['detected'])
            self.assertIsNone(row['executable'])
            self.assertEqual(row['enforcement'], 'soft_rules_only')

    def test_detection_only_stats_entry_does_not_read_or_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / 'synthetic.exe'
            entry.write_bytes(b'not an executable')
            with mock.patch.object(tool_adapters, '_candidates', return_value=[entry]), \
                    mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('read')), \
                    mock.patch.object(Path, 'read_text', side_effect=AssertionError('read')):
                rows = tool_adapters.status({})
            self.assertTrue(all(r['detected'] for r in rows))
            self.assertTrue(all(r['executable'] == str(entry) for r in rows))
            self.assertEqual(entry.read_bytes(), b'not an executable')

    def test_project_files_are_proposals_with_native_names_and_no_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project's $(never-run) `file"
            generated = {key: tool_adapters.project_rules(key, root) for key in tool_adapters._TOOLS}
            self.assertFalse(root.exists())
            self.assertEqual([generated[k][0][0] for k in generated],
                             ['AGENTS.md', 'AGENTS.md', 'AGENTS.md', 'CODEBUDDY.md'])
            self.assertEqual(len({generated[k][0][1] for k in generated}), 1)
            command = generated['codex'][1][1]
            self.assertIn("project''s $(never-run) `file' --sandbox workspace-write", command)
            self.assertIn('--ask-for-approval on-request', command)
            self.assertNotIn('dangerously-bypass', command)
            for files in generated.values():
                for relative, _ in files:
                    self.assertFalse(Path(relative).is_absolute())
                    self.assertNotIn('..', Path(relative).parts)

    def test_invalid_inputs_cannot_inject_multiline_shell_command(self):
        with self.assertRaises(ValueError):
            tool_adapters.project_rules('unknown', str(Path.cwd()))
        for path in ('relative/path', str(Path.cwd()) + '\nmalicious', ''):
            with self.assertRaises(ValueError):
                tool_adapters.project_rules('codex', path)


if __name__ == '__main__':
    unittest.main()
