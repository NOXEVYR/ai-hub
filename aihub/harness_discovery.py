"""Bounded, read-only harness discovery. No commands, native config or registration.

Windows evidence uses process names/image paths and allowlisted registry values.
A running executable is not evidence of an MCP connection or successful AI work.
"""
import ctypes
import os
from pathlib import Path
import re
import stat
import time

MAX_PROCESSES = 2048
MAX_REGISTRY_ENTRIES = 512
MAX_SECONDS = 6.0
BUILTIN_IDS = {'codex', 'zcode', 'dsh', 'workbuddy'}


def _tool(identifier, name, commands, executables=None, folders=None, mode='mcp_stdio'):
    return {'id': identifier, 'name': name, 'commands': tuple(commands),
            'executables': tuple(executables or (command + '.exe' for command in commands)),
            'folders': tuple(folders or (name.replace(' ', ''), identifier)), 'connection_mode': mode}


CATALOG = (
    _tool('codex', 'Codex', ('codex',)),
    _tool('zcode', 'ZCode', ('zcode',)),
    _tool('dsh', 'DeepSeek Harness', ('dsh',), folders=('DeepSeek_Harness_Launcher', 'DSH')),
    _tool('workbuddy', 'WorkBuddy', ('workbuddy',)),
    _tool('qoder', 'Qoder', ('qoder',), mode='manual'),
    _tool('qoder-cli', 'Qoder CLI', ('qodercli', 'qoder-cli'), folders=('Qoder', 'QoderCLI')),
    _tool('claude', 'Claude', ('claude',), folders=('Claude', 'ClaudeCode')),
    _tool('gemini', 'Gemini CLI', ('gemini',), folders=('Gemini', 'GeminiCLI')),
    _tool('opencode', 'OpenCode', ('opencode',)),
    _tool('cursor', 'Cursor', ('cursor',), mode='manual'),
    _tool('trae', 'Trae', ('trae',), executables=('Trae.exe', 'Trae CN.exe'), folders=('Trae', 'Trae CN'), mode='manual'),
    _tool('aider', 'Aider', ('aider',)),
)
_BY_ID = {item['id']: item for item in CATALOG}
_BY_EXE = {name.casefold(): item for item in CATALOG for name in item['executables']}
_PATH_EXTENSIONS = ('.exe', '.cmd', '.ps1', '.bat', '') if os.name == 'nt' else ('',)
_SAFE_ENV = {'programfiles', 'programfiles(x86)', 'localappdata', 'appdata', 'userprofile'}


def _summary(status='complete'):
    return {'status': status, 'checked': 0, 'limited': False, 'truncated': False}


def _local_path(value):
    if not isinstance(value, (str, os.PathLike)):
        return None
    value = os.fspath(value)
    if not value or len(value) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    if value.startswith(('\\\\', '//')) or not os.path.isabs(value):
        return None
    if os.name == 'nt' and (':' in value[2:] or any(char in value for char in '*?<>|"')):
        return None
    return os.path.abspath(value)


def _local_drive(path):
    if os.name != 'nt':
        return True
    # A mapped network drive must not turn metadata discovery into network scanning.
    try:
        from ctypes import wintypes
        get_type = ctypes.WinDLL('kernel32', use_last_error=True).GetDriveTypeW
        get_type.argtypes = (wintypes.LPCWSTR,)
        get_type.restype = wintypes.UINT
        return get_type(Path(path).anchor) in (2, 3, 6)  # removable, fixed, RAM
    except (OSError, AttributeError):
        return False


def _canonical_file(value, drives, skipped=None):
    raw = _local_path(value)
    if raw is None:
        return None
    anchor = Path(raw).anchor.casefold()
    if anchor not in drives:
        drives[anchor] = _local_drive(raw)
    if not drives[anchor]:
        return None
    try:
        path = Path(raw)
        # Walk from the drive root before touching each child. In particular, do
        # not call realpath on a local junction that may redirect to a UNC share.
        for current in (*reversed(path.parents), path):
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                if skipped is not None:
                    skipped['reparse_path'] = skipped.get('reparse_path', 0) + 1
                return None
            if current != path and not stat.S_ISDIR(info.st_mode):
                return None
        return raw if stat.S_ISREG(info.st_mode) else None
    except (OSError, ValueError):
        return None


