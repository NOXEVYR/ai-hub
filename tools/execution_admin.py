"""Create, reconcile or revoke scoped grants on a selected native installation.

Create: --install-root DIR --workspace-root DIR --role source --subject ID --output FILE
Revoke: --install-root DIR --workspace-root DIR --grant-id UUID
List: --install-root DIR --workspace-root DIR --list [--limit 20] [--after-grant-id UUID]
No proxy, redirect, credential display, or automatic grant retry is supported.
"""
import argparse
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import sys
import uuid
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aihub import config, harnesses, service_control

PROTOCOL = 'aihub-execution/1'
EXPORT_SCHEMA = 'ai-hub-execution-grant/1'
MAX_JSON_BYTES = 131072
LIST_KEYS = {'protocol', 'authority_id', 'ledger_epoch', 'items', 'has_more', 'next_after_grant_id'}
ITEM_KEYS = {'grant_id', 'role', 'subject', 'created_at', 'revoked_at'}
_CREDENTIAL_SHAPE = re.compile(r'(?:^|[^A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?:$|[^A-Za-z0-9_-])|(?:^|[^A-Za-z0-9])[a-f0-9]{64}(?:$|[^A-Za-z0-9])', re.I)


class AdminError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AdminError('invalid_json')
        result[key] = value
    return result


def _decode(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(AdminError('invalid_json')))


def _directory(value):
    raw = os.fspath(value)
    if (not os.path.isabs(raw) or raw.startswith(('\\\\', '//'))
            or '..' in raw.replace('\\', '/').split('/') or any(ord(c) < 32 for c in raw)):
        raise AdminError('invalid_path')
    path = Path(os.path.abspath(raw))
    config._check_ancestors(str(path))
    if not path.is_dir():
        raise AdminError('invalid_path')
    return path


def _same_path(first, second):
    return isinstance(second, str) and os.path.isabs(second) and os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)


