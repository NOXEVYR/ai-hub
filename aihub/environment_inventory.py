"""Bounded environment *name* inventory; never obtain or inspect variable values."""
import ctypes
from ctypes import wintypes
import os
import re

MAX_SOURCE_NAMES = 4096
MAX_INVENTORY_ITEMS = 1024
MAX_NAME_LENGTH = 128
_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,127}\Z')
_PERSISTENT = ('windows_user', 'windows_system')
_LOCATIONS = {
    'process': 'os.environ names (current process snapshot)',
    'windows_user': r'HKEY_CURRENT_USER\Environment',
    'windows_system': r'HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Control\Session Manager\Environment',
}
_STATUSES = {'scanned', 'unavailable', 'not_supported', 'truncated_budget'}


def snapshot_names(names, source_id='process', status='scanned'):
    """Consume only mapping keys/an iterable of names, with a fixed inspection budget."""
    result, seen, rejected, examined = [], set(), 0, 0
    if status in {'unavailable', 'not_supported'}:
        names = ()
    for name in names:
        if examined >= MAX_SOURCE_NAMES:
            status = 'truncated_budget'
            break
        examined += 1
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            rejected += 1
            continue
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            result.append(name)
    return {'id': source_id, 'status': status, 'names': result,
            'names_examined': examined, 'names_rejected': rejected}