def _candidate(tool, path, source, pid=None):
    return {'tool': tool['id'], 'path': os.fspath(path), 'source': source, 'pid': pid}


def _process_candidates(deadline):
    summary = _summary('unsupported' if os.name != 'nt' else 'complete')
    if os.name != 'nt':
        return [], summary
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]

    items, snapshot = [], None
    try:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        for name in ('Process32FirstW', 'Process32NextW'):
            function = getattr(kernel, name)
            function.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
            function.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS only
        if snapshot in (None, ctypes.c_void_p(-1).value):
            snapshot = None
            summary.update(status='unavailable', limited=True)
            return [], summary
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        if not more and ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            summary.update(status='unavailable', limited=True)
        while more:
            if summary['checked'] >= MAX_PROCESSES or time.monotonic() >= deadline:
                summary.update(status='partial', truncated=True)
                break
            summary['checked'] += 1
            tool = _BY_EXE.get(entry.szExeFile.casefold())
            if tool is not None:
                # Never request VM_READ, module snapshots, command lines or injection.
                handle = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if handle:
                    try:
                        buffer = ctypes.create_unicode_buffer(32768)
                        length = wintypes.DWORD(len(buffer))
                        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                            path = buffer.value
                            if Path(path).name.casefold() in {name.casefold() for name in tool['executables']}:
                                items.append(_candidate(tool, path, 'running_process', entry.th32ProcessID))
                        else:
                            summary['limited'] = True
                    finally:
                        kernel.CloseHandle(handle)
                else:
                    summary['limited'] = True
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if not more and ctypes.get_last_error() not in (0, 18):
            summary.update(status='partial', limited=True)
    except (OSError, AttributeError, ValueError):
        summary.update(status='unavailable' if not items else 'partial', limited=True)
    finally:
        if snapshot is not None:
            kernel.CloseHandle(snapshot)
    return items, summary


def _expand_registry_path(value):
    if not isinstance(value, str) or len(value) > 8192:
        return None
    environment = {key.casefold(): item for key, item in os.environ.items() if key.casefold() in _SAFE_ENV}
    value = re.sub(r'%([^%]+)%', lambda match: environment.get(match[1].casefold(), match[0]), value)
    return value if '%' not in value else None


def _registry_executable(value):
    value = _expand_registry_path(value)
    if value is None or any(ord(char) < 32 for char in value):
        return None
    # Accept only a path and optional icon index; never parse shell arguments.
    match = re.fullmatch(r'\s*(?:"([^"\r\n]+\.exe)"|([^"\r\n]+\.exe))(?:\s*,\s*-?\d+)?\s*', value, flags=re.I)
    return _local_path(match[1] or match[2]) if match else None


def _display_tool(value):
    if not isinstance(value, str) or len(value) > 256:
        return None
    folded = value.casefold().strip()
    # Specific CLI names precede their desktop family; no arbitrary registry names leak.
    ordered = sorted(CATALOG, key=lambda item: len(item['name']), reverse=True)
    for tool in ordered:
        for prefix in {tool['name'].casefold(), tool['id'], tool['name'].replace(' ', '').casefold()}:
            if folded == prefix or folded.startswith(prefix + ' ') or folded.startswith(prefix + ' ('):
                return tool
    return None


