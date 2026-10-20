"""On-demand, bounded model-index diagnostics. No traversal, content reads or writes.

A metadata observation is not a model/package count, content-duplicate verdict,
allocated-disk measurement, or permission to delete anything.
"""
import collections
import datetime
import os
import re
import stat

from . import classification, meta

MAX_ROWS = 5000


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def intake_hint(path, size):
    """Location/extension hints only: never demote every .pth/.bin weight."""
    parts = re.split(r"[\\/]", str(path))
    parents = {part.casefold() for part in parts[:-1]}
    ext = os.path.splitext(parts[-1])[1].casefold()
    if '.no_exist' in parents and size == 0:
        return 'nonmodel_candidate', 'cache_absence_placeholder'
    if ext == '.pth' and parents & {'site-packages', 'dist-packages'}:
        return 'nonmodel_candidate', 'python_path_configuration_location'
    if parents & {'test', 'tests', 'fixtures', 'test_data'} and size is not None and size < 1024:
        return 'nonmodel_candidate', 'small_test_fixture_location'
    if ext == '.bin':
        evidence = meta.bin_model_evidence(path, size)
        if evidence == 'candidate':
            return ('nonmodel_candidate', 'runtime_cache_binary_location') if parents & {'cache', '.cache', '__pycache__'} else ('unknown', 'generic_binary_unconfirmed')
        return 'model_candidate', evidence
    if ext in meta.MODEL_EXTS:
        return 'model_candidate', 'extension_only'
    return 'unknown', 'format_unconfirmed'


def build_report(rows, facts=None):
    """Summaries apply ONLY to these index rows; do not add batches together."""
    if len(rows) > MAX_ROWS:
        raise ValueError('诊断批次超出上限。')
    facts = facts or classification.FileFacts()
    items, groups = [], collections.defaultdict(list)
    counts = collections.Counter()
    indexed_bytes, observed_bytes, unidentified_bytes = 0, 0, 0
    for raw in rows:
        row = dict(raw)
        path = row.get('path')
        recorded = _integer(row.get('size'))
        counts['indexed_size_unknown_rows'] += recorded is None
        indexed_bytes += recorded or 0
        info = facts.stat(path)
        item = {'rowid_pk': row['rowid_pk'], 'path': path, 'filename': row.get('filename'),
                'indexed_bytes': recorded, 'observed_bytes': None, 'identity': None,
                'indexed_missing': bool(row.get('missing')), 'state_conflict': False,
                'size_conflict': False, 'link_alias': False,
                'intake': 'unknown', 'intake_reason': 'file_not_observed'}
        if info is None:
            item['status'] = facts.errors.get(classification.path_key(path), 'unavailable')
        elif not stat.S_ISREG(info.st_mode):
            item['status'] = 'not_regular'
        else:
            item['status'] = 'present'
            size = _integer(info.st_size)
            item['observed_bytes'] = size
            counts['observed_size_unknown_rows'] += size is None
            observed_bytes += size or 0
            identity = facts.identity(path)
            item['identity'] = identity
            try:
                resolved = classification.path_key(os.path.realpath(path))
                item['link_alias'] = resolved != classification.path_key(path)
            except (OSError, ValueError, TypeError):
                resolved = None
            item['intake'], item['intake_reason'] = intake_hint(path, size)
            if identity:
                groups[identity].append((item, info, resolved))
            else:
                counts['identity_unknown_rows'] += 1
                unidentified_bytes += size or 0
            item['size_conflict'] = recorded is not None and size is not None and recorded != size
            counts['size_conflict_rows'] += item['size_conflict']
            counts['link_alias_rows'] += item['link_alias']
            counts[item['intake'] + '_rows'] += 1
        # Errors and non-regular paths do not prove that a file is missing.
        item['state_conflict'] = (item['status'] == 'present' and item['indexed_missing']) or (item['status'] == 'missing' and not item['indexed_missing'])
        counts['state_conflict_rows'] += item['state_conflict']
        counts[item['status'] + '_rows'] += 1
        items.append(item)

    identified_bytes, alias_bytes, alias_groups, hardlink_groups = 0, 0, [], []
    stable_files = 0
    for identity, members in groups.items():
        # Separate observations can race a replacement/write. Do not claim an
        # exact length for an identity whose metadata changed during the request.
        versions = {(m[1].st_size, getattr(m[1], 'st_mtime_ns', m[1].st_mtime)) for m in members}
        stable = len(versions) == 1 and all(m[0]['observed_bytes'] is not None for m in members)
        if not stable:
            counts['changed_identity_groups'] += 1
            for item, _, _ in members:
                item['identity_changed'] = True
            continue
        stable_files += 1
        size = members[0][0]['observed_bytes']
        identified_bytes += size
        paths = {classification.path_key(m[0]['path']) for m in members}
        resolved = {m[2] for m in members if m[2] is not None}
        if len(members) > 1:
            alias_bytes += size * (len(members) - 1)
            alias_groups.append({'identity': identity, 'rowids': [m[0]['rowid_pk'] for m in members],
                                 'indexed_paths': len(paths), 'file_bytes': size})
        if len(resolved) > 1 and all(getattr(m[1], 'st_nlink', 0) > 1 for m in members):
            hardlink_groups.append(identity)

    fields = ('present_rows', 'missing_rows', 'denied_rows', 'unavailable_rows', 'invalid_path_rows',
              'not_regular_rows', 'identity_unknown_rows', 'state_conflict_rows', 'size_conflict_rows',
              'link_alias_rows', 'nonmodel_candidate_rows', 'model_candidate_rows', 'unknown_rows',
              'indexed_size_unknown_rows', 'observed_size_unknown_rows', 'changed_identity_groups')
    summary = {key: counts[key] for key in fields}
    summary.update(indexed_rows=len(items), indexed_bytes=indexed_bytes,
                   observed_path_bytes=observed_bytes, identified_files=stable_files,
                   identified_file_bytes=identified_bytes, unidentified_path_bytes=unidentified_bytes,
                   alias_rows=sum(len(g['rowids']) - 1 for g in alias_groups),
                   alias_path_bytes=alias_bytes, same_file_alias_groups=len(alias_groups),
                   hardlink_groups=len(hardlink_groups), reclaimable_bytes=None,
                   model_package_count=None, allocated_disk_bytes=None)
    complete = not any(counts[k] for k in ('missing_rows', 'denied_rows', 'unavailable_rows',
                     'invalid_path_rows', 'not_regular_rows', 'identity_unknown_rows',
                     'changed_identity_groups', 'observed_size_unknown_rows'))
    return {'schema': 'aihub-model-governance/1', 'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'read_only': True, 'summary': summary, 'identity_coverage_complete': complete,
            'items': items, 'same_file_aliases': alias_groups, 'hardlink_identities': hardlink_groups,
            'evidence': {'filesystem': 'stat_metadata_only', 'content_hash': 'not_run',
                         'dependencies': 'not_checked', 'inference': 'not_run'},
            'impact_plan': [
                {'step': 'verify_inventory', 'state': 'observation_only'},
                {'step': 'confirm_content_and_model_role', 'state': 'pending'},
                {'step': 'check_workflows_loaders_and_training_retention', 'state': 'pending'},
                {'step': 'record_reverse_paths_and_restore_plan', 'state': 'pending'},
                {'step': 'obtain_separate_disposition_approval', 'state': 'required'}],
            'safe_to_delete': False}