def _control(install):
    path = install / 'data' / 'desktop' / 'server-control.json'
    try:
        config._check_ancestors(str(path.parent))
        service_control._regular(path)
        before = path.lstat()
        if before.st_size > 16384:
            raise AdminError('control_invalid')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            if _signature(os.fstat(stream.fileno())) != _signature(before):
                raise AdminError('control_invalid')
            raw = stream.read(16385)
            if len(raw) > 16384 or _signature(os.fstat(stream.fileno())) != _signature(before):
                raise AdminError('control_invalid')
        config._check_ancestors(str(path.parent))
        service_control._regular(path)
        if _signature(path.lstat()) != _signature(before):
            raise AdminError('control_invalid')
        record = _decode(raw)
        if (not isinstance(record, dict) or record.get('schema') != service_control.PROTOCOL
                or record.get('app') != 'ai-hub' or not _same_path(install, record.get('install_root'))
                or not _same_path(install / 'data', record.get('data_root'))
                or type(record.get('port')) is not int or not 1 <= record['port'] <= 65535
                or not isinstance(record.get('instance_id'), str)
                or not re.fullmatch(r'[0-9a-f]{32}', record['instance_id'])
                or not isinstance(record.get('token'), str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', record['token'])):
            raise AdminError('control_invalid')
        return record
    except (OSError, ValueError, TypeError, RecursionError):
        raise AdminError('control_invalid') from None


def _http(record, path, body=None, owner=False):
    conn = http.client.HTTPConnection('127.0.0.1', record['port'], timeout=5)
    try:
        headers = {'Content-Type': 'application/json'}
        if owner:
            headers['X-AIHub-Control-Token'] = record['token']
        encoded = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        conn.request('GET' if body is None else 'POST', path, encoded, headers)
        response = conn.getresponse()
        raw = response.read(MAX_JSON_BYTES + 1)
        if response.status != 200:
            # Do not parse or print a server error body that could contain secrets.
            return response.status, None
        if len(raw) > MAX_JSON_BYTES:
            raise AdminError('invalid_response')
        result = _decode(raw)
        if not isinstance(result, dict):
            raise AdminError('invalid_response')
        return response.status, result
    finally:
        conn.close()


def _identity(value, record, *, require_port=False):
    if (value.get('app') != 'ai-hub' or value.get('control_protocol') != service_control.PROTOCOL
            or value.get('service_instance_id') != record['instance_id']
            or not _same_path(record['install_root'], value.get('install_root'))
            or ('port' in value and (type(value['port']) is not int or value['port'] != record['port']))
            or (require_port and 'port' not in value)):
        raise AdminError('identity_unverified')


def _describe(record, workspace):
    code, result = _http(record, '/api/execution/describe')
    if code != 200 or result.get('protocol') != PROTOCOL:
        raise AdminError('identity_unverified')
    identity = result.get('identity')
    scope = result.get('workspace')
    if not isinstance(identity, dict) or not isinstance(scope, dict):
        raise AdminError('identity_unverified')
    _identity(identity, record, require_port=True)
    if (identity.get('status') != 'available' or scope.get('status') != 'available'
            or not _same_path(workspace, scope.get('root'))
            or not isinstance(scope.get('binding_revision'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', scope['binding_revision'])
            or not isinstance(result.get('connection_revision'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', result['connection_revision'])):
        raise AdminError('workspace_unverified')
    return result


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _safe_subject(value):
    return (isinstance(value, str) and 0 < len(value) <= 200 and value == value.strip()
            and not any(unicodedata.category(c) in {'Cc', 'Cs'} for c in value)
            and not harnesses._SECRETS.search(value) and not _CREDENTIAL_SHAPE.search(value))


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:\+00:00|Z)', value):
        raise AdminError('grant_list_unverified')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except ValueError:
        raise AdminError('grant_list_unverified') from None


def _validate_list(result, limit, after_grant_id, owner_token):
    """Rebuild the dedicated public schema; never return the raw HTTP object."""
    if (not isinstance(result, dict) or set(result) != LIST_KEYS or result['protocol'] != PROTOCOL
            or type(result['has_more']) is not bool or not isinstance(result['items'], list)
            or len(result['items']) > limit):
        raise AdminError('grant_list_unverified')
    authority, epoch = result['authority_id'], result['ledger_epoch']
    if authority is None or epoch is None:
        if authority is not None or epoch is not None or result['items'] or result['has_more'] or result['next_after_grant_id'] is not None:
            raise AdminError('grant_list_unverified')
    elif not _uuid(authority) or not _uuid(epoch):
        raise AdminError('grant_list_unverified')
    items, seen, previous = [], set(), None
    for item in result['items']:
        if (not isinstance(item, dict) or set(item) != ITEM_KEYS or not _uuid(item['grant_id'])
                or item['grant_id'] in seen or item['grant_id'] == after_grant_id
                or item['role'] not in {'source', 'source_read', 'worker'} or not _safe_subject(item['subject'])
                or owner_token in item['subject']):
            raise AdminError('grant_list_unverified')
        created = _timestamp(item['created_at'])
        if item['revoked_at'] is not None:
            _timestamp(item['revoked_at'])
        order = (created, item['grant_id'])
        if previous is not None and order <= previous:
            raise AdminError('grant_list_unverified')
        previous = order
        seen.add(item['grant_id'])
        items.append({key: item[key] for key in ('grant_id', 'role', 'subject', 'created_at', 'revoked_at')})
    next_id = result['next_after_grant_id']
    if ((result['has_more'] and (len(items) != limit or not items or next_id != items[-1]['grant_id']))
            or (not result['has_more'] and next_id is not None)):
        raise AdminError('grant_list_unverified')
    safe = {'protocol': PROTOCOL, 'authority_id': authority, 'ledger_epoch': epoch,
            'items': items, 'has_more': result['has_more'], 'next_after_grant_id': next_id}
    encoded = json.dumps(safe, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    if len(encoded.encode('ascii')) > MAX_JSON_BYTES or owner_token in encoded:
        raise AdminError('grant_list_unverified')
    return safe


def list_grants(install_root, workspace_root, *, limit=20, after_grant_id=None):
    """Owner-only read, with scope/ledger checks before and after the response."""
    try:
        if type(limit) is not int or not 1 <= limit <= 50 or (after_grant_id is not None and not _uuid(after_grant_id)):
            raise AdminError('invalid_arguments')
        install, workspace = _directory(install_root), _directory(workspace_root)
        record = _control(install)
        code, health = _http(record, '/api/health')
        if code != 200:
            raise AdminError('identity_unverified')
        _identity(health, record)
        before = _describe(record, workspace)
        body = {'_workspace_root': str(workspace), 'instance_id': record['instance_id'],
                'install_root': record['install_root'], 'limit': limit}
        if after_grant_id is not None:
            body['after_grant_id'] = after_grant_id
        code, result = _http(record, '/api/execution/grant_list', body, True)
        if code in {400, 403, 404, 409}:
            raise AdminError('grant_list_denied')
        if code != 200:
            raise AdminError('grant_list_unverified')
        safe = _validate_list(result, limit, after_grant_id, record['token'])
        current = _describe(record, workspace)
        if (current['workspace']['binding_revision'] != before['workspace']['binding_revision']
                or current['connection_revision'] != before['connection_revision']
                or any(desc.get('execution_authority_id') != safe['authority_id']
                       or desc.get('ledger_epoch') != safe['ledger_epoch'] for desc in (before, current))):
            raise AdminError('grant_list_unverified')
        return safe
    except AdminError:
        raise
    except Exception:
        raise AdminError('grant_list_unverified') from None


def _output_path(value):
    raw = os.fspath(value)
    if (not os.path.isabs(raw) or raw.startswith(('\\\\', '//'))
            or '..' in raw.replace('\\', '/').split('/') or any(ord(c) < 32 for c in raw)):
        raise AdminError('invalid_output')
    path = Path(os.path.abspath(raw))
    _directory(path.parent)
    if os.path.lexists(path):
        raise AdminError('output_exists')
    # Prevent alternate streams and Windows reserved file names.
    name = path.name
    reserved = {'con', 'prn', 'aux', 'nul'} | {'com%d' % n for n in range(1, 10)} | {'lpt%d' % n for n in range(1, 10)}
    if (not name or name.rstrip(' .') != name or name.split('.')[0].casefold() in reserved
            or re.search(r'[\\/:*?"<>|]', name)):
        raise AdminError('invalid_output')
    return path


def manage(install_root, workspace_root, *, role=None, subject=None, output=None, grant_id=None):
    """Return only safe completion metadata; credentials never leave the export file."""
    reserved_fd = None
    initial = None
    output_path = None
    submitted = False
    saving = False
    try:
        install, workspace = _directory(install_root), _directory(workspace_root)
        create = grant_id is None
        if create:
            if (role not in {'source', 'source_read', 'worker'} or not _safe_subject(subject) or output is None):
                raise AdminError('invalid_arguments')
            output_path = _output_path(output)  # Refuse existing outputs before any network request.
        elif not _uuid(grant_id) or any(value is not None for value in (role, subject, output)):
            raise AdminError('invalid_arguments')
        record = _control(install)
        if create and (record['token'] in subject or record['token'] in str(output_path)):
            raise AdminError('invalid_arguments')
        code, health = _http(record, '/api/health')
        if code != 200:
            raise AdminError('identity_unverified')
        _identity(health, record)
        desc = _describe(record, workspace)
        if create:
            reserved_fd = os.open(output_path, os.O_RDWR | os.O_CREAT | os.O_EXCL
                                  | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            initial = _signature(os.fstat(reserved_fd))
            config._check_ancestors(str(output_path.parent))
            service_control._regular(output_path)
            if _signature(output_path.lstat()) != initial:
                raise AdminError('output_unavailable')
            service_control._private(output_path)
        body = {'_workspace_root': str(workspace), 'instance_id': record['instance_id'],
                'install_root': record['install_root']}
        body.update({'role': role, 'subject': subject} if create else {'grant_id': grant_id})
        submitted = True
        code, result = _http(record, '/api/execution/' + ('grant_create' if create else 'grant_revoke'), body, True)
        if code in {400, 403, 404, 409}:
            submitted = False
            raise AdminError('grant_denied')
        if code != 200:
            raise AdminError('grant_status_unverified')
        if not create:
            if result.get('revoked') is not True:
                raise AdminError('grant_status_unverified')
            return {'status': 'revoked'}
        if (result.get('protocol') != PROTOCOL or result.get('role') != role or result.get('subject') != subject
                or result.get('secret_returned_once') is not True
                or not isinstance(result.get('token'), str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', result['token'])
                or result['token'] == record['token']
                or result['token'] in str(output_path)
                or not all(_uuid(result.get(key)) for key in ('grant_id', 'authority_id', 'ledger_epoch'))):
            raise AdminError('grant_status_unverified')
        current = _describe(record, workspace)
        if (current['workspace']['binding_revision'] != desc['workspace']['binding_revision']
                or current['connection_revision'] != desc['connection_revision']
                or current.get('execution_authority_id') != result['authority_id']
                or current.get('ledger_epoch') != result['ledger_epoch']):
            raise AdminError('grant_status_unverified')
        exported = {'schema': EXPORT_SCHEMA, 'protocol': PROTOCOL, 'grant_id': result['grant_id'],
                    'role': role, 'subject': subject, 'token': result['token'],
                    'workspace_root': str(workspace), 'workspace_binding_revision': current['workspace']['binding_revision'],
                    'execution_authority_id': result['authority_id'], 'ledger_epoch': result['ledger_epoch'],
                    'connection': {'scheme': 'http', 'host': '127.0.0.1', 'port': record['port'],
                                   'app': 'ai-hub', 'install_root': str(install),
                                   'service_instance_id': record['instance_id'], 'control_protocol': service_control.PROTOCOL,
                                   'connection_revision': current['connection_revision']}}
        encoded = (json.dumps(exported, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
        if record['token'].encode() in encoded:
            raise AdminError('grant_status_unverified')
        saving = True
        config._check_ancestors(str(output_path.parent))
        service_control._regular(output_path)
        if _signature(output_path.lstat()) != initial or _signature(os.fstat(reserved_fd)) != initial:
            raise AdminError('grant_accepted_export_failed')
        with os.fdopen(os.dup(reserved_fd), 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        service_control._regular(output_path)
        config._check_ancestors(str(output_path.parent))
        if _signature(output_path.lstat()) != _signature(os.fstat(reserved_fd)):
            raise AdminError('grant_accepted_export_failed')
        return {'status': 'created', 'output': str(output_path)}
    except Exception as error:
        if submitted:
            code = 'grant_accepted_export_failed' if saving else 'grant_status_unverified'
        else:
            code = error.code if isinstance(error, AdminError) else 'local_operation_failed'
        raise AdminError(code) from None
    finally:
        if reserved_fd is not None:
            reserved_state = _signature(os.fstat(reserved_fd))
            os.close(reserved_fd)  # Windows cannot unlink this open reservation.
            try:
                # Only remove an unchanged, empty reservation owned by this operation.
                if initial == reserved_state and os.path.lexists(output_path):
                    config._check_ancestors(str(output_path.parent))
                    if initial == _signature(output_path.lstat()):
                        output_path.unlink()
            except (OSError, ValueError):
                pass


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise AdminError('invalid_arguments')


def main(argv=None):
    try:
        parser = _Parser(description=__doc__)
        parser.add_argument('--install-root', required=True)
        parser.add_argument('--workspace-root', required=True)
        parser.add_argument('--role', choices=('source', 'source_read', 'worker'))
        parser.add_argument('--subject')
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument('--output')
        action.add_argument('--grant-id')
        action.add_argument('--list', action='store_true')
        parser.add_argument('--limit', type=int)
        parser.add_argument('--after-grant-id')
        args = parser.parse_args(argv)
        if args.list:
            if any(value is not None for value in (args.role, args.subject, args.output, args.grant_id)):
                raise AdminError('invalid_arguments')
            result = list_grants(args.install_root, args.workspace_root,
                                 limit=20 if args.limit is None else args.limit, after_grant_id=args.after_grant_id)
            print(json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(',', ':')))
            return 0
        if args.limit is not None or args.after_grant_id is not None:
            raise AdminError('invalid_arguments')
        result = manage(args.install_root, args.workspace_root, role=args.role, subject=args.subject,
                        output=args.output, grant_id=args.grant_id)
        print('已创建接入，私有文件已保存：' + result['output'] if result['status'] == 'created' else '已撤销接入。')
        return 0
    except AdminError as error:
        suffix = ' 接入待核对，请勿自动重复创建。' if error.code in {'grant_status_unverified', 'grant_accepted_export_failed'} else ''
        print(error.code + suffix, file=sys.stderr)
        return 1
    except Exception:
        print('local_operation_failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