def _registry_candidates(deadline):
    sources = {'app_paths': _summary(), 'uninstall_registry': _summary()}
    if os.name != 'nt':
        for value in sources.values():
            value['status'] = 'unsupported'
        return [], sources
    import winreg
    items = []
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    hives = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)

    def value(key, name):
        try:
            result, kind = winreg.QueryValueEx(key, name)
            return result if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(result, str) and len(result) <= 8192 else None
        except OSError:
            return None

    for hive in hives:
        for view in views:
            for tool in CATALOG:
                for executable in tool['executables']:
                    state = sources['app_paths']
                    if time.monotonic() >= deadline:
                        state.update(status='partial', truncated=True)
                        break
                    state['checked'] += 1
                    try:
                        with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths' + '\\' + executable, 0, winreg.KEY_READ | view) as key:
                            path = _registry_executable(value(key, ''))
                            if path and Path(path).name.casefold() in {name.casefold() for name in tool['executables']}:
                                items.append(_candidate(tool, path, 'app_paths'))
                    except FileNotFoundError:
                        pass
                    except OSError:
                        state['limited'] = True
            state = sources['uninstall_registry']
            if time.monotonic() >= deadline or state['checked'] >= MAX_REGISTRY_ENTRIES:
                state.update(status='partial', truncated=True)
                continue
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', 0, winreg.KEY_READ | view) as base:
                    count = winreg.QueryInfoKey(base)[0]
                    for index in range(count):
                        if time.monotonic() >= deadline or state['checked'] >= MAX_REGISTRY_ENTRIES:
                            state.update(status='partial', truncated=True)
                            break
                        state['checked'] += 1
                        try:
                            child_name = winreg.EnumKey(base, index)
                            with winreg.OpenKey(base, child_name, 0, winreg.KEY_READ | view) as child:
                                tool = _display_tool(value(child, 'DisplayName'))
                                if tool is None:
                                    continue
                                icon = _registry_executable(value(child, 'DisplayIcon'))
                                if icon and Path(icon).name.casefold() in {name.casefold() for name in tool['executables']}:
                                    items.append(_candidate(tool, icon, 'uninstall_registry'))
                                location = _local_path(_expand_registry_path(value(child, 'InstallLocation')))
                                if location:
                                    for executable in tool['executables']:
                                        items.append(_candidate(tool, Path(location) / executable, 'uninstall_registry'))
                        except OSError:
                            state['limited'] = True
            except FileNotFoundError:
                pass
            except OSError:
                state['limited'] = True
    return items, sources


def _file_candidates(cfg, max_path_dirs):
    home = Path.home()
    path_parts = os.environ.get('PATH', '').split(os.pathsep)
    path_directories = []
    for value in path_parts[:max_path_dirs]:
        path = _local_path(value)
        if path and path not in path_directories:
            path_directories.append(path)
    local = _local_path(os.environ.get('LOCALAPPDATA')) or str(home / 'AppData/Local')
    roaming = _local_path(os.environ.get('APPDATA')) or str(home / 'AppData/Roaming')
    user_bins = [Path(roaming) / 'npm', home / '.local/bin', home / 'bin']
    program_roots = [Path(local) / 'Programs']
    for name in ('ProgramFiles', 'ProgramFiles(x86)'):
        path = _local_path(os.environ.get(name))
        if path:
            program_roots.append(Path(path))
    workspace = _local_path(cfg.get('ai_root'))
    if workspace:
        program_roots.extend([Path(workspace) / '10_Apps', Path(workspace) / 'Apps'])
    # Interleave candidates by directory/tool so Qoder and later names get the same budget.
    candidates = []
    for directory in path_directories:
        for extension in _PATH_EXTENSIONS:
            for tool in CATALOG:
                candidates.extend(_candidate(tool, Path(directory) / (command + extension), 'path') for command in tool['commands'])
    common = []
    for directory in user_bins:
        for extension in _PATH_EXTENSIONS:
            for tool in CATALOG:
                common.extend(_candidate(tool, directory / (command + extension), 'common_location') for command in tool['commands'])
    for directory in program_roots:
        for tool in CATALOG:
            for folder in tool['folders']:
                for executable in tool['executables']:
                    common.append(_candidate(tool, directory / folder / executable, 'common_location'))
                for extension in _PATH_EXTENSIONS:
                    common.extend(_candidate(tool, directory / folder / 'bin' / (command + extension), 'common_location') for command in tool['commands'])
    return candidates, common, len(path_parts) > max_path_dirs


