"""User-run MCP configuration helper. Dry run unless --apply; never prints configuration."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import uuid

MAX_CONFIG = 2 * 1024 * 1024
SUPPORTED = ('codex', 'zcode', 'workbuddy')


class ConfigError(Exception):
    pass


def safe_path(path):
    path = Path(path)
    if not path.is_absolute():
        raise ConfigError('Configuration and executable paths must be absolute')
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ConfigError('Refusing a symlink or reparse point in the configuration path')
    return path


def snapshot(path):
    safe_path(path)
    if not path.exists():
        return None
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_CONFIG:
        raise ConfigError('Configuration must be a bounded ordinary non-hardlinked file')
    return path.read_bytes()


def checked_object(value):
    if not isinstance(value, dict):
        raise ConfigError('Expected an object; configuration was not changed')
    return value


def no_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError('Duplicate JSON keys; refusing to rewrite configuration')
        result[key] = value
    return result


def load_config(raw, tool):
    if raw is None:
        return {}
    try:
        if tool == 'codex':
            try:
                import tomllib
            except ImportError as exc:
                raise ConfigError('Codex configuration verification needs Python 3.11+') from exc
            return checked_object(tomllib.loads(raw.decode('utf-8-sig')))
        return checked_object(json.loads(raw.decode('utf-8-sig'), object_pairs_hook=no_duplicate_pairs))
    except (ValueError, UnicodeError):
        raise ConfigError('Configuration is not strict JSON/TOML (JSONC is not rewritten)') from None


def config_path(tool, home, env):
    if tool == 'codex':
        return safe_path(Path(env.get('CODEX_HOME') or home / '.codex') / 'config.toml')
    if tool == 'zcode':
        user_home = Path(env.get('HOME') or env.get('USERPROFILE') or home)
        return safe_path(user_home / '.zcode' / 'cli' / 'config.json')
    root = Path(env.get('WORKBUDDY_CONFIG_DIR') or env.get('CODEBUDDY_CONFIG_DIR') or home / '.workbuddy')
    candidates = [root / '.mcp.json', root / 'mcp.json', home / '.codebuddy.json']
    for path in candidates:
        safe_path(path)
        if path.exists():
            return path
    return safe_path(candidates[0])


def server_map(config, tool, create=False):
    if tool == 'zcode':
        parent = checked_object(config.setdefault('mcp', {}) if create else config.get('mcp', {}))
        key = 'servers'
    else:
        parent = config
        key = 'mcp_servers' if tool == 'codex' else 'mcpServers'
    return checked_object(parent.setdefault(key, {}) if create else parent.get(key, {}))


def resolve_codex(env):
    exe = shutil.which('codex.exe' if os.name == 'nt' else 'codex', path=env.get('PATH', ''))
    if exe:
        return Path(exe)
    if os.name == 'nt' and env.get('APPDATA'):
        package = Path(env['APPDATA']) / 'npm/node_modules/@openai/codex/node_modules/@openai'
        if package.is_dir():
            candidates = list(package.glob('codex-win32-*/vendor/*/bin/codex.exe'))
            if len(candidates) == 1:
                return candidates[0]
    raise ConfigError('Native Codex executable not found; pass --codex with an absolute executable path')


def private_write(path, content):
    safe_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def backup(path, raw):
    parent = path.parent / '.aihub-mcp-backups'
    safe_path(parent)
    parent.mkdir(mode=0o700, exist_ok=True)
    folder = parent / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:10])
    folder.mkdir(mode=0o700)
    if raw is None:
        private_write(folder / 'before-absent.txt', b'Configuration did not exist before this operation.\n')
    else:
        private_write(folder / ('before' + path.suffix), raw)
    return folder


def codex_appended_bytes(raw, entry, desired):
    """Preserve every original byte; append only our independently validated table."""
    original = raw or b''
    newline = '\r\n' if b'\r\n' in original else '\n'
    block = newline.join(('', '', '[mcp_servers.aihub]',
                          'command = ' + json.dumps(entry['command'], ensure_ascii=False),
                          'args = ' + json.dumps(entry['args'], ensure_ascii=False), ''))
    try:
        candidate = original + block.encode('utf-8')
    except UnicodeError:
        raise ConfigError('Owned configuration contains invalid Unicode') from None
    if load_config(candidate, 'codex') != desired:
        raise ConfigError('Cannot append an independent aihub TOML table without changing existing structure')
    return candidate


def codex_native_matches(original, actual, desired):
    """Allow only the native CLI omission of pre-existing empty args arrays."""
    normalized = deepcopy(actual)
    existing = server_map(original, 'codex')
    resulting = server_map(normalized, 'codex')
    for name, value in existing.items():
        target = resulting.get(name)
        if (name != 'aihub' and isinstance(value, dict) and value.get('args') == []
                and isinstance(target, dict) and 'args' not in target):
            target['args'] = []
    return normalized == desired


def replace_if_unchanged(path, expected, content):
    """Recheck native output immediately before replacing our private temporary file."""
    temporary = path.with_name('.aihub-' + uuid.uuid4().hex + '.tmp')
    private_write(temporary, content)
    try:
        if snapshot(path) != expected:
            raise ConfigError('Configuration changed after native inspection; refusing to replace')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def configure(tool, app_dir, python, port=8765, apply=False, home=None, env=None, codex=None, runner=None):
    if tool not in SUPPORTED or not 1024 <= port <= 65535:
        raise ConfigError('Unsupported tool or port')
    env = dict(os.environ if env is None else env)
    home = Path.home() if home is None else Path(home)
    app_dir, python = safe_path(Path(app_dir)), safe_path(Path(python))
    bridge = app_dir / 'tools' / 'aihub_mcp.py'
    if not bridge.is_file() or not python.is_file():
        raise ConfigError('AI Hub MCP bridge and Python executable must already exist')
    path = config_path(tool, home, env)
    raw = snapshot(path)
    original = load_config(raw, tool)
    args = ['-B', str(bridge), '--port', str(port), '--client-id', tool + '-main', '--tool', tool]
    entry = {'command': str(python), 'args': args}
    if tool != 'codex':
        entry = {'type': 'stdio', **entry}
    servers = server_map(original, tool)
    if 'aihub' in servers:
        if servers['aihub'] == entry:
            return {'tool': tool, 'path': str(path), 'status': 'already_configured', 'changed': False}
        raise ConfigError('An aihub entry already exists; it will not be overwritten')
    desired = deepcopy(original)
    server_map(desired, tool, create=True)['aihub'] = entry
    cli = None
    if tool == 'codex':
        cli = safe_path(Path(codex)) if codex else resolve_codex(env)
        if not cli.is_file() or (os.name == 'nt' and cli.suffix.lower() != '.exe'):
            raise ConfigError('Codex must be a native executable; shell wrappers are not used')
        # Preflight before calling the CLI: inline-table configurations cannot always be extended.
        final_codex_bytes = codex_appended_bytes(raw, entry, desired)
    plan = {'tool': tool, 'path': str(path), 'status': 'would_add' if raw is not None else 'would_create',
            'changed': False, 'transport': 'stdio', 'client_id': tool + '-main',
            'custom_data_directory': bool(env.get('WORKBUDDY_CONFIG_DIR') or env.get('CODEBUDDY_CONFIG_DIR')) if tool == 'workbuddy' else bool(env.get('CODEX_HOME')) if tool == 'codex' else False}
    if not apply:
        return plan
    safe_path(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot(path) != raw:
        raise ConfigError('Configuration changed since inspection; refusing to write')
    folder = backup(path, raw)
    if snapshot(path) != raw:
        raise ConfigError('Configuration changed while preparing backup; refusing to write')
    if tool == 'codex':
        # The native command receives only owned, nonsecret arguments. Capture all output.
        command = [str(cli), 'mcp', 'add', 'aihub', '--', str(python), *args]
        child_env = {**env, 'CODEX_HOME': str(path.parent)}
        native_failed = False
        try:
            result = (runner or subprocess.run)(command, env=child_env, capture_output=True, timeout=30, check=False)
            native_failed = result.returncode != 0
        except (OSError, subprocess.TimeoutExpired):
            native_failed = True
        after = snapshot(path)
        if after is not None:
            private_write(folder / ('native-after' + path.suffix), after)
        else:
            private_write(folder / 'after-absent.txt', b'Configuration absent after native command.\n')
        if native_failed:
            raise ConfigError('Native Codex command failed or timed out; configuration may have changed. Private before/after backup preserved')
        if after is None or not codex_native_matches(original, load_config(after, tool), desired):
            raise ConfigError('Codex post-write verification differs from the planned change; inspect private backup before continuing')
        # Native semantic output is now approved. Restore original formatting/comments and
        # empty args using only original bytes + our owned table, never an unknown CLI diff.
        replace_if_unchanged(path, after, final_codex_bytes)
        final = snapshot(path)
        if final != final_codex_bytes or load_config(final, tool) != desired:
            raise ConfigError('Codex final verification failed; private before/native-after backups preserved')
        private_write(folder / ('after' + path.suffix), final)
    else:
        try:
            content = (json.dumps(desired, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        except UnicodeError:
            raise ConfigError('Configuration contains invalid Unicode; original preserved') from None
        if raw is None:
            private_write(path, content)  # exclusive create, including at the final race boundary
        else:
            temporary = path.with_name('.aihub-' + uuid.uuid4().hex + '.tmp')
            private_write(temporary, content)
            try:
                if snapshot(path) != raw:
                    raise ConfigError('Configuration changed before replacement; refusing to write')
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()  # only our uniquely created temporary file
        after = snapshot(path)
        private_write(folder / ('after' + path.suffix), after)
        if load_config(after, tool) != desired:
            raise ConfigError('Post-write verification failed; private backup preserved')
    return {**plan, 'status': 'configured_pending_handshake', 'changed': True, 'backup': str(folder)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-dir', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--tool', choices=(*SUPPORTED, 'all'), default='all')
    parser.add_argument('--codex', type=Path, help='Absolute native Codex executable; no shell wrapper')
    parser.add_argument('--apply', action='store_true', help='User-authorized configuration change; default is read-only')
    args = parser.parse_args()
    results = []
    for tool in SUPPORTED if args.tool == 'all' else (args.tool,):
        try:
            results.append(configure(tool, args.app_dir, args.python, args.port, args.apply, codex=args.codex))
        except (ConfigError, OSError) as exc:
            # OS exceptions may contain sensitive filenames/arguments; report a safe class only.
            results.append({'tool': tool, 'status': 'refused',
                            'reason': str(exc) if isinstance(exc, ConfigError) else 'Filesystem operation failed; no configuration content logged'})
    print(json.dumps({'apply': args.apply, 'items': results}, ensure_ascii=False, indent=2))
    return 1 if any(item['status'] == 'refused' for item in results) else 0


if __name__ == '__main__':
    raise SystemExit(main())
