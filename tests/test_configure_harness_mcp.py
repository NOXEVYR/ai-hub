import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.configure_harness_mcp import ConfigError, configure, config_path


class HarnessConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.app = self.base / 'app'
        (self.app / 'tools').mkdir(parents=True)
        (self.app / 'tools/aihub_mcp.py').write_text('# fixture', encoding='utf-8')
        self.python = self.base / 'python.exe'
        self.python.write_bytes(b'fixture')
        self.codex = self.base / 'codex.exe'
        self.codex.write_bytes(b'fixture')

    def run_config(self, tool, **kwargs):
        return configure(tool, self.app, self.python, home=self.home, env={}, codex=self.codex, **kwargs)

    def test_dry_run_never_creates_files_or_starts_cli(self):
        before = sorted(self.base.rglob('*'))
        for tool in ('codex', 'zcode', 'workbuddy'):
            with patch('tools.configure_harness_mcp.subprocess.run') as run:
                result = self.run_config(tool)
                run.assert_not_called()
            self.assertEqual(result['status'], 'would_create')
        self.assertEqual(sorted(self.base.rglob('*')), before)

    def test_new_json_config_backup_and_idempotence(self):
        for tool in ('zcode', 'workbuddy'):
            result = self.run_config(tool, apply=True)
            path, backup = Path(result['path']), Path(result['backup'])
            self.assertTrue((backup / 'before-absent.txt').is_file())
            self.assertEqual((backup / 'after.json').read_bytes(), path.read_bytes())
            self.assertEqual(result['status'], 'configured_pending_handshake')
            self.assertEqual(self.run_config(tool, apply=True)['status'], 'already_configured')

    def test_existing_json_preserves_other_values_and_original_bytes(self):
        path = self.home / '.zcode/cli/config.json'
        path.parent.mkdir(parents=True)
        raw = b'{ "unrelated": {"private":"DO-NOT-PRINT"}, "mcp": { "other": true, "servers": {"prior":{"command":"original"}}}}'
        path.write_bytes(raw)
        result = self.run_config('zcode', apply=True)
        after = json.loads(path.read_bytes())
        del after['mcp']['servers']['aihub']
        self.assertEqual(after, json.loads(raw))
        self.assertEqual((Path(result['backup']) / 'before.json').read_bytes(), raw)
        self.assertNotIn('DO-NOT-PRINT', json.dumps(result))

    def test_conflict_jsonc_duplicate_keys_and_wrong_shapes_are_refused(self):
        path = self.home / '.workbuddy/.mcp.json'
        path.parent.mkdir()
        for raw in (b'{// comment\n"mcpServers":{}}', b'{"mcpServers":[],"private":"secret"}',
                    b'{"mcpServers":{},"mcpServers":{}}', b'{"mcpServers":{"aihub":{"command":"other"}}}'):
            path.write_bytes(raw)
            with self.assertRaises(ConfigError):
                self.run_config('workbuddy', apply=True)
            self.assertEqual(path.read_bytes(), raw)
        self.assertFalse((path.parent / '.aihub-mcp-backups').exists())

    def test_custom_workbuddy_directory_and_fallback_precedence(self):
        custom = self.base / 'custom'
        custom.mkdir()
        fallback = self.home / '.codebuddy.json'
        fallback.write_text('{}')
        self.assertEqual(config_path('workbuddy', self.home, {'WORKBUDDY_CONFIG_DIR': str(custom)}), fallback)
        (custom / 'mcp.json').write_text('{}')
        self.assertEqual(config_path('workbuddy', self.home, {'WORKBUDDY_CONFIG_DIR': str(custom)}), custom / 'mcp.json')
        (custom / '.mcp.json').write_text('{}')
        result = configure('workbuddy', self.app, self.python, home=self.home,
                           env={'WORKBUDDY_CONFIG_DIR': str(custom)}, apply=True)
        self.assertEqual(result['path'], str(custom / '.mcp.json'))
        self.assertTrue(result['custom_data_directory'])
        self.assertEqual(fallback.read_text(), '{}')

    def test_native_codex_receives_pinned_home_and_only_owned_arguments(self):
        path = self.home / '.codex/config.toml'
        path.parent.mkdir()
        raw = b'model = "existing"\n# keep comment\n'
        path.write_bytes(raw)

        def fake_run(command, **kwargs):
            self.assertEqual(command[:4], [str(self.codex), 'mcp', 'add', 'aihub'])
            self.assertEqual(kwargs['env']['CODEX_HOME'], str(path.parent))
            self.assertTrue(kwargs['capture_output'])
            self.assertFalse(kwargs.get('shell', False))
            args = command[6:]
            # TOML basic strings/arrays have the same escaping as this JSON subset.
            extra = '\n[mcp_servers.aihub]\ncommand = %s\nargs = %s\n' % (
                json.dumps(command[5]), json.dumps(args))
            path.write_bytes(raw + extra.encode('utf-8'))
            return SimpleNamespace(returncode=0, stdout=b'private-output', stderr=b'private-error')

        result = self.run_config('codex', apply=True, runner=fake_run)
        self.assertEqual(result['status'], 'configured_pending_handshake')
        self.assertEqual((Path(result['backup']) / 'before.toml').read_bytes(), raw)
        self.assertTrue(path.read_bytes().startswith(raw))
        self.assertNotIn('private-', json.dumps(result))

    def test_native_codex_failure_preserves_backup_and_does_not_echo_output(self):
        path = self.home / '.codex/config.toml'
        path.parent.mkdir()
        path.write_bytes(b'model = "existing"\n')
        with self.assertRaises(ConfigError) as failure:
            self.run_config('codex', apply=True, runner=lambda *a, **kw: SimpleNamespace(
                returncode=1, stdout=b'SECRET', stderr=b'SECRET'))
        self.assertNotIn('SECRET', str(failure.exception))
        self.assertEqual(len(list((path.parent / '.aihub-mcp-backups').glob('*/before.toml'))), 1)

    def codex_cli_fixture(self, existing, native_base):
        path = self.home / '.codex/config.toml'
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(existing)

        def fake_run(command, **kwargs):
            extra = '\n[mcp_servers.aihub]\ncommand = %s\nargs = %s\n' % (
                json.dumps(command[5]), json.dumps(command[6:]))
            path.write_bytes(native_base + extra.encode('utf-8'))
            return SimpleNamespace(returncode=0)

        return path, fake_run

    def test_codex_omitted_existing_empty_args_restores_exact_original_prefix(self):
        original = ('# 保留原注释\r\nmodel = "existing"\r\n\r\n'
                    '[mcp_servers.node_repl]\r\ncommand = "node"\r\nargs = [] # empty by intent').encode('utf-8')
        native_base = b'model = "existing"\n[mcp_servers.node_repl]\ncommand = "node"\n'
        path, runner = self.codex_cli_fixture(original, native_base)
        result = self.run_config('codex', apply=True, runner=runner)
        final = path.read_bytes()
        self.assertTrue(final.startswith(original))
        self.assertIn(b'args = [] # empty by intent', final)
        self.assertEqual(final[len(original):].count(b'[mcp_servers.aihub]'), 1)
        folder = Path(result['backup'])
        self.assertEqual((folder / 'before.toml').read_bytes(), original)
        self.assertEqual((folder / 'after.toml').read_bytes(), final)
        self.assertTrue((folder / 'native-after.toml').read_bytes().startswith(native_base))

    def test_codex_unknown_native_semantic_change_not_restored(self):
        original = b'model = "existing"\n[mcp_servers.node_repl]\ncommand = "node"\nargs = []\n'
        path, runner = self.codex_cli_fixture(original, original.replace(b'"existing"', b'"unexpected"'))
        with self.assertRaises(ConfigError):
            self.run_config('codex', apply=True, runner=runner)
        self.assertIn(b'"unexpected"', path.read_bytes())
        self.assertFalse(list((path.parent / '.aihub-mcp-backups').glob('*/after.toml')))
        self.assertEqual(len(list((path.parent / '.aihub-mcp-backups').glob('*/native-after.toml'))), 1)

    def test_codex_nonempty_args_omission_rejected(self):
        original = b'[mcp_servers.node_repl]\ncommand = "node"\nargs = ["custom"]\n'
        path, runner = self.codex_cli_fixture(original, original.replace(b'args = ["custom"]\n', b''))
        with self.assertRaises(ConfigError):
            self.run_config('codex', apply=True, runner=runner)
        self.assertNotIn(b'custom', path.read_bytes())

    def test_codex_cas_rejects_change_after_native_verification(self):
        original = b'model = "existing"\n'
        path, runner = self.codex_cli_fixture(original, original)
        import tools.configure_harness_mcp as module
        write = module.private_write

        def race(target, content):
            write(target, content)
            if target.name.startswith('.aihub-') and target.suffix == '.tmp':
                path.write_bytes(b'# concurrent user edit\nmodel = "new-user-choice"\n')

        with patch.object(module, 'private_write', side_effect=race):
            with self.assertRaises(ConfigError):
                self.run_config('codex', apply=True, runner=runner)
        self.assertEqual(path.read_bytes(), b'# concurrent user edit\nmodel = "new-user-choice"\n')
        self.assertFalse(list(path.parent.glob('.aihub-*.tmp')))

    def test_codex_unappendable_inline_table_refused_before_cli(self):
        original = b'mcp_servers = { node_repl = { command = "node", args = [] } }\n'
        path, _ = self.codex_cli_fixture(original, original)
        with patch('tools.configure_harness_mcp.subprocess.run') as run:
            with self.assertRaises(ConfigError):
                self.run_config('codex', apply=True)
            run.assert_not_called()
        self.assertEqual(path.read_bytes(), original)

    def test_concurrent_change_detected_before_apply(self):
        path = self.home / '.workbuddy/.mcp.json'
        path.parent.mkdir()
        path.write_text('{}')
        import tools.configure_harness_mcp as module
        original = module.backup

        def mutate(*args):
            result = original(*args)
            path.write_text('{"other":true}')
            return result

        with patch.object(module, 'backup', side_effect=mutate):
            with self.assertRaises(ConfigError):
                self.run_config('workbuddy', apply=True)
        self.assertEqual(path.read_text(), '{"other":true}')

    def test_hardlinked_configuration_rejected(self):
        path = self.home / '.workbuddy/.mcp.json'
        path.parent.mkdir()
        source = self.base / 'original.json'
        source.write_text('{}')
        os.link(source, path)
        with self.assertRaises(ConfigError):
            self.run_config('workbuddy', apply=True)

    def test_relative_override_and_missing_bridge_rejected(self):
        with self.assertRaises(ConfigError):
            config_path('workbuddy', self.home, {'WORKBUDDY_CONFIG_DIR': 'relative'})
        (self.app / 'tools/aihub_mcp.py').unlink()
        with self.assertRaises(ConfigError):
            self.run_config('zcode', apply=True)


if __name__ == '__main__':
    unittest.main()
