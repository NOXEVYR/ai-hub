"""Resolve one explicitly selected installation; never probe ports or read keys."""
import http.client
import hashlib
import json
import os
from pathlib import Path
import stat
import re

PROTOCOL = 'aihub-mcp-endpoint/1'


def installation_identity(root):
    return hashlib.sha256(os.path.normcase(os.path.normpath(str(root))).encode('utf-8')).hexdigest()


class EndpointError(ValueError):
    def __init__(self, code, retryable=False):
        super().__init__('AI Hub installation endpoint unavailable; verify the selected installation')
        self.code, self.retryable = code, retryable


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _plain(path, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or
            getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400) or
            (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))):
        raise EndpointError('endpoint_config_invalid')
    return info


def _port(root):
    path = root / 'data' / 'config.json'
    _plain(path.parent, directory=True)
    before = _plain(path)
    if before.st_nlink != 1 or not 2 <= before.st_size <= 1024 * 1024:
        raise EndpointError('endpoint_config_invalid')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if len(raw) > 1024 * 1024 or identity(before) != identity(opened) or identity(opened) != identity(after):
        raise EndpointError('endpoint_config_changed', True)
    cfg = _json(raw)
    value = cfg.get('server', {}).get('port') if isinstance(cfg, dict) and isinstance(cfg.get('server'), dict) else None
    if type(value) is not int or not 1024 <= value <= 65535:
        raise EndpointError('endpoint_config_invalid')
    return value


def resolve_endpoint(install_root, *, include_identity=False):
    """Recheck config + public health before a bridge operation; no control token."""
    try:
        root = Path(install_root).resolve(strict=True)
        _plain(root, directory=True)
        port = _port(root)
    except EndpointError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise EndpointError('endpoint_config_invalid') from None
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        conn.request('GET', '/api/health')
        response = conn.getresponse()
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536:
            raise EndpointError('endpoint_identity_invalid')
        health = _json(raw)
        if (not isinstance(health, dict) or health.get('app') != 'ai-hub' or
                health.get('control_protocol') != 'ai-hub-local-control-v1' or
                not isinstance(health.get('service_instance_id'), str) or
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', health['service_instance_id']) or
                not isinstance(health.get('install_root'), str) or
                not os.path.isabs(health['install_root']) or
                os.path.normcase(os.path.normpath(health['install_root'])) != os.path.normcase(str(root))):
            raise EndpointError('endpoint_identity_invalid')
        if include_identity and health.get('mcp_endpoint_binding') != PROTOCOL:
            raise EndpointError('endpoint_identity_invalid')
        if _port(root) != port:
            raise EndpointError('endpoint_config_changed', True)
        return {'port': port, 'install_identity': installation_identity(root),
                'service_instance_id': health['service_instance_id']} if include_identity else port
    except EndpointError:
        raise
    except (OSError, http.client.HTTPException):
        raise EndpointError('network_unavailable', True) from None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise EndpointError('endpoint_identity_invalid') from None
    finally:
        conn.close()