def discover(cfg, *, max_checks=1024, max_path_dirs=64):
    """Return observations only; the registry wrapper supplies registered state."""
    if type(max_checks) is not int or not 1 <= max_checks <= 4096 or type(max_path_dirs) is not int or not 1 <= max_path_dirs <= 128:
        raise ValueError('发现预算超出允许范围。')
    start = time.monotonic()
    deadline = start + MAX_SECONDS
    running, process_summary = _process_candidates(min(deadline, start + 2.0))
    registered_paths, registry_summaries = _registry_candidates(min(deadline, start + 3.5))
    path_candidates, common_candidates, path_truncated = _file_candidates(cfg, max_path_dirs)
    sources = {'running_process': process_summary, **registry_summaries,
               'path': _summary(), 'common_location': _summary()}
    sources['path']['truncated'] = path_truncated
    if path_truncated:
        sources['path']['status'] = 'partial'
    # Running/installation evidence is examined before optional filename guesses.
    pending = list(running) + list(registered_paths)
    for index in range(max(len(path_candidates), len(common_candidates))):
        if index < len(path_candidates):
            pending.append(path_candidates[index])
        if index < len(common_candidates):
            pending.append(common_candidates[index])
    drives, checked_paths, grouped, skipped = {}, {}, {}, {}
    checks = 0
    for offset, candidate in enumerate(pending):
        source = candidate['source']
        if time.monotonic() >= deadline:
            for rest in pending[offset:]:
                sources[rest['source']].update(status='partial', truncated=True)
            break
        path = _local_path(candidate['path'])
        if path is None:
            continue
        key = os.path.normcase(path)
        if key not in checked_paths:
            if checks >= max_checks:
                sources[source].update(status='partial', truncated=True)
                continue
            checks += 1
            if source in ('path', 'common_location'):
                sources[source]['checked'] += 1
            before_skipped = sum(skipped.values())
            checked_paths[key] = _canonical_file(path, drives, skipped)
            if sum(skipped.values()) != before_skipped:
                sources[source]['limited'] = True
        canonical = checked_paths[key]
        if canonical is None:
            continue
        canonical_key = os.path.normcase(canonical)
        item = grouped.get(canonical_key)
        if item is None:
            tool = _BY_ID[candidate['tool']]
            item = {'id': tool['id'], 'suggested_id': tool['id'], 'name': tool['name'],
                    'executable': canonical, 'builtin': tool['id'] in BUILTIN_IDS,
                    'registered': False, 'evidence': 'file_metadata_only', 'scan_scope': source,
                    'connection_mode': tool['connection_mode'], 'verified': False,
                    'evidence_sources': [], 'running': False, 'process_count': 0, '_pids': set()}
            grouped[canonical_key] = item
        if source not in item['evidence_sources']:
            item['evidence_sources'].append(source)
        if source == 'running_process' and candidate.get('pid'):
            item['_pids'].add(candidate['pid'])
            item['running'] = True
            item['process_count'] = len(item['_pids'])
    items = sorted(grouped.values(), key=lambda item: (not item['running'], item['name'].casefold(), item['executable'].casefold()))
    for item in items:
        item.pop('_pids')
    truncated = any(value['truncated'] for value in sources.values())
    limited = any(value['limited'] for value in sources.values())
    limitations = ['发现只读取入口元数据，不启动软件、不读取原生配置、不自动登记。',
                   '运行进程证据不代表 MCP 在线、任务可领取或模型调用成功。',
                   '仅匹配已知名称；未发现的工具仍可手动登记。']
    if limited:
        limitations.append('部分系统元数据访问受限，结果可能遗漏对应入口。')
    if skipped.get('reparse_path'):
        limitations.append('安全边界跳过了联接或符号链接路径，不跟随到网络或其他位置。')
    if truncated:
        limitations.append('发现达到时间或数量边界，仅显示本次已检查的结果。')
    return {'items': items, 'checks': checks, 'truncated': truncated, 'limited': limited,
            'sources': sources, 'limitations': limitations, 'skipped': skipped,
            'scan_scope': {'path_directories_limit': max_path_dirs, 'candidate_limit': max_checks,
                           'process_limit': MAX_PROCESSES, 'registry_entry_limit': MAX_REGISTRY_ENTRIES,
                           'soft_time_budget_seconds': MAX_SECONDS, 'native_call_hard_timeout': False, 'recursive': False,
                           'process_command_lines': False, 'reads_configuration_contents': False,
                           'registers_automatically': False}}
