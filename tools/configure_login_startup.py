"""Explicit current-user Windows login startup. Dry-run unless --apply; no service/MCP changes."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
OWNER_KEY = r'Software\AIHub\LoginStartup'
BACKGROUND_MARKER = 'AIHub.LoginBackground.v1'


def plan(install_root):
    root = Path(install_root)
    if str(root).startswith('\\\\') or not root.is_absolute() or any(c in str(root) for c in '\r\n\x00"'):
        raise ValueError('安装目录格式无效。')
    # Do not resolve aliases silently: enabling checks every existing ancestor.
    root = Path(os.path.normpath(root))
    command = subprocess.list2cmdline([str(root / 'AI Hub.exe'), '--root', str(root), '--background'])
    identity = hashlib.sha256(str(root).casefold().encode('utf-8')).hexdigest()[:24]
    name = 'AIHub.' + identity
    owner = json.dumps({'schema': 'aihub-login-startup/1', 'root': str(root), 'command': command}, sort_keys=True)
    return {'scope': 'current_user', 'default_enabled': False, 'root': str(root), 'name': name,
            'command': command, 'owner': owner, 'registry_key': 'HKCU\\' + RUN_KEY,
            'owner_key': 'HKCU\\' + OWNER_KEY}


def validate_install(p):
    if len(p['command']) > 260:
        raise ValueError('登录启动命令过长，请使用较短的完整安装路径。')
    root = Path(p['root'])
    for path in [root, *root.parents, root / 'AI Hub.exe', root / 'server.py', root / 'launcher.pyw', root / 'frontend']:
        info = path.lstat()
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('登录启动安装路径不能使用符号链接或重解析点。')
    if not all((root / name).is_file() for name in ('AI Hub.exe', 'server.py', 'launcher.pyw')) or not (root / 'frontend').is_dir():
        raise ValueError('安装目录缺少曜核程序文件。')
    exe = root / 'AI Hub.exe'
    if exe.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('桌面程序超过兼容性检查上限。')
    binary = exe.read_bytes()
    if not binary.startswith(b'MZ') or BACKGROUND_MARKER.encode('utf-16-le') not in binary:
        raise ValueError('此桌面 EXE 尚未声明后台登录入口；请先构建并验收新候选，不能对旧版启用。')


class Registry:
    def __init__(self):
        if os.name != 'nt':
            raise OSError('仅支持 Windows 当前用户登录启动；本平台未修改任何启动设置。')
        import winreg
        self.api = winreg

    def read(self, key, name):
        w = self.api
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, key, 0, w.KEY_READ) as handle:
                value, kind = w.QueryValueEx(handle, name)
                return value if kind == w.REG_SZ else ('unexpected_registry_type', kind)
        except FileNotFoundError:
            return None

    def write(self, key, name, value):
        w = self.api
        with w.CreateKeyEx(w.HKEY_CURRENT_USER, key, 0, w.KEY_SET_VALUE) as handle:
            w.SetValueEx(handle, name, 0, w.REG_SZ, value)

    def delete(self, key, name):
        w = self.api
        with w.OpenKey(w.HKEY_CURRENT_USER, key, 0, w.KEY_SET_VALUE) as handle:
            w.DeleteValue(handle, name)


def status(registry, p):
    run, owner = registry.read(RUN_KEY, p['name']), registry.read(OWNER_KEY, p['name'])
    conflict = run not in (None, p['command']) or owner not in (None, p['owner']) or (run is not None and owner is None)
    return {'state': 'conflict' if conflict else ('enabled' if run is not None else ('incomplete' if owner is not None else 'disabled')),
            'owned': owner == p['owner'], 'scope': 'current_user', 'command': p['command'],
            'registry_key': p['registry_key'], 'value_name': p['name'], 'default_enabled': False}


def configure(registry, p, enabled, apply=False, validator=validate_install):
    before = status(registry, p)
    if before['state'] == 'conflict':
        raise ValueError('同名启动项或所有权记录不匹配；未覆盖、未删除，请先人工核对。')
    if enabled:
        validator(p)
    if not apply:
        return dict(before, dry_run=True, requested_enabled=enabled)
    # Caller serializes our tool with the installation-specific named mutex.
    if enabled:
        if registry.read(OWNER_KEY, p['name']) is None:
            registry.write(OWNER_KEY, p['name'], p['owner'])
        if registry.read(OWNER_KEY, p['name']) != p['owner'] or registry.read(RUN_KEY, p['name']) not in (None, p['command']):
            raise ValueError('写入前启动项已变化；保留现场，请重新检查。')
        registry.write(RUN_KEY, p['name'], p['command'])
    elif before['owned']:
        if registry.read(RUN_KEY, p['name']) == p['command']:
            registry.delete(RUN_KEY, p['name'])
        if registry.read(RUN_KEY, p['name']) is not None:
            raise ValueError('撤销期间启动项变化，未删除所有权记录。')
        if registry.read(OWNER_KEY, p['name']) == p['owner']:
            registry.delete(OWNER_KEY, p['name'])
    result = status(registry, p)
    if result['state'] != ('enabled' if enabled else 'disabled'):
        raise ValueError('启动设置结果尚未确认，请重新执行 --status。')
    return dict(result, dry_run=False)


@contextlib.contextmanager
def installation_lock(name):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateMutexW(None, False, 'Local\\' + name + '.LoginStartup')
    if not handle:
        raise OSError('无法锁定登录启动设置。')
    acquired = False
    try:
        acquired = kernel.WaitForSingleObject(handle, 5000) in (0, 0x80)
        if not acquired:
            raise OSError('另一项登录启动设置正在进行，请稍后重试。')
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-root', required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--status', action='store_true')
    group.add_argument('--enable', action='store_true')
    group.add_argument('--disable', action='store_true')
    parser.add_argument('--apply', action='store_true', help='Explicitly change only this installation current-user entry')
    args = parser.parse_args(argv)
    if args.status and args.apply:
        parser.error('--status cannot be combined with --apply')
    try:
        p = plan(args.install_root)
        registry = Registry()
        with installation_lock(p['name']):
            result = status(registry, p) if args.status else configure(registry, p, args.enable, args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({'state': 'not_confirmed', 'error': str(error),
                          'next': '核对 --status；不自动重试或覆盖冲突。'}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