def _registry_functions():
    api = ctypes.WinDLL('advapi32', use_last_error=True)
    api.RegOpenKeyExW.argtypes = [wintypes.HKEY, wintypes.LPCWSTR, wintypes.DWORD,
                                 wintypes.DWORD, ctypes.POINTER(wintypes.HKEY)]
    api.RegOpenKeyExW.restype = wintypes.LONG
    api.RegEnumValueW.argtypes = [wintypes.HKEY, wintypes.DWORD, wintypes.LPWSTR,
                                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
                                 ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    api.RegEnumValueW.restype = wintypes.LONG
    api.RegCloseKey.argtypes = [wintypes.HKEY]
    api.RegCloseKey.restype = wintypes.LONG
    return api


def _windows_source(source_id, api=None):
    """RegEnumValueW with NULL lpData AND lpcbData: no data or size requested."""
    root = 0x80000001 if source_id == 'windows_user' else 0x80000002
    subkey = _LOCATIONS[source_id].split('\\', 1)[1]
    handle = wintypes.HKEY()
    try:
        api = _registry_functions() if api is None else api
        # Predefined HKEY constants are sign-extended on 64-bit Windows.
        root_handle = wintypes.HKEY(ctypes.c_int32(root).value)
        code = api.RegOpenKeyExW(root_handle, subkey, 0, 0x0001, ctypes.byref(handle))
        if code:
            return {'id': source_id, 'status': 'unavailable', 'names': [], 'error_code': int(code)}
        names, rejected, examined = [], 0, 0
        status = 'truncated_budget'
        try:
            for index in range(MAX_SOURCE_NAMES):
                buffer = ctypes.create_unicode_buffer(MAX_NAME_LENGTH + 1)
                length = wintypes.DWORD(len(buffer))
                code = api.RegEnumValueW(handle, index, buffer, ctypes.byref(length),
                                         None, None, None, None)
                if code == 259:  # ERROR_NO_MORE_ITEMS
                    status = 'scanned'
                    break
                examined += 1
                if code == 234:  # ERROR_MORE_DATA: name exceeds the public-name limit
                    rejected += 1
                    continue
                if code:
                    return {'id': source_id, 'status': 'unavailable', 'names': [],
                            'error_code': int(code)}
                names.append(buffer.value)
        finally:
            api.RegCloseKey(handle)
        source = snapshot_names(names, source_id, status)
        source['names_examined'] = examined
        source['names_rejected'] += rejected
        return source
    except (OSError, AttributeError, TypeError, ValueError):
        # Do not return exception strings: they could contain unrelated private metadata.
        return {'id': source_id, 'status': 'unavailable', 'names': []}


def persistent_name_sources(platform=None, api=None):
    platform = os.name if platform is None else platform
    if platform != 'nt':
        return [{'id': source_id, 'status': 'not_supported', 'names': []}
                for source_id in _PERSISTENT]
    return [_windows_source(source_id, api) for source_id in _PERSISTENT]


def name_presence(name, names_by_source, sources):
    """Describe name presence only; unavailable sources cannot prove absence."""
    key = name.casefold()
    found = [source_id for source_id in ('process',) + _PERSISTENT
             if key in names_by_source.get(source_id, {})]
    runtime = 'process' in found
    configured = any(source_id in found for source_id in _PERSISTENT)
    if not configured and any(source['id'] in _PERSISTENT and source['status'] != 'scanned'
                              for source in sources):
        configured = None
    return {'sources': found, 'runtime_available': runtime,
            'configured_in_system': configured, 'needs_restart': configured is True and not runtime,
            'value_included': False, 'capability_inferred': False,
            'evidence': {
                'source': {'status': 'name_observed' if found else 'name_not_observed',
                           'type': 'environment_variable_names', 'sources': found},
                'configuration': {'status': 'persistent_name_present' if configured is True else
                                  'unknown' if configured is None else 'persistent_name_absent',
                                  'evidence': 'Only names were enumerated; no values or value changes were checked.'},
                'runtime': {'status': 'name_present' if runtime else 'name_absent',
                            'evidence': 'Current process snapshot; an empty or invalid value is not detected.'},
                'callability': {'status': 'not_established',
                               'evidence': 'Name presence does not establish a protocol connection or callability.'},
                'actual_invocation': {'status': 'not_observed', 'evidence': 'No API or model call was performed.'},
            }}


def build_inventory(runtime_names, persistent_sources=(), associations=(), known_hints=None,
                    query='', limit=MAX_INVENTORY_ITEMS):
    """Pure injectable name-only inventory, with search applied before output pagination."""
    if not isinstance(query, str) or len(query) > 2000:
        raise ValueError('环境变量名称搜索须为不超过 2000 字符的文本。')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_INVENTORY_ITEMS:
        raise ValueError('环境变量清单数量超出允许范围。')
    snapshots = [snapshot_names(runtime_names)]
    supplied = {source.get('id'): source for source in persistent_sources if isinstance(source, dict)}
    for source_id in _PERSISTENT:
        source = supplied.get(source_id, {})
        status = source.get('status', 'unavailable')
        status = status if status in _STATUSES else 'unavailable'
        normalized = snapshot_names(source.get('names', ()), source_id, status)
        examined = source.get('names_examined')
        if (isinstance(examined, int) and not isinstance(examined, bool) and
                normalized['names_examined'] <= examined <= MAX_SOURCE_NAMES):
            normalized['names_examined'] = examined
        # Retain bounded reader rejection counts without exposing supplied metadata.
        count = source.get('names_rejected', 0)
        if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= MAX_SOURCE_NAMES:
            normalized['names_rejected'] += count
        code = source.get('error_code')
        if isinstance(code, int) and not isinstance(code, bool) and 0 <= code <= 0xffffffff:
            normalized['error_code'] = code
        snapshots.append(normalized)
    sources = [{key: value for key, value in source.items() if key != 'names'} for source in snapshots]
    for source in sources:
        source.update({'type': 'environment_variable_names', 'location': _LOCATIONS[source['id']],
                       'value_included': False})
    names_by_source = {source['id']: {name.casefold(): name for name in source['names']}
                       for source in snapshots}
    names = {}
    for source in snapshots:
        for name in source['names']:
            names.setdefault(name.casefold(), name)
    associations_by_name = {}
    for association in associations:
        name = association.get('name')
        if isinstance(name, str) and _NAME.fullmatch(name):
            associations_by_name.setdefault(name.casefold(), []).append(association)
    hints = {name.casefold(): provider for name, provider in (known_hints or {}).items()}
    search = query.strip().casefold()
    items = []
    # Explicit associations first so large process environments cannot hide manifest names.
    for key in sorted(names, key=lambda key: (key not in associations_by_name, key not in hints, key)):
        name = names[key]
        if search and search not in key:
            continue
        linked = associations_by_name.get(key, [])
        item = dict(name_presence(name, names_by_source, sources), name=name,
                    association_status='manifest_declared' if linked else
                    'known_name_hint' if key in hints else 'unassociated',
                    provider_hint=hints.get(key, ''), provider='', domains=[], tools=[],
                    discovery_only=True)
        if linked:
            providers = sorted({entry.get('provider', '') for entry in linked if entry.get('provider')})
            item['provider'] = providers[0] if len(providers) == 1 else ''
            item['domains'] = sorted({domain for entry in linked for domain in entry.get('domains', [])})
            item['tools'] = sorted({tool for entry in linked for tool in entry.get('tools', [])})
        items.append(item)
    total = len(items)
    truncated = total > limit or any(source['status'] == 'truncated_budget' for source in sources)
    return {'items': items[:limit], 'sources': sources, 'total': total, 'returned': min(total, limit),
            'truncated': truncated, 'query': query.strip(), 'value_included': False}


def collect_inventory(environ=None, associations=(), known_hints=None, persistent_sources=None,
                      query='', limit=MAX_INVENTORY_ITEMS):
    # os.environ iteration obtains keys only; never copy dict(os.environ) or read .values().
    runtime_names = os.environ if environ is None else environ
    persistent_sources = persistent_name_sources() if persistent_sources is None else persistent_sources
    return build_inventory(runtime_names, persistent_sources, associations, known_hints, query, limit)
