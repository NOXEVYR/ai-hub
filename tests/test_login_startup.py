import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tools import configure_login_startup as startup


class FakeRegistry:
    def __init__(self): self.values = {}; self.writes = []
    def read(self, key, name): return self.values.get((key, name))
    def write(self, key, name, value): self.writes.append(('write', key, name)); self.values[key, name] = value
    def delete(self, key, name): self.writes.append(('delete', key, name)); del self.values[key, name]


class LoginStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / '曜核 App'
        self.root.mkdir()
        for name in ('AI Hub.exe', 'server.py', 'launcher.pyw'): (self.root / name).write_bytes(b'fixture')
        (self.root / 'frontend').mkdir()
        (self.root / 'AI Hub.exe').write_bytes(b'MZ' + startup.BACKGROUND_MARKER.encode('utf-16-le'))
        self.p, self.reg = startup.plan(str(self.root)), FakeRegistry()

    def test_default_off_and_dry_run_writes_nothing(self):
        self.assertEqual(startup.status(self.reg, self.p)['state'], 'disabled')
        self.assertFalse(self.p['default_enabled'])
        self.assertTrue(startup.configure(self.reg, self.p, True)['dry_run'])
        self.assertEqual(self.reg.writes, [])
        self.assertIn('--background', self.p['command'])
        self.assertIn('--root', self.p['command'])

    def test_enable_revoke_exact_current_user_item(self):
        self.assertEqual(startup.configure(self.reg, self.p, True, True)['state'], 'enabled')
        self.reg.values[startup.RUN_KEY, 'Unrelated'] = 'keep'
        startup.configure(self.reg, self.p, True, True)
        self.assertEqual(startup.configure(self.reg, self.p, False, True)['state'], 'disabled')
        self.assertEqual(self.reg.values, {(startup.RUN_KEY, 'Unrelated'): 'keep'})
        self.assertEqual(startup.configure(self.reg, self.p, False, True)['state'], 'disabled')

    def test_conflicts_not_adopted_overwritten_or_deleted(self):
        for value in ('other executable', self.p['command'], ('unexpected_registry_type', 2)):
            self.reg = FakeRegistry()
            self.reg.values[startup.RUN_KEY, self.p['name']] = value
            for enable in (True, False):
                with self.subTest(value=value, enable=enable), self.assertRaises(ValueError):
                    startup.configure(self.reg, self.p, enable, True)
            self.assertEqual(self.reg.writes, [])

    def test_interrupted_enable_and_path_movement_recovery(self):
        self.reg.values[startup.OWNER_KEY, self.p['name']] = self.p['owner']
        self.assertEqual(startup.status(self.reg, self.p)['state'], 'incomplete')
        self.assertEqual(startup.configure(self.reg, self.p, True, True)['state'], 'enabled')
        new = startup.plan(str(self.root.parent / 'Moved'))
        self.assertNotEqual(new['name'], self.p['name'])
        self.assertEqual(startup.status(self.reg, new)['state'], 'disabled')
        (self.root / 'AI Hub.exe').unlink()
        # An old exact entry may be revoked even when that installation was moved.
        self.assertEqual(startup.configure(self.reg, self.p, False, True)['state'], 'disabled')
        with self.assertRaises(FileNotFoundError): startup.configure(self.reg, self.p, True, True)

    def test_changed_owned_command_is_not_removed(self):
        startup.configure(self.reg, self.p, True, True)
        self.reg.values[startup.RUN_KEY, self.p['name']] = 'changed'
        before = dict(self.reg.values)
        with self.assertRaises(ValueError): startup.configure(self.reg, self.p, False, True)
        self.assertEqual(before, self.reg.values)

    def test_symlink_install_rejected(self):
        link = self.root.parent / 'Alias'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): startup.configure(self.reg, startup.plan(str(link)), True, True)
        self.assertEqual(self.reg.writes, [])

    def test_windows_entry_contract_source_not_runtime_verification(self):
        text = (Path(__file__).resolve().parents[1] / 'desktop/Program.cs').read_text()
        self.assertIn('if (!BackgroundStartup) ActivateEvent.Set()', text)
        self.assertIn('base.SetVisibleCore(false)', text)
        self.assertIn('backgroundHidden = false;', text)
        self.assertIn('UpdateStartupPending()', text)

    def test_legacy_exe_cannot_be_registered(self):
        (self.root / 'AI Hub.exe').write_bytes(b'MZ legacy binary without background argument')
        with self.assertRaisesRegex(ValueError, 'EXE'):
            startup.configure(self.reg, self.p, True, True)
        self.assertEqual(self.reg.writes, [])

    def test_relative_paths_rejected(self):
        with self.assertRaises(ValueError): startup.plan('relative/app')
